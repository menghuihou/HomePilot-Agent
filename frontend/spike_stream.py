"""SSE spike：st.write_stream 消费 /chat/stream（第 7 步时间盒验证）。

运行：uv run streamlit run frontend/spike_stream.py
打字机效果出来即算 spike 成功；不通则降级轮询 /chat，不恋战。
"""

import json
import sys
from pathlib import Path

import requests
import streamlit as st

# Streamlit 只把脚本所在目录加入 sys.path；显式补充项目根目录才能导入
# app 包（与 frontend/home.py 同款引导）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings

API_BASE = get_settings().streamlit_api_base_url

st.title("SSE Spike — /chat/stream")

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

home = st.selectbox("家庭", homes, format_func=lambda h: h["name"])
message = st.text_input("消息", "把客厅灯叫主灯")

sent = st.button("发送")

if sent and not message.strip():
    st.warning("请输入消息内容后再发送")

if sent and message.strip():
    payload = {"home_id": home["id"], "message": message.strip()}
    events: list[dict] = []

    def _stream():
        with requests.post(
            f"{API_BASE}/chat/stream", json=payload, stream=True, timeout=120
        ) as resp:
            if resp.status_code != 200:
                st.error(f"接口返回 {resp.status_code}：{resp.text[:200]}")
                return
            for line in resp.iter_lines(decode_unicode=True):
                if line.startswith("data: "):
                    event = json.loads(line[len("data: ") :])
                    if event["event"] == "delta":
                        yield event["text"]
                    elif event["event"] in ("done", "error"):
                        events.append(event)

    st.write_stream(_stream())

    for event in events:
        if event["event"] == "error":
            st.error(event["message"])
