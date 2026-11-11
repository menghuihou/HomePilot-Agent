"""chat 接口测试：参数校验 422、家庭不存在 404、同步回答与 SSE 事件流。"""

import json
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.agent.chat import build_agent_graph
from app.agent.inspect import build_inspect_graph
from app.main import application
from app.routers import chat as chat_api
from tests.test_agent_chat import StubChatModel, StubRouterModel


@pytest.fixture()
def stub_graph(client):
    """用桩图覆盖 Agent 依赖：不触发真实 LLM。"""
    graph = build_agent_graph(
        router_model=StubRouterModel("chat"),
        chat_model=StubChatModel([AIMessage(content="你好，我是家庭助手。")]),
    )
    application.dependency_overrides[chat_api.get_agent_graph] = lambda: graph
    yield graph
    application.dependency_overrides.pop(chat_api.get_agent_graph, None)


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


def test_chat_requires_home_id(client):
    # 参数校验在进入业务代码前完成，天然不会触发任何 LLM 调用。
    resp = client.post("/chat", json={"message": "你好"})

    assert resp.status_code == 422
    assert any(err["loc"][-1] == "home_id" for err in resp.json()["detail"])


def test_chat_rejects_blank_message(client):
    resp = client.post("/chat", json={"home_id": 1, "message": "   "})

    assert resp.status_code == 422


def test_chat_home_not_found(client):
    resp = client.post("/chat", json={"home_id": 999999, "message": "你好"})

    assert resp.status_code == 404
    assert resp.json()["detail"] == "home not found"


