"""chat 子图与主图编排测试：偏好落库、覆盖、家庭隔离与直答分发。

LLM 全部用桩模型；数据库走真实本地库验证工具落库。
"""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.chat as agent_chat
import app.agent.inspect as inspect_module
from app.agent.chat import build_agent_graph, build_chat_graph, record_preference
from app.agent.inspect import build_inspect_graph
from app.agent.router import RouterDecision, classify_intent
from app.config import get_settings


class StubRouterModel:
    """固定意图的结构化输出桩。

    received_messages 记录收到的 prompt 消息列表，供窗口切片断言使用。
    """

    def __init__(self, intent: str):
        self._decision = RouterDecision(intent=intent)  # type: ignore[arg-type]
        self.received_messages: list[list] = []

    def with_structured_output(self, schema):
        decision = self._decision
        stub = self

        class _Structured:
            async def ainvoke(self, messages):
                stub.received_messages.append(list(messages))
                return decision

        return _Structured()


class StubChatModel:
    """按脚本回放 AIMessage 的工具调用桩；脚本耗尽即报错，兼做未调用断言。

    received_messages 记录每轮收到的消息列表，供多轮上下文断言使用。
    """

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)
        self.received_messages: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        self.received_messages.append(list(messages))
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


def test_chat_subgraph_does_not_own_memory_checkpointer():
    """chat 作为主图节点时继承主图持久化，避免进程内历史无界增长。"""
    assert build_chat_graph().checkpointer is None


def test_record_preference_declares_state_injection():
    """状态注入由元数据声明，且字段不暴露给 LLM 的 tool_call schema。"""
    assert record_preference.metadata == {"inject_from_state": ("home_id",)}
    assert "home_id" not in record_preference.tool_call_schema.model_fields
    assert "home_id" in record_preference.args_schema.model_fields


def test_injected_home_id_overrides_model_supplied_value(client, home_id):
    """手写工具节点执行元数据声明：图状态始终覆盖模型伪造的 home_id。"""
    result = _run_graph(
        home_id,
        "记住我偏好暗色模式",
        router_intent="chat",
        chat_responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "record_preference",
                        "args": {
                            "pref_key": "theme",
                            "pref_value": "dark",
                            "home_id": 999999,
                        },
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="已记录偏好。"),
        ],
    )

    prefs = client.get(f"/homes/{home_id}/preferences").json()
    assert result["answer"] == "已记录偏好。"
    assert [(p["pref_key"], p["pref_value"]) for p in prefs] == [("theme", "dark")]


def _run_graph(
    home_id: int,
    text: str,
    *,
    router_intent: str,
    chat_responses=None,
    inspect_graph=None,
):
    """用独立引擎运行主图；结束后同循环销毁，避免连接池跨事件循环复用。"""
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    graph = build_agent_graph(
        router_model=StubRouterModel(router_intent),
        chat_model=StubChatModel(chat_responses) if chat_responses else None,
        inspect_graph=inspect_graph,
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_chat, "session_factory", session_maker)
    # 巡检子图有独立的模块级 session_factory，须一并替换（跨事件循环约定）。
    monkeypatch.setattr(inspect_module, "session_factory", session_maker)
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
    # 直答路径也要落 AI 消息，保持历史 human/ai 交替
    assert any(getattr(m, "type", "") == "ai" for m in result["messages"])


def test_placeholder_intent_replies_directly(client, home_id):
    # chat_responses 为空：若被误派发到 chat 子图会立即报错。
    result = _run_graph(home_id, "把灯打开", router_intent="control")

    assert "后续版本上线" in result["answer"]


def test_inspect_intent_routes_into_inspection_subgraph(client, home_id):
    """inspect 意图进巡检子图：答案为摘要，inspect_result 携带落库报告 id。"""
    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    ).json()["id"]
    recent = datetime.now(UTC).isoformat()
    statuses = {device_id: {"online": True, "last_seen_at": recent}}

    async def fetcher(device: dict) -> dict:
        return dict(statuses[device["id"]])

    result = _run_graph(
        home_id,
        "帮我看看家里设备都正常吗",
        router_intent="inspect",
        inspect_graph=build_inspect_graph(status_fetcher=fetcher),
    )

    assert result["intent"] == "inspect"
    assert "检查了 1 台设备" in result["answer"]
    assert result["inspect_result"]["report_id"] > 0
    assert result["inspect_result"]["total_devices"] == 1


