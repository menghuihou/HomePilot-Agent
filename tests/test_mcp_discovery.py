"""MCP 动态发现测试：真实拉起 light_server 子进程的进程级用例。"""

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.chat as agent_chat
from app.agent.chat import build_agent_graph
from app.config import BASE_DIR, get_settings
from app.mcp.client import McpToolClient, _schema_to_args_model
from tests.test_agent_chat import StubChatModel, StubRouterModel
from tests.test_agent_diagnose import _create_device


def _make_client() -> McpToolClient:
    """每个用例独立客户端实例：会话绑定事件循环，不能跨用例复用。"""
    return McpToolClient(
        command="uv",
        args=["run", "python", "mcp_servers/light_server.py"],
        cwd=BASE_DIR,
    )


def _run_agent(graph, home_id: int, text: str, on_finish=None):
    """独立引擎运行主图：隔离连接池，结束后同循环销毁，避免跨事件循环复用。"""
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    patch = pytest.MonkeyPatch()
    patch.setattr(agent_chat, "session_factory", session_maker)
    try:

        async def _run():
            try:
                result = await graph.ainvoke(
                    {
                        "messages": [HumanMessage(content=text)],
                        "home_id": home_id,
                    },
                    config={"configurable": {"thread_id": f"mcp-{uuid.uuid4()}"}},
                )
                if on_finish is not None:
                    await on_finish()
                return result
            finally:
                await engine.dispose()

        return asyncio.run(_run())
    finally:
        patch.undo()


def test_discovers_all_light_domain_tools():
    mcp_client = _make_client()

    async def _run():
        try:
            return await mcp_client.ensure_started()
        finally:
            await mcp_client.aclose()

    tools = asyncio.run(_run())

    assert {t.name for t in tools} == {
        "get_device_status",
        "turn_light_on",
        "turn_light_off",
        "restart_gateway",
    }
    assert all(t.description for t in tools)

    # 审批演示主路径：诊断子图在审批通过后调用的高风险网关工具
    # 必须能被动态发现。
    gateway_tool = next(t for t in tools if t.name == "restart_gateway")
    assert "风险等级：high" in gateway_tool.description


def test_converted_tool_calls_server(client, home_id):
    """MCP 转换工具按业务库 devices.id 查询真实设备。"""
    device_id = _create_device(client, home_id, "阳台灯", "light")
    mcp_client = _make_client()

    async def _run():
        try:
            tools = {t.name: t for t in await mcp_client.ensure_started()}
            result = await tools["turn_light_on"].ainvoke({"device_id": str(device_id)})
            status = await tools["get_device_status"].ainvoke(
                {"device_id": str(device_id)}
            )
            return result, status
        finally:
            await mcp_client.aclose()

    result, status = asyncio.run(_run())

    assert "已打开阳台灯" in result
    assert status["online"] is True
    assert status["brightness"] == 80


def test_agent_calls_turn_light_on_through_mcp(client, home_id):
    """桩模型发起 turn_light_on 调用，经真实 MCP Server 返回文本。"""
    device_id = _create_device(client, home_id, "阳台灯", "light")
    mcp_client = _make_client()

    async def provider():
        return await mcp_client.ensure_started()

    graph = build_agent_graph(
        router_model=StubRouterModel("chat"),
        chat_model=StubChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "turn_light_on",
                            "args": {"device_id": str(device_id)},
                            "id": "call_1",
                        }
                    ],
                ),
                AIMessage(content="已为你打开阳台灯。"),
            ]
        ),
        extra_tools_provider=provider,
    )

    result = _run_agent(graph, home_id, "打开阳台灯", on_finish=mcp_client.aclose)

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any("已打开阳台灯" in str(m.content) for m in tool_messages)
    assert result["answer"] == "已为你打开阳台灯。"


def test_tool_failure_returns_error_message_to_model(client, home_id):
    """Server 崩溃时错误文本回给模型，对话不中断（本周不做自动重连）。"""
    mcp_client = _make_client()
    holder: dict = {}

    async def provider():
        tools = await mcp_client.ensure_started()
        holder["tools"] = tools
        # 首次调用后立即关闭会话，模拟 Server 崩溃：后续拿到的是失效工具
        await mcp_client.aclose()
        return holder["tools"]

    graph = build_agent_graph(
        router_model=StubRouterModel("chat"),
        chat_model=StubChatModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_device_status",
                            "args": {"device_id": "light-1"},
                            "id": "call_1",
                        }
                    ],
                ),
                AIMessage(content="设备状态暂时查不到，稍后再试。"),
            ]
        ),
        extra_tools_provider=provider,
    )

    result = _run_agent(graph, home_id, "查一下客厅灯状态", on_finish=mcp_client.aclose)

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any("工具调用失败" in str(m.content) for m in tool_messages)
    # 失败后对话仍然收口
    assert result["answer"] == "设备状态暂时查不到，稍后再试。"


def test_schema_args_model_supports_anyof_optional_int():
    """可选参数 anyOf（int | null）→ int | None 字段（夜间调光依赖）。

    回归：anyOf 形态曾被兜底成 str，int 传参触发 ValidationError，
    set_light_brightness 永远「工具暂不可用」。
    """
    model = _schema_to_args_model(
        "turn_light_on_args",
        {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "brightness": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "default": None,
                },
            },
            "required": ["device_id"],
        },
    )

    fields = model.model_fields
    assert "brightness" in fields
    instance = model(device_id="1", brightness=20)
    assert instance.brightness == 20
    assert model(device_id="1", brightness=None).brightness is None


def test_concurrent_ensure_started_spawns_server_once(monkeypatch):
    """并发冷启动竞态回归：多协程同时 ensure_started 只拉起一个 Server。

    无锁时 _client 的赋值在 enter_async_context（长耗时 await）之后，
    并发调用各自通过空检查、各自拉起子进程并覆盖单例引用（实测 10 并发
    → 10 个 Server 实例，aclose 后泄漏存活）。锁 + 锁内二次检查修复。
    """
    import app.mcp.client as client_module

    mcp_client = _make_client()
    transports: list[object] = []
    real_transport = client_module.StdioTransport

    def _counting_transport(*args, **kwargs):
        transports.append(real_transport(*args, **kwargs))
        return transports[-1]

    monkeypatch.setattr(client_module, "StdioTransport", _counting_transport)

    async def _run():
        try:
            results = await asyncio.gather(
                *(mcp_client.ensure_started() for _ in range(10))
            )
            return results
        finally:
            await mcp_client.aclose()

    results = asyncio.run(_run())

    # 只构造一个 transport（一个子进程）；全部调用拿到同一份缓存工具。
    assert len(transports) == 1
    assert all(result == results[0] for result in results)
    # aclose 语义：关闭后重置连接状态，下次 ensure_started 重新拉起。
    assert mcp_client._client is None
