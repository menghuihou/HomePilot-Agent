"""chat 子图与主图编排测试：偏好落库、覆盖、家庭隔离与直答分发。

LLM 全部用桩模型；数据库走真实本地库验证工具落库。
"""

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.chat as agent_chat
from app.agent.chat import build_agent_graph
from app.agent.router import RouterDecision
from app.config import get_settings


class StubRouterModel:
    """固定意图的结构化输出桩。"""

    def __init__(self, intent: str):
        self._decision = RouterDecision(intent=intent)  # type: ignore[arg-type]

    def with_structured_output(self, schema):
        decision = self._decision

        class _Structured:
            async def ainvoke(self, messages):
                return decision

        return _Structured()


class StubChatModel:
    """按脚本回放 AIMessage 的工具调用桩；脚本耗尽即报错，兼做未调用断言。"""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        return self._responses.pop(0)


def _tool_call_msg(home_id: int, pref_key: str, pref_value: str) -> AIMessage:
    """构造一次 record_preference 工具调用的模型输出。"""
    # home_id 由 InjectedState 注入，模型参数里只有键值。
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "record_preference",
                "args": {"pref_key": pref_key, "pref_value": pref_value},
                "id": "call_1",
            }
        ],
    )


def _run_graph(home_id: int, text: str, *, router_intent: str, chat_responses=None):
    """用独立引擎运行主图；结束后同循环销毁，避免连接池跨事件循环复用。"""
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    graph = build_agent_graph(
        router_model=StubRouterModel(router_intent),
        chat_model=StubChatModel(chat_responses) if chat_responses else None,
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_chat, "session_factory", session_maker)
    try:

        async def _run():
            try:
                return await graph.ainvoke(
                    {"messages": [HumanMessage(content=text)], "home_id": home_id},
                    config={"configurable": {"thread_id": f"test-{uuid.uuid4()}"}},
                )
            finally:
                await engine.dispose()

        return asyncio.run(_run())
    finally:
        monkeypatch.undo()


def test_chat_flow_records_device_alias(client, home_id):
    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    ).json()["id"]
    alias_key = f"device_alias:{device_id}"

    result = _run_graph(
        home_id,
        "把客厅灯叫主灯",
        router_intent="chat",
        chat_responses=[
            _tool_call_msg(home_id, alias_key, "主灯"),
            AIMessage(content="好的，已把客厅灯叫主灯。"),
        ],
    )

    assert result["answer"] == "好的，已把客厅灯叫主灯。"
    prefs = client.get(f"/homes/{home_id}/preferences").json()
    assert len(prefs) == 1
    assert prefs[0]["pref_key"] == alias_key
    assert prefs[0]["pref_value"] == "主灯"


def test_chat_flow_overwrites_same_alias(client, home_id):
    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    ).json()["id"]
    alias_key = f"device_alias:{device_id}"

    _run_graph(
        home_id,
        "把客厅灯叫主灯",
        router_intent="chat",
        chat_responses=[
            _tool_call_msg(home_id, alias_key, "主灯"),
            AIMessage(content="好的。"),
        ],
    )
    _run_graph(
        home_id,
        "把客厅灯叫主照明灯",
        router_intent="chat",
        chat_responses=[
            _tool_call_msg(home_id, alias_key, "主照明灯"),
            AIMessage(content="好的，已更新称呼。"),
        ],
    )

    prefs = client.get(f"/homes/{home_id}/preferences").json()
    assert len(prefs) == 1
    assert prefs[0]["pref_value"] == "主照明灯"


def test_chat_flow_isolated_by_home(client, home_id, other_home_id):
    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    ).json()["id"]

    _run_graph(
        home_id,
        "把客厅灯叫主灯",
        router_intent="chat",
        chat_responses=[
            _tool_call_msg(home_id, f"device_alias:{device_id}", "主灯"),
            AIMessage(content="好的。"),
        ],
    )

    other_prefs = client.get(f"/homes/{other_home_id}/preferences").json()
    assert other_prefs == []


def test_home_mode_intent_replies_directly(client, home_id):
    result = _run_graph(home_id, "现在是什么模式", router_intent="home_mode")

    assert result["answer"] == "当前家庭模式是：normal。"


def test_placeholder_intent_replies_directly(client, home_id):
    # chat_responses 为空：若被误派发到 chat 子图会立即报错。
    result = _run_graph(home_id, "把灯打开", router_intent="control")

    assert "后续版本上线" in result["answer"]
