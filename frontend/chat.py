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
if "pending_approval" not in st.session_state:
    st.session_state.pending_approval = None
if "home_id" not in st.session_state:
    # 持久化已选家庭 id：服务重启/家庭列表变化后按 id 恢复选择，
    # 避免静默回退到列表第一个（可能是空家庭）导致请求打错家庭。
    st.session_state.home_id = None

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

valid_homes = {h["id"]: h for h in homes}
if st.session_state.home_id not in valid_homes:
    if st.session_state.home_id is not None:
        st.info("服务已重启或家庭列表发生变化，请重新确认所选家庭。")
    st.session_state.home_id = homes[0]["id"]

with st.sidebar:
    home = st.selectbox(
        "家庭",
        homes,
        index=list(valid_homes).index(st.session_state.home_id),
        format_func=lambda h: h["name"],
    )
    st.session_state.home_id = home["id"]
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
            elif event["event"] == "approval_required":
                captured["approval"] = event
                yield (
                    f"⛔ 高风险操作待审批：{event.get('action')}"
                    f"（风险等级：{event.get('risk_level')}）"
                )
            elif event["event"] == "inspect_progress":
                captured["inspect_progress"] = event.get("message", "")
                yield f"🔍 {event.get('message', '正在巡检设备状态…')}"
            elif event["event"] == "inspect_result":
                captured["inspect_result"] = event
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
        streamed = st.write_stream(_stream_reply(payload, captured))
        # 直答路径（home_mode / 占位回复）没有 delta 流：write_stream
        # 渲染为空，用 done 事件的最终答案补齐，避免空气泡。
        # 空 answer 兜底：模型空回复不再渲染空气泡。
        final_text = (
            captured.get("answer")
            or streamed
            or "（本次未生成回复内容，请重试，或查看 Langfuse trace 排查）"
        )
        if captured.get("inspect_result"):
            # 巡检路径：delta 流里只有进度提示，摘要正文在这里补渲染。
            st.markdown(captured["inspect_result"].get("summary_text", final_text))
        elif not streamed and final_text:
            st.markdown(final_text)

    st.session_state.messages.append({"role": "assistant", "content": final_text})
    if captured.get("error"):
        st.error(captured["error"])
    if captured.get("trace_id"):
        st.caption(f"trace：`{captured['trace_id']}`")
    if captured.get("inspect_result"):
        # 巡检卡片：异常项展开高亮，正常项计数收纳（异常项高亮 + 正常项折叠）。
        result = captured["inspect_result"]
        with st.chat_message("assistant"):
            st.info(
                f"🔍 巡检完成：共 {result.get('total_devices')} 台设备，"
                f"正常 {result.get('normal_count')} 台，"
                f"异常 {len(result.get('anomalies', []))} 台"
                f"（报告 #{result.get('report_id')}）"
            )
            for anomaly in result.get("anomalies", []):
                severity = anomaly.get("severity", "medium")
                icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(severity, "🟡")
                st.markdown(
                    f"{icon} **{anomaly.get('device_name')}**：{anomaly.get('detail')}"
                )
            if not result.get("anomalies"):
                st.success("所有设备运行正常。")
    if captured.get("approval"):
        # 审批卡片在整页刷新后仍需可交互：存入会话状态，下方统一渲染。
        st.session_state.pending_approval = captured["approval"]
        st.rerun()


# --- 审批卡片：高风险操作等待用户决策（决策走 /approvals 决策接口） ---
if st.session_state.pending_approval:
    approval = st.session_state.pending_approval
    with st.chat_message("assistant"):
        st.warning(
            f"⛔ 高风险操作待审批\n\n"
            f"动作：`{approval.get('action')}`　风险等级："
            f"**{approval.get('risk_level')}**\n\n{approval.get('reason', '')}"
            f"\n\n建议：{approval.get('suggestion', '')}"
        )
        col_approve, col_reject = st.columns(2)
        decision = None
        if col_approve.button("✅ 同意执行", key="approval-approve"):
            decision = "approved"
        if col_reject.button("🚫 拒绝", key="approval-reject"):
            decision = "rejected"
        if decision:
            try:
                resp = requests.post(
                    f"{API_BASE}/approvals/{st.session_state.thread_id}/decision",
                    json={"decision": decision},
                    timeout=60,
                )
                if resp.status_code == 200:
                    report = resp.json().get("report") or "审批已处理。"
                else:
                    report = f"审批提交失败（{resp.status_code}）：{resp.text[:200]}"
            except requests.RequestException as exc:
                report = f"审批提交失败：{exc}"
            st.session_state.messages.append({"role": "assistant", "content": report})
            st.session_state.pending_approval = None
            st.rerun()
