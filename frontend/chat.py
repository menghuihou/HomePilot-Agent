"""家庭助手聊天页（第 8 步）：多轮对话 + SSE 流式输出。

运行：uv run streamlit run frontend/chat.py
依赖本地 FastAPI 服务已启动（地址取 .env 的 STREAMLIT_API_BASE_URL）。
"""

import json
import sys
import uuid
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings

settings = get_settings()
API_BASE = settings.streamlit_api_base_url

st.set_page_config(page_title="HomePilot 聊天", page_icon="💬")
st.title("💬 HomePilot 家庭助手")

# --- 会话状态：消息历史与服务端会话 id ---
if "messages" not in st.session_state:
    st.session_state.messages = []
if "thread_id" not in st.session_state:
    st.session_state.thread_id = f"web-{uuid.uuid4().hex}"

# --- 家庭选择：API 未启动或无数据时优雅降级 ---
homes: list = []
try:
    homes = requests.get(f"{API_BASE}/homes", timeout=5).json()
except requests.RequestException:
    st.error(
        f"无法连接 API 服务（{API_BASE}）。请先在另一个终端启动：\n\n"
        "uv run uvicorn app.main:application --reload"
    )
    st.stop()

if not homes:
    st.warning("还没有家庭数据，请先执行 uv run python -m db.seeds.load_family")
    st.stop()

with st.sidebar:
    home = st.selectbox("家庭", homes, format_func=lambda h: h["name"])
    if st.button("🗑 新对话"):
        st.session_state.messages = []
        st.session_state.thread_id = f"web-{uuid.uuid4().hex}"
        st.rerun()
    st.caption(f"thread_id：`{st.session_state.thread_id}`")

# --- 历史消息 ---
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])


def _stream_reply(payload: dict, captured: dict):
    """消费 SSE 事件：yield 回复文本，done/error 存入 captured 供收尾展示。"""
    with requests.post(
        f"{API_BASE}/chat/stream", json=payload, stream=True, timeout=180
    ) as resp:
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", resp.text[:200])
            except ValueError:
                detail = resp.text[:200]
            captured["error"] = f"接口错误 {resp.status_code}：{detail}"
            yield captured["error"]
            return
        for line in resp.iter_lines(decode_unicode=True):
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: ") :])
            if event["event"] == "delta":
                yield event["text"]
            elif event["event"] == "error":
                captured["error"] = event["message"]
                yield f"⚠️ {event['message']}"
            elif event["event"] == "done":
                captured["answer"] = event.get("answer", "")
                captured["trace_id"] = event.get("trace_id", "")


# --- 输入与流式回复 ---
if prompt := st.chat_input("和助手聊聊，例如：把客厅灯叫主灯"):
    if not prompt.strip():
        st.warning("请输入消息内容")
        st.stop()

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    payload = {
        "home_id": home["id"],
        "message": prompt.strip(),
        "thread_id": st.session_state.thread_id,
    }
    captured: dict = {}
    with st.chat_message("assistant"):
        full_answer = st.write_stream(_stream_reply(payload, captured))

    st.session_state.messages.append(
        {"role": "assistant", "content": full_answer or captured.get("answer", "")}
    )
    if captured.get("error"):
        st.error(captured["error"])
    if captured.get("trace_id"):
        st.caption(f"trace：`{captured['trace_id']}`")
