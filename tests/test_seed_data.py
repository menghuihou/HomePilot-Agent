"""种子数据脚本幂等性测试，对应第 8 步检查点。"""

import subprocess
import sys
from pathlib import Path

import psycopg

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SEED_ROOM_NAMES = ("客厅", "卧室", "厨房")
SEED_MEMBER_NAMES = ("小明", "小红")
SEED_DEVICE_NAMES = ("客厅灯", "卧室空调", "家庭网关")


def _load_seed() -> subprocess.CompletedProcess[str]:
    """以子进程方式执行真实种子命令，覆盖文档中的调用路径。"""
    return subprocess.run(
        [sys.executable, "-m", "db.seeds.load_family"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=60,
        check=True,
    )


def _seed_entity_counts() -> dict[str, int]:
    """返回种子家庭内各核心条目的出现次数（按名称聚合）。"""
    conninfo = get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    counts: dict[str, int] = {}
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        for label, table, names in (
            ("房间", "rooms", SEED_ROOM_NAMES),
            ("成员", "members", SEED_MEMBER_NAMES),
            ("设备", "devices", SEED_DEVICE_NAMES),
        ):
            for name in names:
                cur.execute(
                    f"""
                    SELECT count(*) FROM {table} t
                      JOIN homes h ON h.id = t.home_id
                    WHERE h.name = '演示之家' AND t.name = %s
                    """,
                    (name,),
                )
                counts[f"{label}-{name}"] = int(cur.fetchone()[0])
    return counts


def test_seed_data_is_idempotent(db_ready) -> None:
    """连续执行两次种子脚本：种子条目各自唯一，且数量不再增长。

    演示之家下允许存在页面操作产生的额外设备，因此只断言
    「种子条目唯一」与「两次执行后数量不变」，不锁定子树总数。
    """
    _load_seed()
    before = _seed_entity_counts()
    assert all(count == 1 for count in before.values()), before

    second = _load_seed()
    assert "跳过" in second.stdout, second.stdout

    after = _seed_entity_counts()
    assert after == before, (before, after)
