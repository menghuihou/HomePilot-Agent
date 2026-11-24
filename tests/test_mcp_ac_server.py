"""空调域 MCP Server 测试：工具注册、事件溯源状态推导、控制写路径与校验。

使用 fastmcp 内存客户端直连 Server 对象；数据走真实本地库，
每个用例独立家庭/设备，结束清理。对齐 test_mcp_light_server 风格。
"""

import asyncio
import uuid

import pytest
from fastmcp import Client
from psycopg.types.json import Json

from mcp_servers import ac_server


from tests.helpers import db_execute as _db_execute
@pytest.fixture()
def seeded_home():
    """创建临时家庭；测试后按外键依赖顺序清理。"""
    home_id = _db_execute(
        "INSERT INTO homes (name, synthetic, source_type) "
        "VALUES (%s, TRUE, 'test') RETURNING id",
        (f"mcp-ac-{uuid.uuid4().hex[:6]}",),
        fetch=True,
    )[0][0]
    yield home_id
    _db_execute(
        """
        DELETE FROM device_events
         WHERE device_id IN (SELECT id FROM devices WHERE home_id = %s)
        """,
        (home_id,),
    )
    _db_execute("DELETE FROM devices WHERE home_id = %s", (home_id,))
    _db_execute("DELETE FROM homes WHERE id = %s", (home_id,))


def _create_device(home_id: int, name: str, device_type: str) -> int:
    return _db_execute(
        """
        INSERT INTO devices (home_id, name, device_type, synthetic, source_type)
        VALUES (%s, %s, %s, TRUE, 'test') RETURNING id
        """,
        (home_id, name, device_type),
        fetch=True,
    )[0][0]


def _append_sync_event(device_id: int, event_type: str, payload: dict) -> None:
    _db_execute(
        """
        INSERT INTO device_events (device_id, event_type, payload,
                                   synthetic, source_type)
        VALUES (%s, %s, %s, TRUE, 'test')
        """,
        (device_id, event_type, Json(payload)),
    )


def _call(name: str, args: dict):
    async def _run():
        async with Client(ac_server.mcp) as client:
            return (await client.call_tool(name, args)).data

    return asyncio.run(_run())


def _latest_event(device_id: int) -> tuple[str, dict | None]:
    rows = _db_execute(
        """
        SELECT event_type, payload FROM device_events
         WHERE device_id = %s ORDER BY occurred_at DESC, id DESC LIMIT 1
        """,
        (device_id,),
        fetch=True,
    )
    return (rows[0][0], rows[0][1]) if rows else (None, None)


def test_registers_exactly_three_tools_with_risk_levels(seeded_home):
    """工具注册完整：3 个工具描述都必须带「风险等级」。"""

    async def _run():
        async with Client(ac_server.mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_run())

    assert {t.name for t in tools} == {
        "get_ac_status",
        "set_temperature",
        "set_ac_power",
    }
    for tool in tools:
        assert tool.description
        assert "风险等级" in tool.description


def test_get_ac_status_without_events_is_offline(seeded_home):
    device_id = _create_device(seeded_home, "卧室空调", "ac")

    status = _call("get_ac_status", {"device_id": str(device_id)})

    assert status["online"] is False
    assert status["power"] is None
    assert status["temperature"] is None


def test_get_ac_status_derives_power_from_latest_event(seeded_home):
    """状态 = 最新事件推导：power_on 快照携带开关与温度。"""
    device_id = _create_device(seeded_home, "客厅空调", "ac")
    _append_sync_event(device_id, "power_on", {"power": True, "temperature": 26})

    status = _call("get_ac_status", {"device_id": str(device_id)})

    assert status["online"] is True
    assert status["power"] is True
    assert status["temperature"] == 26


def test_set_temperature_updates_and_validates_range(seeded_home):
    """调温写 state_change 事件（快照携带 power）；越界返回结构化错误。"""
    device_id = _create_device(seeded_home, "客厅空调", "ac")
    _append_sync_event(device_id, "power_on", {"power": True, "temperature": 26})

    result = _call("set_temperature", {"device_id": str(device_id), "temperature": 24})
    assert "24" in result["message"]

    event_type, payload = _latest_event(device_id)
    assert event_type == "state_change"
    assert payload["power"] is True  # 调温不改开关，快照携带当前开关
    assert payload["temperature"] == 24

    for out_of_range in (15, 31):
        blocked = _call(
            "set_temperature",
            {"device_id": str(device_id), "temperature": out_of_range},
        )
        assert "超出范围" in blocked["error"]
    # 越界调用不落事件
    event_type, _ = _latest_event(device_id)
    assert event_type == "state_change"


def test_set_temperature_on_unknown_or_non_ac_device(seeded_home):
    light_id = _create_device(seeded_home, "客厅灯", "light")

    blocked = _call("set_temperature", {"device_id": str(light_id), "temperature": 24})
    assert "不是空调设备" in blocked["error"]

    unknown = _call("set_temperature", {"device_id": "999999", "temperature": 24})
    assert "不存在" in unknown["error"]


def test_set_ac_power_roundtrip(seeded_home):
    """开关往返：关机 → 开机（保持当前温度设定）。"""
    device_id = _create_device(seeded_home, "客厅空调", "ac")
    _append_sync_event(device_id, "power_on", {"power": True, "temperature": 26})

    off = _call("set_ac_power", {"device_id": str(device_id), "on": False})
    assert off["power"] is False
    event_type, payload = _latest_event(device_id)
    assert event_type == "power_off"
    assert payload["power"] is False

    on = _call("set_ac_power", {"device_id": str(device_id), "on": True})
    assert on["power"] is True
    # 开机未带温度：保持当前设定（26）
    assert _latest_event(device_id)[1]["temperature"] == 26


def test_set_ac_power_on_with_temperature(seeded_home):
    """开机同时设定温度：单条事件携带完整快照。"""
    device_id = _create_device(seeded_home, "书房空调", "ac")
    result = _call(
        "set_ac_power",
        {"device_id": str(device_id), "on": True, "temperature": 27},
    )

    assert result["power"] is True
    event_type, payload = _latest_event(device_id)
    assert event_type == "power_on"
    assert payload == {"power": True, "temperature": 27.0}


def test_set_temperature_rejects_out_of_range_before_device_lookup(seeded_home):
    """越界校验前置：非法 id + 越界温度也返回温度错误（入参校验优先）。"""
    blocked = _call("set_temperature", {"device_id": "abc", "temperature": 35})
    assert "超出范围" in blocked["error"]
