"""灯光域 MCP Server 测试：工具注册、描述完整性与模拟行为。

使用 fastmcp 内存客户端直连 Server 对象，无需拉起子进程。
"""

import copy

import pytest
from fastmcp import Client

from mcp_servers import light_server


@pytest.fixture(autouse=True)
def _restore_lights():
    """每个用例结束后还原模拟设备表，避免用例间状态串扰。"""
    snapshot = copy.deepcopy(light_server._LIGHTS)
    yield
    light_server._LIGHTS.clear()
    light_server._LIGHTS.update(snapshot)


def _call(name: str, args: dict):
    async def _run():
        async with Client(light_server.mcp) as client:
            return (await client.call_tool(name, args)).data

    return asyncio_run(_run())


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_registers_exactly_three_tools_with_descriptions():
    async def _run():
        async with Client(light_server.mcp) as client:
            return await client.list_tools()

    tools = asyncio_run(_run())

    assert {t.name for t in tools} == {
        "get_device_status",
        "turn_light_on",
        "turn_light_off",
    }
    for tool in tools:
        # 工具描述会进入动态发现结果，供 LLM 选择工具，必须完整。
        assert tool.description
    assert "device_id" in tool.input_schema["properties"]


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
