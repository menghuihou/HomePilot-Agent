"""设备 MCP Server：4 个工具，数据源为业务库（devices / device_events）。

读写边界（第 4 周起）：`get_device_status` 已泛化为全设备域只读状态
查询（低风险，按 device_type 分派状态推导）；控制类工具仍保持域限制：
turn_light_on / turn_light_off 仅接受 light，restart_gateway 仅接受 gateway。

独立进程运行（stdio 传输）：

    uv run python mcp_servers/light_server.py

数据口径：设备当前状态由「最新一条 device_event」推导（事件溯源 MVP）；
turn_on / turn_off / restart_gateway 通过追加事件落库，天然留痕。

注意事项：
- stdio 模式下 stdout 是 JSON-RPC 协议通道，服务端代码禁止 print 到
  stdout，日志一律走 stderr，否则会破坏客户端的协议解析；
- 进程内自建 AsyncEngine（连接跟随进程生命周期）；作为 pytest 内进程
  复用时，跨事件循环复用前须先 dispose（见 test_mcp_light_server）；
- 风险等级标注说明：工具描述里的「风险等级」仅做展示参考，编排层的
  审批门控以 app/agent/risk_rules.py 的规则表为唯一依据。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Device, DeviceEvent

mcp = FastMCP("home-light-server")

# 独立进程自建引擎：NullPool 每次现连现断——MCP 工具调用稀疏，
# 且规避 pytest 进程内多事件循环复用连接池的跨 loop 问题。
_engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def _parse_device_id(device_id: str) -> int | None:
    """把传入 id 解析为业务库自增主键；不合法返回 None。"""
    try:
        return int(device_id)
    except TypeError, ValueError:
        return None


async def _load_device(device_id: int, *device_types: str) -> Device | None:
    """按主键加载设备；指定类型时校验设备域，不匹配视同不存在。"""
    async with _session_factory() as session:
        device = await session.get(Device, device_id)
    if device is None:
        return None
    if device_types and device.device_type not in device_types:
        return None
    return device


async def _latest_event(device_id: int) -> DeviceEvent | None:
    """加载设备最新一条事件（状态推导数据源）。"""
    async with _session_factory() as session:
        row = await session.execute(
            select(DeviceEvent)
            .where(DeviceEvent.device_id == device_id)
            .order_by(DeviceEvent.occurred_at.desc(), DeviceEvent.id.desc())
            .limit(1)
        )
        return row.scalars().first()


async def _append_event(device_id: int, event_type: str, payload: dict) -> None:
    """追加设备事件并提交（状态变更的唯一写入口径）。"""
    async with _session_factory() as session:
        session.add(
            DeviceEvent(device_id=device_id, event_type=event_type, payload=payload)
        )
        await session.commit()


def _status_from_event(device: Device, event: DeviceEvent | None) -> dict:
    """由最新事件按设备域推导当前状态；从未上报事件的设备统一视为离线。

    状态推导口径（第 4 周巡检的确定性异常检测依赖本结构）：
    - light：online + brightness；
    - sensor：online + battery（电量可能不上报，payload 缺省时不带该字段）；
    - thermostat：online + target_temperature / temperature（缺省为 None）；
    - gateway：online（以 payload["online"] 为准）+ last_seen_at；
    - 其他域：只返回 online + last_seen_at 最小状态，不编造域专属字段。
    """
    if event is None:
        return {"online": False, "last_seen_at": None}

    payload = event.payload or {}
    status: dict = {
        "online": event.event_type != "offline",
        "last_seen_at": event.occurred_at.isoformat(),
    }

    if device.device_type == "light":
        status["brightness"] = (
            0 if event.event_type == "power_off" else payload.get("brightness") or 100
        )
    elif device.device_type == "sensor":
        battery = payload.get("battery")
        if battery is not None:
            status["battery"] = battery
    elif device.device_type == "thermostat":
        status["target_temperature"] = payload.get("target_temperature")
        status["temperature"] = payload.get("temperature")
    elif device.device_type == "gateway":
        # gateway 的 online 以事件 payload 为准（heartbeat / offline / restart 都带该字段）。
        gateway_online = payload.get("online")
        status["online"] = (
            bool(gateway_online) if gateway_online is not None else status["online"]
        )

    return status


@mcp.tool
async def get_device_status(device_id: str) -> dict:
    """查询任意设备域的当前状态（在线 + 按域推导的状态字段）。

    风险等级：low。

    Args:
        device_id: 设备 id（业务库 devices.id），如 "17"。
    """
    parsed = _parse_device_id(device_id)
    if parsed is None:
        return {"device_id": device_id, "error": f"设备 id 不合法：{device_id}"}
    # 只读状态查询泛化到全设备域：巡检子图需要跨域并发查询。
    device = await _load_device(parsed)
    if device is None:
        return {
            "device_id": device_id,
            "error": f"设备不存在：{device_id}",
        }
    status = _status_from_event(device, await _latest_event(parsed))
    return {"device_id": device_id, "name": device.name, **status}


@mcp.tool
async def turn_light_on(device_id: str) -> str:
    """打开灯光设备；亮度为 0 时自动设为 80%。

    风险等级：low。

    Args:
        device_id: 设备 id（业务库 devices.id），如 "17"。
    """
    parsed = _parse_device_id(device_id)
    if parsed is None:
        return f"错误：设备 id 不合法：{device_id}"
    device = await _load_device(parsed, "light")
    if device is None:
        return f"错误：设备 {device_id} 不存在或不是灯光设备"
    brightness = _status_from_event(device, await _latest_event(parsed)).get(
        "brightness"
    )
    new_brightness = brightness or 80
    await _append_event(parsed, "power_on", {"brightness": new_brightness})
    return f"已打开{device.name}，当前亮度 {new_brightness}%"


@mcp.tool
async def turn_light_off(device_id: str) -> str:
    """关闭灯光设备，亮度归零。

    风险等级：low。

    Args:
        device_id: 设备 id（业务库 devices.id），如 "17"。
    """
    parsed = _parse_device_id(device_id)
    if parsed is None:
        return f"错误：设备 id 不合法：{device_id}"
    device = await _load_device(parsed, "light")
    if device is None:
        return f"错误：设备 {device_id} 不存在或不是灯光设备"
    await _append_event(parsed, "power_off", {"brightness": 0})
    return f"已关闭{device.name}"


@mcp.tool
async def restart_gateway(gateway_id: str) -> str:
    """重启家庭网关，恢复全屋设备接入；高风险动作，须审批通过后调用。

    风险等级：high。

    Args:
        gateway_id: 网关设备 id（业务库 devices.id），如 "3"。
    """
    parsed = _parse_device_id(gateway_id)
    if parsed is None:
        return f"错误：网关 id 不合法：{gateway_id}"
    device = await _load_device(parsed, "gateway")
    if device is None:
        return f"错误：网关 {gateway_id} 不存在"
    await _append_event(parsed, "gateway_restart", {"online": True})
    return f"{device.name}重启成功，全屋设备已重新接入"


if __name__ == "__main__":
    # 默认 stdio 传输：stdin/stdout 承载 JSON-RPC，进程阻塞等待客户端。
    mcp.run()
