"""MCP 统一域路由测试：状态入口与工具调用都不得绕过客户端抽象。"""

import pytest

import app.mcp.client as mcp_client


class StubTool:
    def __init__(self, name: str, result):
        self.name = name
        self.result = result
        self.calls = []

    async def ainvoke(self, arguments: dict):
        self.calls.append(arguments)
        return self.result


def test_get_domain_tools_rejects_unknown_domain():
    import asyncio

    with pytest.raises(ValueError, match="未接入的设备域"):
        asyncio.run(mcp_client.get_domain_tools("media"))


def test_fetch_status_routes_ac_to_ac_status_tool(monkeypatch):
    """ac 设备必须走专用状态工具，而不是 light server 的通用查询。"""
    import asyncio

    light_tool = StubTool("get_device_status", {"online": False})
    ac_tool = StubTool("get_ac_status", {"online": True, "power": True})

    async def get_light_tools():
        return [light_tool]

    async def get_ac_tools():
        return [ac_tool]

    monkeypatch.setitem(mcp_client._DOMAIN_CLIENT_GETTERS, "light", get_light_tools)
    monkeypatch.setitem(mcp_client._DOMAIN_CLIENT_GETTERS, "ac", get_ac_tools)

    status = asyncio.run(mcp_client.fetch_status_for_device("ac", 12))

    assert status == {"online": True, "power": True}
    assert ac_tool.calls == [{"device_id": "12"}]
    assert light_tool.calls == []


def test_fetch_status_routes_other_domains_to_generic_status(monkeypatch):
    import asyncio

    light_tool = StubTool("get_device_status", {"online": True})

    async def get_ac_tools():
        raise AssertionError("非 ac 设备不应启动空调 Server")

    async def get_light_tools():
        return [light_tool]

    monkeypatch.setitem(mcp_client._DOMAIN_CLIENT_GETTERS, "light", get_light_tools)
    monkeypatch.setitem(mcp_client._DOMAIN_CLIENT_GETTERS, "ac", get_ac_tools)

    status = asyncio.run(mcp_client.fetch_status_for_device("sensor", 7))

    assert status == {"online": True}
    assert light_tool.calls == [{"device_id": "7"}]


def test_call_domain_tool_reports_missing_tool(monkeypatch):
    import asyncio

    async def get_domain_tools(domain: str):
        return []

    monkeypatch.setattr(mcp_client, "get_domain_tools", get_domain_tools)

    with pytest.raises(LookupError, match="ac 域工具 set_ac_power 不可用"):
        asyncio.run(mcp_client.call_domain_tool("ac", "set_ac_power", {}))
