"""种子数据脚本幂等性测试，对应第 2 步检查点。"""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SEED_ROOM_NAMES = ("客厅", "卧室", "厨房", "书房", "走廊")
SEED_MEMBER_NAMES = ("小明", "小红")
SEED_DEVICE_NAMES = (
    "客厅灯",
    "卧室灯",
    "书房灯",
    "走廊灯",
    "客厅温湿度传感器",
    "卧室人体传感器",
    "卧室空调",
    "家庭网关",
)
SEED_DEVICE_TYPES = {"light", "sensor", "thermostat", "gateway"}


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


def _seed_device_types() -> set[str]:
    """返回种子家庭内设备类型去重集合（第 4 周 4 域覆盖口径）。"""
    conninfo = get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT d.device_type FROM devices d
              JOIN homes h ON h.id = d.home_id
              JOIN unnest(%s::text[]) AS want(name) ON want.name = d.name
            WHERE h.name = '演示之家'
            """,
            (list(SEED_DEVICE_NAMES),),
        )
        return {row[0] for row in cur.fetchall()}


def _latest_event(device_name: str) -> tuple[str, dict | None, datetime]:
    """返回设备最新事件 (event_type, payload, occurred_at)。

    排序口径与 MCP Server 状态推导一致：occurred_at DESC, id DESC。
    """
    conninfo = get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.event_type, e.payload, e.occurred_at
            FROM device_events e
              JOIN devices d ON d.id = e.device_id
              JOIN homes h ON h.id = d.home_id
            WHERE h.name = '演示之家' AND d.name = %s
            ORDER BY e.occurred_at DESC, e.id DESC
            LIMIT 1
            """,
            (device_name,),
        )
        row = cur.fetchone()
    assert row is not None, f"设备 {device_name} 无任何事件"
    return row[0], row[1], row[2]


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


def test_seed_devices_cover_four_types(db_ready) -> None:
    """种子设备覆盖 light / sensor / thermostat / gateway 四个设备域。"""
    _load_seed()
    types = _seed_device_types()
    assert types == SEED_DEVICE_TYPES, types


def test_seed_demo_anomaly_events(db_ready) -> None:
    """预置两条巡检演示异常：网关离线约 3 小时、温湿度传感器电量 8%。"""
    _load_seed()

    event_type, payload, occurred_at = _latest_event("家庭网关")
    assert event_type == "offline", (event_type, payload)
    assert payload == {"online": False}, payload
    # occurred_at 由脚本回拨 3 小时；窗口放宽到 2-4 小时避免执行延迟抖动。
    age_hours = (datetime.now(UTC) - occurred_at).total_seconds() / 3600
    assert 2 < age_hours < 4, f"网关离线事件距今 {age_hours:.1f} 小时，不符合预置"

    event_type, payload, _ = _latest_event("客厅温湿度传感器")
    assert event_type == "battery_low", (event_type, payload)
    assert payload == {"battery": 8}, payload
