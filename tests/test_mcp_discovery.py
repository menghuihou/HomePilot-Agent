"""MCP 动态发现测试：真实拉起 light_server 子进程的进程级用例。"""

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.chat as agent_chat
from app.agent.chat import build_agent_graph
from app.config import BASE_DIR, get_settings
from app.mcp.client import McpToolClient
from tests.test_agent_chat import StubChatModel, StubRouterModel


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


def test_discovers_three_light_tools():
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
    }
    assert all(t.description for t in tools)


def test_converted_tool_calls_server():
    mcp_client = _make_client()

    async def _run():
        try:
            tools = {t.name: t for t in await mcp_client.ensure_started()}
            result = await tools["turn_light_on"].ainvoke({"device_id": "light-3"})
            status = await tools["get_device_status"].ainvoke({"device_id": "light-3"})
            return result, status
        finally:
            await mcp_client.aclose()

    result, status = asyncio.run(_run())

    assert "已打开阳台灯" in result
    assert status["online"] is True
    assert status["brightness"] == 80


def test_agent_calls_turn_light_on_through_mcp(client, home_id):
    """桩模型发起 turn_light_on 调用，经真实 MCP Server 返回文本。"""
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
                            "args": {"device_id": "light-3"},
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
