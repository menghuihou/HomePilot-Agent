"""故障知识库种子数据测试，对应第 3 周第 2 步检查点。"""

import json
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "db" / "seeds" / "data"
DOMAIN_FILES = sorted(DATA_DIR.glob("*_knowledge.json"))
EXPECTED_DEVICE_TYPES = {"light", "ac", "sensor", "gateway"}
# MVP 口径：故障原因知识条目总量 200-300 条，覆盖四个设备域。
CAUSE_TOTAL_RANGE = (200, 300)


def _load_knowledge() -> subprocess.CompletedProcess[str]:
    """以子进程方式执行真实导入命令，覆盖文档中的调用路径。"""
    return subprocess.run(
        [sys.executable, "-m", "db.seeds.load_fault_knowledge"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=120,
        check=True,
    )


def _knowledge_counts() -> dict[str, int]:
    """返回知识库三张表总量与按设备域分组的原因数量。"""
    conninfo = get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    counts: dict[str, int] = {}
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        for table in ("fault_symptoms", "fault_causes", "symptom_causes"):
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = int(cur.fetchone()[0])
        cur.execute(
            "SELECT device_type, count(*) FROM fault_causes GROUP BY device_type"
        )
        for device_type, total in cur.fetchall():
            counts[f"causes/{device_type}"] = int(total)
    return counts


def test_domain_files_cover_four_types() -> None:
    """四个设备域各有一个数据文件，文件数与域集合一一对应。"""
    device_types = {
        json.loads(path.read_text())["device_type"] for path in DOMAIN_FILES
    }
    assert device_types == EXPECTED_DEVICE_TYPES
    assert len(DOMAIN_FILES) == len(EXPECTED_DEVICE_TYPES)


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=lambda path: path.name)
def test_domain_json_hygiene(path: Path) -> None:
    """单域数据：key 唯一、映射引用完整、无孤儿原因、取值在合法范围。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    symptom_keys = [item["symptom_key"] for item in data["symptoms"]]
    cause_keys = [item["cause_key"] for item in data["causes"]]
    assert len(symptom_keys) == len(set(symptom_keys))
    assert len(cause_keys) == len(set(cause_keys))

    mapped: set[str] = set()
    for entry in data["symptom_causes"]:
        assert entry["symptom"] in symptom_keys, entry
        for cause_key in entry["causes"]:
            assert cause_key in cause_keys, (entry["symptom"], cause_key)
            mapped.add(cause_key)
    orphans = set(cause_keys) - mapped
    assert not orphans, sorted(orphans)

    for cause in data["causes"]:
        assert 0 < cause["prevalence"] <= 1, cause["cause_key"]
        assert 1 <= cause["investigation_weight"] <= 5, cause["cause_key"]


def test_seed_import_is_idempotent(db_ready) -> None:
    """连续导入两次：第二次零新增零更新，三张表数量保持不变。"""
    _load_knowledge()
    before = _knowledge_counts()

    second = _load_knowledge()
    assert "新增0/更新0" in second.stdout, second.stdout

    after = _knowledge_counts()
    assert after == before, (before, after)


def test_knowledge_totals_within_mvp_range(db_ready) -> None:
    """原因总量落在 200-300 条，且四个设备域都有数据、映射无悬空外键。"""
    _load_knowledge()
    counts = _knowledge_counts()
    assert CAUSE_TOTAL_RANGE[0] <= counts["fault_causes"] <= CAUSE_TOTAL_RANGE[1]

    grouped = {key.split("/")[1] for key in counts if key.startswith("causes/")}
    assert grouped == EXPECTED_DEVICE_TYPES

    conninfo = get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM symptom_causes sc
             WHERE NOT EXISTS (
                   SELECT 1 FROM fault_symptoms fs WHERE fs.id = sc.symptom_id)
                OR NOT EXISTS (
                   SELECT 1 FROM fault_causes fc WHERE fc.id = sc.cause_id)
            """
        )
        dangling = int(cur.fetchone()[0])
    assert dangling == 0
