"""加载故障知识库种子数据（第 3 周第 2 步）。

用法：

    uv run python -m db.seeds.load_fault_knowledge

幂等约定：症状按 (device_type, symptom_key) upsert，原因按
(device_type, cause_key) upsert；字段值未变化时不产生 UPDATE，
映射关系缺失时才插入，重复执行不会产生重复数据。
数据来源：全部条目由 AI 辅助整理，source_type 保持 synthetic，
人工逐条审核通过后应改为 manual。
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.database import session_factory
from app.models import FaultCause, FaultSymptom, SymptomCause

DATA_DIR = Path(__file__).resolve().parent / "data"
WEIGHT_MIN, WEIGHT_MAX = 1, 5


def _load_domains() -> list[tuple[str, dict[str, Any]]]:
    """读取全部域数据文件并做静态校验，返回 (文件名, 数据) 列表。"""
    files = sorted(DATA_DIR.glob("*_knowledge.json"))
    if not files:
        raise FileNotFoundError(f"未找到知识库数据文件：{DATA_DIR}")
    return [(path.name, json.loads(path.read_text(encoding="utf-8"))) for path in files]


def _duplicates(keys: list[str]) -> set[str]:
    """返回列表中出现超过一次的元素集合。"""
    return {key for key in keys if keys.count(key) > 1}


def _validate_domain(filename: str, data: dict[str, Any]) -> None:
    """校验单域数据的引用完整性与取值范围，坏数据直接拒绝导入。"""
    context = f"文件 {filename}"
    if not data.get("device_type"):
        raise ValueError(f"{context}：缺少 device_type")

    symptom_keys = [item["symptom_key"] for item in data["symptoms"]]
    if dup := _duplicates(symptom_keys):
        raise ValueError(f"{context}：症状 key 重复 {sorted(dup)}")
    cause_keys = [item["cause_key"] for item in data["causes"]]
    if dup := _duplicates(cause_keys):
        raise ValueError(f"{context}：原因 key 重复 {sorted(dup)}")

    symptom_set = set(symptom_keys)
    cause_set = set(cause_keys)
    mapped: set[str] = set()
    for entry in data["symptom_causes"]:
        if entry["symptom"] not in symptom_set:
            raise ValueError(f"{context}：映射引用了未定义症状 {entry['symptom']}")
        for key in entry["causes"]:
            if key not in cause_set:
                raise ValueError(f"{context}：映射引用了未定义原因 {key}")
            mapped.add(key)
    orphans = cause_set - mapped
    if orphans:
        raise ValueError(f"{context}：原因未被任何症状引用 {sorted(orphans)}")

    for cause in data["causes"]:
        if not 0 < cause["prevalence"] <= 1:
            raise ValueError(f"{context}：prevalence 越界 {cause['cause_key']}")
        if not WEIGHT_MIN <= cause["investigation_weight"] <= WEIGHT_MAX:
            raise ValueError(
                f"{context}：investigation_weight 越界 {cause['cause_key']}"
            )


def _cause_values(item: dict[str, Any]) -> dict[str, Any]:
    """提取原因条目的业务字段，供新增与更新比较复用。"""
    return {
        "cause_name": item["cause_name"],
        "description": item["description"],
        "prevalence": item["prevalence"],
        "investigation_weight": item["investigation_weight"],
        "fix_action": item["fix_action"],
        "fix_suggestion": item["fix_suggestion"],
    }


def _cause_row_values(row: FaultCause) -> dict[str, Any]:
    """从 ORM 行提取与 _cause_values 同构的字段，用于 diff 比较与更新。"""
    return {
        "cause_name": row.cause_name,
        "description": row.description,
        "prevalence": row.prevalence,
        "investigation_weight": row.investigation_weight,
        "fix_action": row.fix_action,
        "fix_suggestion": row.fix_suggestion,
    }


async def _seed() -> None:
    domains = _load_domains()
    for filename, data in domains:
        _validate_domain(filename, data)

    async with session_factory() as session:
        # 一次性预加载既有数据构建内存索引，避免逐条查询。
        symptom_rows = await session.execute(select(FaultSymptom))
        symptom_by_key = {
            (row.device_type, row.symptom_key): row for row in symptom_rows.scalars()
        }
        cause_rows = await session.execute(select(FaultCause))
        cause_by_key = {
            (row.device_type, row.cause_key): row for row in cause_rows.scalars()
        }
        mapping_pairs = set(
            await session.execute(
                select(SymptomCause.symptom_id, SymptomCause.cause_id)
            )
        )

        symptom_inserted = symptom_updated = 0
        cause_inserted = cause_updated = 0
        mapping_inserted = 0
        for _, data in domains:
            device_type = data["device_type"]

            symptom_ids: dict[str, int] = {}
            for item in data["symptoms"]:
                key = (device_type, item["symptom_key"])
                row = symptom_by_key.get(key)
                if row is None:
                    row = FaultSymptom(
                        device_type=device_type,
                        symptom_key=item["symptom_key"],
                        description=item["description"],
                    )
                    session.add(row)
                    # flush 后才能拿到自增主键，映射阶段要用 id 建关联。
                    await session.flush()
                    symptom_by_key[key] = row
                    symptom_inserted += 1
                elif row.description != item["description"]:
                    row.description = item["description"]
                    symptom_updated += 1
                symptom_ids[item["symptom_key"]] = row.id
            await session.flush()

            cause_ids: dict[str, int] = {}
            for item in data["causes"]:
                key = (device_type, item["cause_key"])
                values = _cause_values(item)
                row = cause_by_key.get(key)
                if row is None:
                    row = FaultCause(
                        device_type=device_type, cause_key=item["cause_key"], **values
                    )
                    session.add(row)
                    cause_by_key[key] = row
                    cause_inserted += 1
                    # flush 后才能拿到自增主键，映射阶段要用 id 建关联。
                    await session.flush()
                elif _cause_row_values(row) != values:
                    for field, value in values.items():
                        setattr(row, field, value)
                    cause_updated += 1
                cause_ids[item["cause_key"]] = row.id
            await session.flush()

            for entry in data["symptom_causes"]:
                symptom_id = symptom_ids[entry["symptom"]]
                for cause_key in entry["causes"]:
                    pair = (symptom_id, cause_ids[cause_key])
                    if pair not in mapping_pairs:
                        session.add(
                            SymptomCause(symptom_id=symptom_id, cause_id=pair[1])
                        )
                        mapping_pairs.add(pair)
                        mapping_inserted += 1

        await session.commit()

        totals = {}
        for label, model in (
            ("症状", FaultSymptom),
            ("原因", FaultCause),
            ("映射", SymptomCause),
        ):
            totals[label] = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        print(
            "知识库导入完成："
            f"症状 新增{symptom_inserted}/更新{symptom_updated}，"
            f"原因 新增{cause_inserted}/更新{cause_updated}，"
            f"映射 新增{mapping_inserted}；"
            f"库内总量 症状{totals['症状']} 原因{totals['原因']} 映射{totals['映射']}。"
        )


def main() -> None:
    asyncio.run(_seed())


if __name__ == "__main__":
    main()
