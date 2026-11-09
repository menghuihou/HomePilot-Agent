"""Router 测试：结构化意图判定、非法输出兜底与上下文注入。

全部使用桩模型，不依赖真实 LLM，保证测试确定性。
"""

import asyncio
from datetime import datetime

import pytest
from langchain_core.messages import HumanMessage

from app.agent.context import HomeContext
from app.agent.router import (
    RouterDecision,
    classify_intent,
    reply_for_intent,
    summarize_home_context,
)
from app.schemas import DeviceRead, HomeRead, MemberRead, PreferenceRead, RoomRead


def _make_context() -> HomeContext:
    """构造一份固定上下文：别名偏好 + 睡眠模式。"""
    now = datetime.now()
    base = {
        "synthetic": False,
        "source_type": "user",
        "created_at": now,
        "updated_at": now,
    }
    return HomeContext(
        home=HomeRead(id=1, name="测试之家", address=None, **base),
        rooms=[RoomRead(id=1, home_id=1, name="客厅", room_type=None, **base)],
        members=[MemberRead(id=1, home_id=1, name="小明", role="admin", **base)],
        devices=[
            DeviceRead(
                id=10,
                home_id=1,
                name="客厅灯",
                device_type="light",
                vendor=None,
                room_id=1,
                **base,
            )
        ],
        preferences=[
            PreferenceRead(
                id=1,
                home_id=1,
                member_id=None,
                pref_key="device_alias:10",
                pref_value="主灯",
                **base,
            )
        ],
        current_mode="sleep",
    )


class StubChatModel:
    """固定返回预设 RouterDecision 的桩模型，并捕获收到的消息供断言。"""

    def __init__(self, decision: RouterDecision):
        self.decision = decision
        self.received_messages: list = []

    def with_structured_output(self, schema):
        model = self

        class _Structured:
            async def ainvoke(self, messages):
                model.received_messages = list(messages)
                return model.decision

        return _Structured()


class BrokenChatModel:
    """模拟 LLM 输出非法 JSON / 网关异常的桩模型。"""

    def with_structured_output(self, schema):
        class _Structured:
            async def ainvoke(self, messages):
                raise ValueError("模拟结构化输出解析失败")

        return _Structured()


@pytest.mark.parametrize(
    ("utterance", "intent"),
    [
        ("把客厅灯叫主灯", "chat"),
        ("把客厅灯打开", "control"),
        ("客厅灯怎么不亮了", "diagnose"),
        ("帮我检查一下全屋设备", "inspect"),
        ("现在是什么模式", "home_mode"),
    ],
)
def test_classify_returns_model_decision(utterance, intent):
    model = StubChatModel(RouterDecision(intent=intent))

    assert (
        asyncio.run(
            classify_intent(
                [HumanMessage(content=utterance)], _make_context(), chat_model=model
            )
        )
        == intent
    )


def test_classify_falls_back_to_chat_on_invalid_output():
    assert (
        asyncio.run(
            classify_intent(
                [HumanMessage(content="你好")],
                _make_context(),
                chat_model=BrokenChatModel(),
            )
        )
        == "chat"
    )


class NoneOutputRouterModel:
    """模拟网关不调用结构化输出工具：ainvoke 返回 None 而非抛异常。"""

    def with_structured_output(self, schema):
        class _Structured:
            async def ainvoke(self, messages):
                return None

        return _Structured()


def test_router_none_output_falls_back_to_chat():
    """with_structured_output 返回 None 时兜底 chat。

    回归：曾直接取 None.intent 抛 AttributeError，Langfuse output 显示 undefined。
    """

    intent = asyncio.run(
        classify_intent(
            [HumanMessage(content="有没有成员叫小李的")],
            _make_context(),
            chat_model=NoneOutputRouterModel(),
        )
    )

    assert intent == "chat"


def test_router_none_output_retries_once_before_fallback():
    """首次 None（GLM 偶发不调结构化工具）重试一次：重试命中则正常路由。"""

    class FlakyThenOkRouterModel:
        """第一次返回 None，第二次返回固定决策。"""

        def __init__(self, decision: RouterDecision):
            self._decision = decision
            self.calls = 0

        def with_structured_output(self, schema):
            stub = self

            class _Structured:
                async def ainvoke(self, messages):
                    stub.calls += 1
                    return stub._decision if stub.calls >= 2 else None

            return _Structured()

    model = FlakyThenOkRouterModel(RouterDecision(intent="diagnose"))

    intent = asyncio.run(
        classify_intent(
            [HumanMessage(content="客厅灯不亮了")], _make_context(), chat_model=model
        )
    )

    assert intent == "diagnose"
    assert model.calls == 2


def test_router_merges_consecutive_humans_in_window():
    """窗口末尾连续 human 消息合并为一条，消除 GLM 不调工具的触发形态。"""
    model = StubChatModel(RouterDecision(intent="chat"))
    messages = [
        HumanMessage(content="hello，小p，可以帮我查询一下家里有多少成员吗"),
        HumanMessage(content="有没有成员叫小李的"),
    ]

    asyncio.run(classify_intent(messages, _make_context(), chat_model=model))

    sent = model.received_messages
    assert sent[0].type == "system"
    assert len(sent) == 2  # system + 合并后的单条 human
    assert sent[1].content == (
        "hello，小p，可以帮我查询一下家里有多少成员吗\n有没有成员叫小李的"
    )


def test_classify_injects_home_context_summary():
    model = StubChatModel(RouterDecision(intent="chat"))

    asyncio.run(
        classify_intent(
            [HumanMessage(content="把客厅灯叫主灯")], _make_context(), chat_model=model
        )
    )

    system_content = model.received_messages[0].content
    # 摘要要带上名称、别名和当前模式，路由器才能理解「主灯」指客厅灯。
    assert "测试之家" in system_content
    assert "device_alias:10=主灯" in system_content
    assert "sleep" in system_content
    # 设备列表带 id，路由器和 chat 子图才能把「主灯」解析到具体设备。
    assert "客厅灯(id=10)" in system_content


def test_summarize_home_context_hides_home_mode_key():
    summary = summarize_home_context(_make_context())

    assert "home_mode" not in summary
    assert "当前模式：sleep" in summary


def test_reply_for_home_mode_reads_current_mode():
    reply = reply_for_intent("home_mode", _make_context())

    assert reply == "当前家庭模式是：sleep。"


@pytest.mark.parametrize("intent", ["control", "inspect"])
def test_reply_for_unsupported_intents_are_placeholders(intent):
    reply = reply_for_intent(intent, _make_context())

    assert reply is not None
    assert "后续版本上线" in reply


def test_reply_for_chat_defers_to_chat_subgraph():
    assert reply_for_intent("chat", _make_context()) is None
