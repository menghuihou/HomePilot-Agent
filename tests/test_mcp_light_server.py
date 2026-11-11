"""设备 MCP Server 测试：工具注册、按域状态推导、DB 状态读写与事件留痕。

使用 fastmcp 内存客户端直连 Server 对象；数据走真实本地库，
每个用例独立家庭/设备，结束清理。Server 引擎为 NullPool：
每次工具调用现连现断，天然规避 pytest 多事件循环的跨 loop 复用。
"""

import asyncio
import uuid

import psycopg
import pytest
from fastmcp import Client
from psycopg.types.json import Json

from app.config import get_settings
from mcp_servers import light_server


def _conninfo() -> str:
    return get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def _db_execute(query: str, params: tuple = (), fetch: bool = False):
    with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows


@pytest.fixture()
def seeded_home():
    """创建临时家庭；测试后按外键依赖顺序清理。"""
    home_id = _db_execute(
        "INSERT INTO homes (name, synthetic, source_type) "
        "VALUES (%s, TRUE, 'test') RETURNING id",
        (f"mcp-light-{uuid.uuid4().hex[:6]}",),
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
        async with Client(light_server.mcp) as client:
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


def test_registers_exactly_four_tools_with_risk_levels():
    async def _run():
        async with Client(light_server.mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_run())

    assert {t.name for t in tools} == {
        "get_device_status",
        "turn_light_on",
        "turn_light_off",
        "restart_gateway",
    }
    # 风险等级写入工具描述，随动态发现暴露给 LLM 做展示参考；
    # 审批门控的唯一依据是 repo 规则表，这里只锁定描述标注。
    expected_levels = {
        "get_device_status": "low",
        "turn_light_on": "low",
        "turn_light_off": "low",
        "restart_gateway": "high",
    }
    for tool in tools:
        assert tool.description
        assert f"风险等级：{expected_levels[tool.name]}" in tool.description


def test_get_device_status_derives_from_latest_event(seeded_home):
    device_id = _create_device(seeded_home, "客厅灯", "light")
    _append_sync_event(device_id, "power_on", {"brightness": 80})

    status = _call("get_device_status", {"device_id": str(device_id)})

    assert status["device_id"] == str(device_id)
    assert status["name"] == "客厅灯"
    assert status["online"] is True
    assert status["brightness"] == 80


def test_get_device_status_preserves_zero_brightness(seeded_home):
    """brightness=0 是有效状态，不能被默认值吞掉。"""
    device_id = _create_device(seeded_home, "客厅灯", "light")
    _append_sync_event(device_id, "power_on", {"brightness": 80})
    _append_sync_event(device_id, "power_on", {"brightness": 0})

    status = _call("get_device_status", {"device_id": str(device_id)})

    assert status["online"] is True
    assert status["brightness"] == 0


def test_get_device_status_without_events_is_offline(seeded_home):
    device_id = _create_device(seeded_home, "阳台灯", "light")

    status = _call("get_device_status", {"device_id": str(device_id)})

    assert status["online"] is False
    # 从未上报事件的设备统一返回最小离线状态，不携带任何域专属字段。
    assert status["last_seen_at"] is None
    assert "brightness" not in status


def test_turn_on_appends_power_on_event(seeded_home):
    device_id = _create_device(seeded_home, "客厅灯", "light")

    message = _call("turn_light_on", {"device_id": str(device_id)})

    assert "已打开" in message
    event_type, payload = _latest_event(device_id)
    assert event_type == "power_on"
    assert payload["brightness"] == 80


def test_turn_off_appends_power_off_event(seeded_home):
    device_id = _create_device(seeded_home, "客厅灯", "light")
    _append_sync_event(device_id, "power_on", {"brightness": 80})

    message = _call("turn_light_off", {"device_id": str(device_id)})

    assert "已关闭" in message
    event_type, payload = _latest_event(device_id)
    assert event_type == "power_off"
    assert payload["brightness"] == 0


def test_unknown_device_returns_explicit_error(seeded_home):
    status = _call("get_device_status", {"device_id": "999999"})
    assert "不存在" in status["error"]

    message = _call("turn_light_on", {"device_id": "999999"})
    assert "错误" in message


def test_turn_light_on_rejects_non_light_device(seeded_home):
    """控制类工具保持灯光域限制；只有只读状态查询泛化到全设备域。"""
    device_id = _create_device(seeded_home, "卧室空调", "thermostat")

    message = _call("turn_light_on", {"device_id": str(device_id)})

    assert "不是灯光设备" in message


def test_get_device_status_thermostat_reports_temperature(seeded_home):
    device_id = _create_device(seeded_home, "卧室空调", "thermostat")
    _append_sync_event(device_id, "power_on", {"target_temperature": 26})

    status = _call("get_device_status", {"device_id": str(device_id)})

    assert status["online"] is True
    assert status["target_temperature"] == 26
    assert status["temperature"] is None
    assert status["last_seen_at"]


def test_get_device_status_sensor_reports_battery(seeded_home):
    device_id = _create_device(seeded_home, "客厅温湿度传感器", "sensor")
    _append_sync_event(device_id, "battery_low", {"battery": 8})

    status = _call("get_device_status", {"device_id": str(device_id)})

    assert status["online"] is True
    assert status["battery"] == 8
    assert status["last_seen_at"]


def test_get_device_status_sensor_without_battery_omits_field(seeded_home):
    """电量未上报的传感器不带 battery 字段——供异常规则跳过低电量判定。"""
    device_id = _create_device(seeded_home, "卧室人体传感器", "sensor")
    _append_sync_event(device_id, "heartbeat", {"online": True})

    status = _call("get_device_status", {"device_id": str(device_id)})

    assert status["online"] is True
    assert "battery" not in status


def test_get_device_status_gateway_online_follows_payload(seeded_home):
    device_id = _create_device(seeded_home, "家庭网关", "gateway")

    # heartbeat 事件 payload online=True → 在线。
    _append_sync_event(device_id, "heartbeat", {"online": True})
    status = _call("get_device_status", {"device_id": str(device_id)})
    assert status["online"] is True
    assert status["last_seen_at"]

    # 后写入的 offline 事件成为最新事件：online 以 payload 为准 → 离线。
    _append_sync_event(device_id, "offline", {"online": False})
    status = _call("get_device_status", {"device_id": str(device_id)})
    assert status["online"] is False


def test_get_device_status_unknown_type_returns_minimal_status(seeded_home):
    """未建模的设备域只返回 online + last_seen_at，不编造域专属字段。"""
    device_id = _create_device(seeded_home, "客厅新风", "wind")
    _append_sync_event(device_id, "heartbeat", {"online": True})

    status = _call("get_device_status", {"device_id": str(device_id)})

    assert status["online"] is True
    assert status["last_seen_at"]
    assert "brightness" not in status
    assert "battery" not in status
    assert "target_temperature" not in status


def test_restart_gateway_appends_event(seeded_home):
    gateway_id = _create_device(seeded_home, "家庭网关", "gateway")

    message = _call("restart_gateway", {"gateway_id": str(gateway_id)})

    assert "重启成功" in message
    event_type, payload = _latest_event(gateway_id)
    assert event_type == "gateway_restart"
    assert payload["online"] is True


def test_restart_gateway_unknown_id_returns_explicit_error(seeded_home):
    message = _call("restart_gateway", {"gateway_id": "999999"})

    assert "不存在" in message