def _stub_home_context() -> SimpleNamespace:
    """classify_intent 单测的最小上下文：只补 summarize 用到的属性。"""
    return SimpleNamespace(
        home=SimpleNamespace(name="测试家"),
        current_mode="normal",
        rooms=[],
        members=[],
        devices=[],
        preferences=[],
    )


def test_router_window_sends_only_recent_dialog():
    """窗口截断只发最近 6 条有正文的 human/ai 消息，过滤工具消息痕迹。

    ToolMessage 与空正文 AI 消息进入窗口开头会被 Anthropic 协议拒绝，
    导致 Router 静默降级 chat。
    """
    stub = StubRouterModel("chat")
    messages = [HumanMessage(content=f"消息{i}") for i in range(8)]
    # 在历史中间插入一轮工具调用痕迹，验证窗口过滤。
    messages.insert(2, AIMessage(content="", tool_calls=[]))
    messages.insert(3, ToolMessage(content="已记录偏好", tool_call_id="call_1"))

    intent = asyncio.run(
        classify_intent(messages, _stub_home_context(), chat_model=stub)
    )

    assert intent == "chat"
    sent = stub.received_messages[0]
    assert sent[0].type == "system"
    dialog = sent[1:]
    # 连续 human 消息合并为一条转写文本（消除网关不调工具的触发形态）
    assert len(dialog) == 1
    assert all(getattr(m, "type", "") in ("human", "ai") for m in dialog)
    for i in range(2, 8):
        assert f"消息{i}" in dialog[0].content


def test_router_without_human_message_falls_back_to_chat():
    """历史里没有用户消息时不调 LLM，直接兜底 chat。"""
    stub = StubRouterModel("control")

    intent = asyncio.run(
        classify_intent(
            [AIMessage(content="早上好")], _stub_home_context(), chat_model=stub
        )
    )

    assert intent == "chat"
    assert stub.received_messages == []


def _insert_pending_approval(home_id: int, action: str = "restart_gateway") -> int:
    """直接落一条 pending 审批（decision 为空），供日报待确认清单测试。"""
    conninfo = get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO approvals (thread_id, home_id, action, risk_level, decision,
                                   synthetic, source_type)
            VALUES (%s, %s, %s, 'high', NULL, TRUE, 'test') RETURNING id
            """,
            (f"dx-{uuid.uuid4().hex[:8]}", home_id, action),
        )
        approval_id = cur.fetchone()[0]
        conn.commit()
        return approval_id


def test_inspect_daily_report_phrasing_includes_pending_approvals(client, home_id):
    """日报话术（「家庭报告」）→ daily_report 模式：报告含待确认审批清单。"""
    client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    )
    _insert_pending_approval(home_id, "restart_gateway")
    recent = datetime.now(UTC).isoformat()
    statuses = {1: {"online": True, "last_seen_at": recent}}

    async def fetcher(device: dict) -> dict:
        return dict(
            statuses.get(device["id"], {"online": True, "last_seen_at": recent})
        )

    result = _run_graph(
        home_id,
        "生成一份家庭报告",
        router_intent="inspect",
        inspect_graph=build_inspect_graph(status_fetcher=fetcher),
    )

    assert result["report_type"] == "daily_report"
    assert result["answer"].startswith("家庭日报：")
    assert "待确认操作：restart_gateway" in result["answer"]
    assert "需要处理吗？" in result["answer"]


def test_inspect_inspection_phrasing_stays_plain(client, home_id):
    """纯巡检话术 → inspection 模式：不含日报前缀与待确认段落。"""
    client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    )
    _insert_pending_approval(home_id, "restart_gateway")
    recent = datetime.now(UTC).isoformat()

    async def fetcher(device: dict) -> dict:
        return {"online": True, "last_seen_at": recent}

    result = _run_graph(
        home_id,
        "帮我看看家里设备都正常吗",
        router_intent="inspect",
        inspect_graph=build_inspect_graph(status_fetcher=fetcher),
    )

    assert result["report_type"] == "inspection"
    assert result["answer"].startswith("检查了 1 台设备")
    assert "待确认操作" not in result["answer"]
