"""灯光域 MCP Server 测试：工具注册、描述完整性与模拟行为。

使用 fastmcp 内存客户端直连 Server 对象，无需拉起子进程。
"""

import copy

import pytest
from fastmcp import Client

from mcp_servers import light_server


@pytest.fixture(autouse=True)
def _restore_simulated_state():
    """每个用例结束后还原模拟设备与网关状态，避免用例间状态串扰。"""
    snapshot = copy.deepcopy(light_server._LIGHTS)
    gateway_snapshot = copy.deepcopy(light_server._GATEWAY)
    yield
    light_server._LIGHTS.clear()
    light_server._LIGHTS.update(snapshot)
    light_server._GATEWAY.clear()
    light_server._GATEWAY.update(gateway_snapshot)


def _call(name: str, args: dict):
    async def _run():
        async with Client(light_server.mcp) as client:
            return (await client.call_tool(name, args)).data

    return asyncio_run(_run())


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_registers_exactly_four_tools_with_risk_levels():
    async def _run():
        async with Client(light_server.mcp) as client:
            return await client.list_tools()

    tools = asyncio_run(_run())

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
        assert f"风险等级：{expected_levels[tool.name]}" in tool.description, tool.name


def test_get_device_status_returns_simulated_state():
    status = _call("get_device_status", {"device_id": "light-1"})

    assert status["device_id"] == "light-1"
    assert status["name"] == "客厅灯"
    assert status["online"] is True
    assert status["brightness"] == 80


def test_turn_on_sets_online_and_default_brightness():
    message = _call("turn_light_on", {"device_id": "light-2"})

    assert "已打开" in message
    status = _call("get_device_status", {"device_id": "light-2"})
    assert status["online"] is True
    assert status["brightness"] == 80


def test_turn_off_zeros_brightness():
    _call("turn_light_off", {"device_id": "light-1"})

    status = _call("get_device_status", {"device_id": "light-1"})
    assert status["online"] is False
    assert status["brightness"] == 0


def test_unknown_device_returns_explicit_error():
    status = _call("get_device_status", {"device_id": "nope"})

    assert "不存在" in status["error"]
    assert "light-1" in status["error"]  # 错误信息带可用设备提示

    message = _call("turn_light_on", {"device_id": "nope"})
    assert "错误" in message


def test_restart_gateway_success():
    message = _call("restart_gateway", {"gateway_id": "gateway-1"})

    assert "重启成功" in message
    assert light_server._GATEWAY["restarts"] == 1
    assert light_server._GATEWAY["online"] is True


def test_restart_gateway_unknown_id_returns_explicit_error():
    message = _call("restart_gateway", {"gateway_id": "nope"})

    assert "不存在" in message
    assert "gateway-1" in message
    assert light_server._GATEWAY["restarts"] == 0
