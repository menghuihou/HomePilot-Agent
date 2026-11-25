"""知识 chunker 纯函数测试：确定性、metadata 隔离与通用兜底过滤。"""

import hashlib
import re
from typing import Any

from app.config import get_settings
from app.knowledge.chunker import (
    GENERIC_SYMPTOM_KEY,
    KnowledgeDomain,
    generate_domain_chunks,
    generate_fault_knowledge_chunks,
    load_fault_knowledge_domains,
)


def _domain(symptom_causes: list[dict[str, Any]]) -> KnowledgeDomain:
    """构造包含具体症状与 generic 兜底的最小知识域。"""
    data = {
        "device_type": "light",
        "symptoms": [
            {"symptom_key": "light_not_on", "description": "灯完全不亮"},
            {"symptom_key": "light_flicker", "description": "灯闪烁"},
            {"symptom_key": GENERIC_SYMPTOM_KEY, "description": "设备异常"},
        ],
        "causes": [
            {
                "cause_key": "bulb_burned",
                "cause_name": "灯泡损坏",
                "description": "灯泡寿命到期。",
                "prevalence": 0.3,
                "investigation_weight": 5,
                "fix_action": "replace_bulb",
                "fix_suggestion": "更换同型号灯泡。",
            },
            {
                "cause_key": "unknown_fault",
                "cause_name": "未知故障",
                "description": "无法归类的异常。",
                "prevalence": 0.1,
                "investigation_weight": 1,
                "fix_action": None,
                "fix_suggestion": "继续观察并收集日志。",
            },
        ],
        "symptom_causes": symptom_causes,
    }
    return KnowledgeDomain(
        source_key="lighting_knowledge",
        source_title="照明故障知识库",
        device_type="light",
        data=data,
    )


def test_load_all_domains_and_generate_one_chunk_per_cause():
    domains = load_fault_knowledge_domains()
    version = get_settings().knowledge_version
    chunks = generate_fault_knowledge_chunks(version)

    assert [domain.source_key for domain in domains] == [
        "ac_knowledge",
        "gateway_knowledge",
        "lighting_knowledge",
        "sensor_knowledge",
    ]
    assert len(chunks) == sum(len(domain.data["causes"]) for domain in domains)
    assert len(chunks) == len({chunk.chunk_hash for chunk in chunks})
    assert all(chunk.knowledge_version == version for chunk in chunks)


def test_domain_chunks_are_deterministic():
    domain = _domain(
        [
            {"symptom": "light_not_on", "causes": ["bulb_burned"]},
            {"symptom": "light_flicker", "causes": ["bulb_burned"]},
            {"symptom": GENERIC_SYMPTOM_KEY, "causes": ["unknown_fault"]},
        ]
    )

    first = generate_domain_chunks(domain, "2026.11-test-v1")
    second = generate_domain_chunks(domain, "2026.11-test-v1")

    assert first == second
    assert [chunk.chunk_key for chunk in first] == [
        "cause:bulb_burned",
        "cause:unknown_fault",
    ]
    assert [chunk.metadata["chunk_index"] for chunk in first] == [0, 1]


def test_generic_symptom_does_not_pollute_specific_cause_metadata():
    domain = _domain(
        [
            {"symptom": "light_not_on", "causes": ["bulb_burned"]},
            {"symptom": "light_flicker", "causes": ["bulb_burned"]},
            {
                "symptom": GENERIC_SYMPTOM_KEY,
                "causes": ["bulb_burned", "unknown_fault"],
            },
        ]
    )

    chunks = {chunk.chunk_key: chunk for chunk in generate_domain_chunks(domain, "v1")}

    assert chunks["cause:bulb_burned"].metadata["symptom_keys"] == [
        "light_not_on",
        "light_flicker",
    ]
    assert chunks["cause:unknown_fault"].metadata["symptom_keys"] == [
        GENERIC_SYMPTOM_KEY
    ]
    assert all(
        chunk.metadata["device_type"] == domain.device_type for chunk in chunks.values()
    )


def test_chunk_text_and_search_text_keep_structured_terms():
    domain = _domain(
        [
            {"symptom": "light_not_on", "causes": ["bulb_burned"]},
            {"symptom": GENERIC_SYMPTOM_KEY, "causes": ["unknown_fault"]},
        ]
    )

    chunks = {chunk.chunk_key: chunk for chunk in generate_domain_chunks(domain, "v1")}
    specific = chunks["cause:bulb_burned"]
    fallback = chunks["cause:unknown_fault"]

    assert specific.chunk_text.startswith("设备域：light")
    assert "light_not_on（灯完全不亮）" in specific.chunk_text
    assert "replace_bulb" in specific.chunk_text
    assert "cause:bulb_burned" not in specific.chunk_text
    for term in (
        "lighting_knowledge",
        "light",
        "light_not_on",
        "bulb_burned",
        "replace_bulb",
    ):
        assert term in specific.search_text
    assert "manual_inspection" in fallback.search_text


def test_chunk_hash_is_sha256_of_canonical_chunk_text():
    domain = _domain(
        [
            {"symptom": "light_not_on", "causes": ["bulb_burned"]},
            {"symptom": GENERIC_SYMPTOM_KEY, "causes": ["unknown_fault"]},
        ]
    )

    chunk = generate_domain_chunks(domain, "v1")[0]

    assert (
        chunk.chunk_hash == hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest()
    )


def test_seed_chunks_do_not_contain_sensitive_values():
    chunks = generate_fault_knowledge_chunks(get_settings().knowledge_version)
    sensitive_key_pattern = re.compile(
        r"(api[_-]?key\s*[=:]|password\s*[=:]|token\s*[=:]|secret\s*[=:]|"
        r"sk-[a-z0-9]{8,})",
    )
    phone_pattern = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

    for chunk in chunks:
        combined = f"{chunk.chunk_text}\n{chunk.search_text}"
        assert sensitive_key_pattern.search(combined) is None
        assert phone_pattern.search(combined) is None