def test_chat_returns_answer_intent_and_thread_id(client, stub_graph, home_id):
    resp = client.post("/chat", json={"home_id": home_id, "message": "你好"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "你好，我是家庭助手。"
    assert body["intent"] == "chat"
    assert body["thread_id"]
    # trace_id 尽力而为：Langfuse 上下文可用时非空
    assert "trace_id" in body


def test_chat_interrupt_returns_guidance_not_500(client, home_id):
    """同步接口命中审批挂起：返回引导话术，而非 KeyError 500。"""

    class InterruptStubGraph:
        """复现审批挂起形态：to_answer 未跑，结果只有 __interrupt__。"""

        async def ainvoke(self, state, config=None):
            return {
                "intent": "diagnose",
                "__interrupt__": (
                    SimpleNamespace(
                        value={
                            "action": "restart_gateway",
                            "risk_level": "high",
                        }
                    ),
                ),
            }

    application.dependency_overrides[chat_api.get_agent_graph] = lambda: (
        InterruptStubGraph()
    )
    try:
        resp = client.post("/chat", json={"home_id": home_id, "message": "把灯打开"})
    finally:
        application.dependency_overrides.pop(chat_api.get_agent_graph, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "diagnose"
    assert "流式对话" in body["answer"]


def test_chat_echoes_existing_thread_id(client, stub_graph, home_id):
    resp = client.post(
        "/chat",
        json={"home_id": home_id, "message": "你好", "thread_id": "t-123"},
    )

    assert resp.json()["thread_id"] == "t-123"


def test_chat_stream_emits_meta_and_done(client, stub_graph, home_id):
    with client.stream(
        "POST",
        "/chat/stream",
        json={"home_id": home_id, "message": "你好", "thread_id": "t-abc"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())

    events = _parse_sse(text)

    assert events[0]["event"] == "meta"
    assert events[0]["thread_id"] == "t-abc"
    assert events[-1]["event"] == "done"
    assert events[-1]["answer"] == "你好，我是家庭助手。"


def test_chat_stream_home_not_found_before_stream(client):
    """校验在建流之前：404 是普通 JSON，不是 event-stream。"""
    resp = client.post("/chat/stream", json={"home_id": 999999, "message": "你好"})

    assert resp.status_code == 404
    assert "text/event-stream" not in resp.headers.get("content-type", "")


def test_chat_stream_rejects_blank_message(client):
    resp = client.post("/chat/stream", json={"home_id": 1, "message": "   "})

    assert resp.status_code == 422


def test_chat_direct_reply_home_mode(client, home_id):
    graph = build_agent_graph(
        router_model=StubRouterModel("home_mode"),
        chat_model=StubChatModel([]),
    )
    application.dependency_overrides[chat_api.get_agent_graph] = lambda: graph
    try:
        resp = client.post(
            "/chat", json={"home_id": home_id, "message": "现在是什么模式"}
        )
    finally:
        application.dependency_overrides.pop(chat_api.get_agent_graph, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "home_mode"
    assert body["answer"] == "当前家庭模式是：normal。"


def test_chat_multi_turn_accumulates_history(client, home_id):
    """同一 thread_id 连发两轮：第二轮模型收到首轮问答，多轮上下文生效。"""
    chat_model = StubChatModel(
        [AIMessage(content="第一轮回复"), AIMessage(content="第二轮回复")]
    )
    graph = build_agent_graph(
        router_model=StubRouterModel("chat"),
        chat_model=chat_model,
    )
    application.dependency_overrides[chat_api.get_agent_graph] = lambda: graph
    try:
        thread_id = f"mt-{uuid.uuid4().hex}"
        r1 = client.post(
            "/chat",
            json={"home_id": home_id, "message": "第一句话", "thread_id": thread_id},
        )
        r2 = client.post(
            "/chat",
            json={"home_id": home_id, "message": "第二句话", "thread_id": thread_id},
        )
    finally:
        application.dependency_overrides.pop(chat_api.get_agent_graph, None)

    assert r1.json()["answer"] == "第一轮回复"
    assert r2.json()["answer"] == "第二轮回复"
    # 首轮：system + 用户消息；次轮：system + 首轮问答 + 新用户消息
    assert len(chat_model.received_messages[0]) == 2
    assert len(chat_model.received_messages[1]) == 4


def test_chat_stream_direct_reply_path(client, home_id):
    """home_mode 等直答路径的 done 事件必须带完整答案（回归：曾漏 direct_reply）。"""
    graph = build_agent_graph(
        router_model=StubRouterModel("home_mode"),
        chat_model=StubChatModel([]),
    )
    application.dependency_overrides[chat_api.get_agent_graph] = lambda: graph
    try:
        with client.stream(
            "POST",
            "/chat/stream",
            json={"home_id": home_id, "message": "现在是什么模式"},
        ) as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())
    finally:
        application.dependency_overrides.pop(chat_api.get_agent_graph, None)

    events = _parse_sse(text)

    assert events[-1]["event"] == "done"
    assert events[-1]["answer"] == "当前家庭模式是：normal。"


def test_chat_stream_emits_inspect_events(client, home_id):
    """inspect 意图：SSE 流依次出现 inspect_progress 与 inspect_result 事件。"""
    from datetime import UTC, datetime

    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    ).json()["id"]
    recent = datetime.now(UTC).isoformat()
    statuses = {device_id: {"online": True, "last_seen_at": recent}}

    async def fetcher(device: dict) -> dict:
        return dict(statuses[device["id"]])

    graph = build_agent_graph(
        router_model=StubRouterModel("inspect"),
        inspect_graph=build_inspect_graph(status_fetcher=fetcher),
    )
    application.dependency_overrides[chat_api.get_agent_graph] = lambda: graph
    try:
        with client.stream(
            "POST",
            "/chat/stream",
            json={
                "home_id": home_id,
                "message": "帮我看看家里设备都正常吗",
                "thread_id": f"inspect-{uuid.uuid4().hex[:8]}",
            },
        ) as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())
    finally:
        application.dependency_overrides.pop(chat_api.get_agent_graph, None)

    events = _parse_sse(text)
    progress_events = [e for e in events if e["event"] == "inspect_progress"]
    result_events = [e for e in events if e["event"] == "inspect_result"]

    assert len(progress_events) == 1
    assert "巡检" in progress_events[0]["message"]
    assert len(result_events) == 1
    assert result_events[0]["report_id"] > 0
    assert result_events[0]["total_devices"] == 1
    done = events[-1]
    assert done["event"] == "done"
    assert "检查了 1 台设备" in done["answer"]
