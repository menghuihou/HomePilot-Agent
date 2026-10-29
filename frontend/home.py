"""家庭状态页面。

用法：uv run streamlit run frontend/home.py

依赖本地 FastAPI 服务已启动（地址取 .env 的 STREAMLIT_API_BASE_URL，
默认 http://127.0.0.1:8000）。
"""

import sys
from pathlib import Path

import requests
import streamlit as st

# Streamlit 只把脚本所在目录加入 sys.path；显式补充项目根目录才能导入
# app 包。页面文件不能叫 app.py，否则模块名会遮蔽 app 包导致导入失败。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings

settings = get_settings()

st.set_page_config(page_title="HomePilot 家庭状态", page_icon="🏠")
st.title("🏠 HomePilot 家庭状态")


def _api_get(path: str, timeout: float = 5) -> list | dict:
    resp = requests.get(f"{settings.streamlit_api_base_url}{path}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _api_post(path: str, payload: dict, timeout: float = 5) -> dict:
    resp = requests.post(
        f"{settings.streamlit_api_base_url}{path}", json=payload, timeout=timeout
    )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(detail)
    return resp.json()


try:
    homes = _api_get("/homes")
except requests.RequestException as exc:
    st.error(
        f"无法连接 API 服务（{settings.streamlit_api_base_url}）。"
        "请先启动：uv run uvicorn app.main:application --reload"
    )
    st.caption(str(exc))
    st.stop()

if not homes:
    st.info(
        "暂无家庭数据。可运行 `uv run python -m db.seeds.load_family` 生成演示数据。"
    )
    st.stop()

home_labels = [f"{h['name']}（#{h['id']}）" for h in homes]
home = homes[home_labels.index(st.selectbox("选择家庭", home_labels))]
home_id = home["id"]

try:
    rooms = _api_get(f"/homes/{home_id}/rooms")
    devices = _api_get(f"/homes/{home_id}/devices")
except requests.RequestException as exc:
    st.error(f"读取家庭数据失败：{exc}")
    st.stop()

room_name_by_id = {r["id"]: r["name"] for r in rooms}

st.subheader("🚪 房间")
if rooms:
    st.dataframe(
        [{"房间": r["name"], "类型": r.get("room_type") or "—"} for r in rooms],
        hide_index=True,
    )
else:
    st.caption("该家庭还没有房间。")

st.subheader("📱 设备列表")
if devices:
    st.dataframe(
        [
            {
                "设备": d["name"],
                "类型": d["device_type"],
                "厂商": d.get("vendor") or "—",
                "房间": room_name_by_id.get(d["room_id"], "—"),
            }
            for d in devices
        ],
        hide_index=True,
    )
else:
    st.caption("该家庭还没有设备，可在下方录入。")

st.subheader("➕ 录入设备")
with st.form("add-device", clear_on_submit=True):
    device_name = st.text_input("设备名称")
    device_type = st.text_input(
        "设备类型",
        value="light",
        help="如 light / thermostat / sensor / plug / gateway",
    )
    vendor = st.text_input("厂商（可选）")
    room_options = ["（不分配房间）"] + [r["name"] for r in rooms]
    room_choice = st.selectbox("所属房间", room_options)
    submitted = st.form_submit_button("添加设备", type="primary")

if submitted:
    if not device_name.strip():
        st.error("设备名称不能为空。")
    else:
        payload = {
            "name": device_name.strip(),
            "device_type": device_type.strip() or "light",
        }
        if room_choice != "（不分配房间）":
            payload["room_id"] = next(
                r["id"] for r in rooms if r["name"] == room_choice
            )
        try:
            created = _api_post(f"/homes/{home_id}/devices", payload)
            st.success(f"设备「{created['name']}」已添加。")
            st.rerun()  # 重新加载页面，刷新房间与设备列表
        except RuntimeError as exc:
            st.error(f"添加失败：{exc}")

st.subheader("🤖 LLM 调用演示")
st.caption(
    "点击后调用后端模型接口；下方 trace id 可直接在 Langfuse 中检索完整调用链路。"
)
if st.button("触发一次 LLM 调用"):
    with st.spinner("模型调用中……"):
        try:
            # LLM 是真实推理调用，耗时远超普通接口，放宽超时。
            result = _api_post("/llm/demo", {}, timeout=120)
            st.success(result["answer"])
            st.session_state["last_trace_id"] = result["trace_id"]
        except requests.RequestException as exc:
            st.error(f"请求失败，请确认 API 服务已启动：{exc}")
        except RuntimeError as exc:
            st.error(f"LLM 调用失败：{exc}")
if "last_trace_id" in st.session_state:
    st.write("本次 trace id：")
    st.code(st.session_state["last_trace_id"], language=None)
