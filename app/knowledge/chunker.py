"""故障知识 chunker：把结构化种子转换成稳定的 RAG 知识块。

同一 source/version 内每个 cause 恰好生成一个 chunk。``device_abnormal``
只是通用兜底症状：只有未挂到任何具体症状的原因才使用它，避免兜底
映射污染所有 chunk 的 metadata。
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jieba

GENERIC_SYMPTOM_KEY = "device_abnormal"
DATA_DIR = Path(__file__).resolve().parents[2] / "db" / "seeds" / "data"
_SOURCE_TITLES = {
    "ac": "空调故障知识库",
    "gateway": "网关故障知识库",
    "light": "照明故障知识库",
    "sensor": "传感器故障知识库",
}


@dataclass(frozen=True, slots=True)
class KnowledgeDomain:
    """一个设备域 JSON 种子的加载结果。"""

    source_key: str
    source_title: str
    device_type: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class KnowledgeChunkSeed:
    """可写入库的 chunk 值对象；embedding 由摄取器按需补齐。"""

    source_key: str
    source_title: str
    device_type: str
    knowledge_version: str
    chunk_key: str
    title: str
    chunk_text: str
    search_text: str
    chunk_hash: str
    metadata: dict[str, Any]


def _duplicates(keys: list[str]) -> set[str]:
    """返回列表中出现超过一次的元素集合。"""
    return {key for key in keys if keys.count(key) > 1}


def validate_knowledge_domain(source_key: str, data: Mapping[str, Any]) -> None:
    """校验 chunker 依赖的引用完整性，坏数据直接拒绝摄取。"""
    context = f"知识来源 {source_key}"
    if not data.get("device_type"):
        raise ValueError(f"{context}：缺少 device_type")
    for name in ("symptoms", "causes", "symptom_causes"):
        if not isinstance(data.get(name), list):
            raise ValueError(f"{context}：缺少 {name} 列表")

    symptom_keys = [item["symptom_key"] for item in data["symptoms"]]
    if duplicates := _duplicates(symptom_keys):
        raise ValueError(f"{context}：症状 key 重复 {sorted(duplicates)}")
    cause_keys = [item["cause_key"] for item in data["causes"]]
    if duplicates := _duplicates(cause_keys):
        raise ValueError(f"{context}：原因 key 重复 {sorted(duplicates)}")

    symptom_set = set(symptom_keys)
    cause_set = set(cause_keys)
    mapped: set[str] = set()
    for entry in data["symptom_causes"]:
        symptom_key = entry["symptom"]
        if symptom_key not in symptom_set:
            raise ValueError(f"{context}：映射引用了未定义症状 {symptom_key}")
        for cause_key in entry["causes"]:
            if cause_key not in cause_set:
                raise ValueError(f"{context}：映射引用了未定义原因 {cause_key}")
            mapped.add(cause_key)
    if orphans := cause_set - mapped:
        raise ValueError(f"{context}：原因未被任何症状引用 {sorted(orphans)}")

    for cause in data["causes"]:
        if not 0 < cause["prevalence"] <= 1:
            raise ValueError(f"{context}：prevalence 越界 {cause['cause_key']}")
        if not 1 <= cause["investigation_weight"] <= 5:
            raise ValueError(
                f"{context}：investigation_weight 越界 {cause['cause_key']}"
            )


def load_fault_knowledge_domains(
    data_dir: Path = DATA_DIR,
) -> list[KnowledgeDomain]:
    """加载全部 *_knowledge.json，文件名顺序固定，保证 chunk 顺序稳定。"""
    files = sorted(data_dir.glob("*_knowledge.json"))
    if not files:
        raise FileNotFoundError(f"未找到知识库数据文件：{data_dir}")

    domains: list[KnowledgeDomain] = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        source_key = path.stem
        device_type = data["device_type"]
        validate_knowledge_domain(source_key, data)
        domains.append(
            KnowledgeDomain(
                source_key=source_key,
                source_title=_SOURCE_TITLES.get(
                    device_type, f"{device_type} 故障知识库"
                ),
                device_type=device_type,
                data=data,
            )
        )
    return domains


def _symptom_groups(data: Mapping[str, Any]) -> dict[str, list[str]]:
    """只保留具体症状映射；generic 兜底仅在无具体症状时使用。"""
    specific: dict[str, list[str]] = defaultdict(list)
    generic: set[str] = set()
    for entry in data["symptom_causes"]:
        symptom_key = entry["symptom"]
        if symptom_key == GENERIC_SYMPTOM_KEY:
            generic.update(entry["causes"])
            continue
        for cause_key in entry["causes"]:
            specific[cause_key].append(symptom_key)
    return specific


def _canonical_chunk_text(
    domain: KnowledgeDomain,
    cause: Mapping[str, Any],
    symptom_keys: list[str],
    symptoms_by_key: Mapping[str, Mapping[str, Any]],
) -> str:
    """生成供展示、embedding 和 hash 使用的规范正文。"""
    descriptions: list[str] = []
    for key in symptom_keys:
        description = symptoms_by_key[key]["description"]
        descriptions.append(f"{key}（{description}）")
    fix_action = cause.get("fix_action") or "manual_inspection"
    fix_suggestion = cause["fix_suggestion"]
    return (
        f"设备域：{domain.device_type}\n"
        f"知识来源：{domain.source_key}\n"
        f"症状：{'；'.join(descriptions)}\n"
        f"原因：{cause['cause_key']}（{cause['cause_name']}）\n"
        f"原因说明：{cause['description']}\n"
        f"处理动作：{fix_action}\n"
        f"处理建议：{fix_suggestion}"
    )


def _search_text(
    domain: KnowledgeDomain,
    cause: Mapping[str, Any],
    symptom_keys: list[str],
    symptoms_by_key: Mapping[str, Mapping[str, Any]],
) -> str:
    """用 jieba 生成分词文本，并保留结构化 key 的原词用于精确召回。"""
    parts = [
        domain.source_key,
        domain.source_title,
        domain.device_type,
        *[f"{key} {symptoms_by_key[key]['description']}" for key in symptom_keys],
        cause["cause_key"],
        cause["cause_name"],
        cause["description"],
        cause.get("fix_action") or "manual_inspection",
        cause["fix_suggestion"],
    ]
    raw_text = "\n".join(part for part in parts if part)
    segmented = jieba.lcut_for_search(raw_text, HMM=True)
    exact_terms = [
        domain.source_key,
        domain.device_type,
        *symptom_keys,
        cause["cause_key"],
        cause.get("fix_action") or "manual_inspection",
    ]
    return " ".join(
        dict.fromkeys(
            token.strip()
            for token in (*segmented, *exact_terms)
            if isinstance(token, str) and token.strip()
        )
    )


def generate_domain_chunks(
    domain: KnowledgeDomain, knowledge_version: str
) -> list[KnowledgeChunkSeed]:
    """将一个设备域的全部原因转换为确定性排序的知识块。"""
    validate_knowledge_domain(domain.source_key, domain.data)
    if not 1 <= len(knowledge_version) <= 30:
        raise ValueError(f"knowledge_version 长度非法：{len(knowledge_version)}")

    symptoms_by_key = {item["symptom_key"]: item for item in domain.data["symptoms"]}
    specific_symptoms = _symptom_groups(domain.data)
    generic_causes = {
        cause_key
        for entry in domain.data["symptom_causes"]
        if entry["symptom"] == GENERIC_SYMPTOM_KEY
        for cause_key in entry["causes"]
    }

    chunks: list[KnowledgeChunkSeed] = []
    for chunk_index, cause in enumerate(domain.data["causes"]):
        cause_key = cause["cause_key"]
        symptom_keys = specific_symptoms[cause_key] or (
            [GENERIC_SYMPTOM_KEY] if cause_key in generic_causes else []
        )
        chunk_text = _canonical_chunk_text(domain, cause, symptom_keys, symptoms_by_key)
        metadata = {
            "device_type": domain.device_type,
            "knowledge_version": knowledge_version,
            "symptom_keys": symptom_keys,
            "cause_key": cause_key,
            "fix_action": cause.get("fix_action"),
            "chunk_index": chunk_index,
        }
        chunks.append(
            KnowledgeChunkSeed(
                source_key=domain.source_key,
                source_title=domain.source_title,
                device_type=domain.device_type,
                knowledge_version=knowledge_version,
                chunk_key=f"cause:{cause_key}",
                title=cause["cause_name"],
                chunk_text=chunk_text,
                search_text=_search_text(domain, cause, symptom_keys, symptoms_by_key),
                chunk_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                metadata=metadata,
            )
        )
    return chunks


def generate_fault_knowledge_chunks(
    knowledge_version: str, data_dir: Path = DATA_DIR
) -> list[KnowledgeChunkSeed]:
    """加载全部设备域并生成跨域 chunk 列表。"""
    return [
        chunk
        for domain in load_fault_knowledge_domains(data_dir)
        for chunk in generate_domain_chunks(domain, knowledge_version)
    ]
