"""chat 接口测试：参数校验 422、家庭不存在 404、同步回答与 SSE 事件流。"""

import json

import pytest
from langchain_core.messages import AIMessage

from app.agent.chat import build_agent_graph
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
