"""空调域 MCP Server：3 个工具，数据源为业务库（devices / device_events）。

事件溯源口径与灯光 Server 同款：状态 = 最新事件推导；控制动作 = 追加
事件落库（power_on / power_off / state_change），天然留痕。控制事件的
payload 携带完整快照（power + temperature），单条最新事件即可完整推导
空调状态，无需回放历史。

零代码扩展实证（第 6 周招牌动作）：Agent 侧在第 4 步已按 ac 域预埋
接线——执行器注册表（_ACTION_TOOL_BY_ID）与状态装配域路由
（_fetch_status_for）——本文件交付后即自动接入：编排图、规则表、
前端零改动。

独立进程运行（stdio 传输）：

    uv run python mcp_servers/ac_server.py

注意事项（与灯光 Server 一致）：
- stdio 模式下 stdout 是 JSON-RPC 协议通道，服务端代码禁止 print 到
  stdout，日志一律走 stderr；
- 进程内自建 AsyncEngine（NullPool：MCP 工具调用稀疏，现连现断，
  规避 pytest 多事件循环复用连接池的问题）；
- 工具描述里的「风险等级」仅做展示参考，编排层的审批门控以
  app/agent/risk_rules.py 的规则表为唯一依据。
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

mcp = FastMCP("home-ac-server")

_engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)

MIN_TEMPERATURE = 16.0
MAX_TEMPERATURE = 30.0


def _parse_device_id(device_id: str) -> int | None:
    """把传入 id 解析为业务库自增主键；不合法返回 None。"""
    try:
        return int(device_id)
    except TypeError, ValueError:
        return None


async def _load_ac_device(session, device_id: int) -> Device | None:
    """按主键加载设备并校验空调域；不匹配视同不存在。"""
    device = await session.get(Device, device_id)
    if device is None or device.device_type != "ac":
        return None
    return device


async def _latest_event(session, device_id: int) -> DeviceEvent | None:
    """加载设备最新一条事件（状态推导数据源）。"""
    result = await session.execute(
        select(DeviceEvent)
        .where(DeviceEvent.device_id == device_id)
        .order_by(DeviceEvent.occurred_at.desc(), DeviceEvent.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _append_event(
    session, device_id: int, event_type: str, payload: dict
) -> None:
    """追加设备事件并提交（状态变更的唯一写入口径）。"""
    session.add(
        DeviceEvent(device_id=device_id, event_type=event_type, payload=payload)
    )
    await session.commit()


def _status_from_event(device: Device, event: DeviceEvent | None) -> dict:
    """ac 域状态推导：最新事件的 payload 即完整快照（power + temperature）。

    控制事件落库时始终携带完整快照，单条最新事件即可推导，无需回放；
    从未上报事件的设备统一视为离线且不含域字段。"""
    if event is None:
        return {
            "online": False,
            "power": None,
            "temperature": None,
            "last_seen_at": None,
        }
    payload = event.payload or {}
    return {
        "online": event.event_type != "offline",
        "power": payload.get("power"),
        "temperature": payload.get("temperature"),
        "last_seen_at": event.occurred_at.isoformat(),
    }


@mcp.tool
async def get_ac_status(device_id: str) -> dict:
    """查询空调当前状态（在线 / 开关 / 目标温度）。

    风险等级：low。

    Args:
        device_id: 设备 id（业务库 devices.id），如 "4387"。
    """
    parsed = _parse_device_id(device_id)
    if parsed is None:
        return {"device_id": device_id, "error": f"设备 id 不合法：{device_id}"}
    async with _session_factory() as session:
        device = await _load_ac_device(session, parsed)
        if device is None:
            return {
                "device_id": device_id,
                "error": f"设备 {device_id} 不存在或不是空调设备",
            }
        status = _status_from_event(device, await _latest_event(session, parsed))
    return {"device_id": device_id, "name": device.name, **status}


@mcp.tool
async def set_ac_power(
    device_id: str, on: bool, temperature: float | None = None
) -> dict:
    """开关空调电源；开机时可同时设定目标温度（℃）。

    风险等级：medium（离家 / 夜间模式关停影响舒适度，编排层可叠加策略）。

    Args:
        device_id: 设备 id（业务库 devices.id），如 "4387"。
        on: True 开机 / False 关机。
        temperature: 开机时可选目标温度（16-30℃）；缺省保持当前设定。
    """
    if (
        temperature is not None
        and not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE
    ):
        return {
            "device_id": device_id,
            "error": (
                f"温度 {temperature:g}℃ 超出范围"
                f"（{MIN_TEMPERATURE:g}-{MAX_TEMPERATURE:g}℃）"
            ),
        }
    parsed = _parse_device_id(device_id)
    if parsed is None:
        return {"device_id": device_id, "error": f"设备 id 不合法：{device_id}"}
    async with _session_factory() as session:
        device = await _load_ac_device(session, parsed)
        if device is None:
            return {
                "device_id": device_id,
                "error": f"设备 {device_id} 不存在或不是空调设备",
            }
        latest = await _latest_event(session, parsed)
        current_temperature = (
            (latest.payload or {}).get("temperature") if latest else None
        )
        event_type = "power_on" if on else "power_off"
        payload: dict = {"power": on}
        if on:
            keep = temperature if temperature is not None else current_temperature
            if keep is not None:
                payload["temperature"] = keep
        elif current_temperature is not None:
            # 关机事件同样携带完整快照：温度保留，重新开机时无需回放历史。
            payload["temperature"] = current_temperature
        await _append_event(session, parsed, event_type, payload)
        if on:
            if "temperature" in payload:
                message = f"已开机，目标温度 {payload['temperature']:g}℃"
            else:
                message = "已开机"
        else:
            message = "已关机"
    return {
        "device_id": device_id,
        "name": device.name,
        "power": on,
        "temperature": payload.get("temperature"),
        "message": message,
    }


@mcp.tool
async def set_temperature(device_id: str, temperature: float) -> dict:
    """调节空调目标温度（16-30℃）；不改变开关状态。

    风险等级：low。

    Args:
        device_id: 设备 id（业务库 devices.id），如 "4387"。
        temperature: 目标温度（16-30℃）。
    """
    if not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
        return {
            "device_id": device_id,
            "error": (
                f"温度 {temperature:g}℃ 超出范围"
                f"（{MIN_TEMPERATURE:g}-{MAX_TEMPERATURE:g}℃）"
            ),
        }
    parsed = _parse_device_id(device_id)
    if parsed is None:
        return {"device_id": device_id, "error": f"设备 id 不合法：{device_id}"}
    async with _session_factory() as session:
        device = await _load_ac_device(session, parsed)
        if device is None:
            return {
                "device_id": device_id,
                "error": f"设备 {device_id} 不存在或不是空调设备",
            }
        latest = await _latest_event(session, parsed)
        current_power = (latest.payload or {}).get("power") if latest else None
        await _append_event(
            session,
            parsed,
            "state_change",
            {"power": current_power, "temperature": temperature},
        )
    return {
        "device_id": device_id,
        "name": device.name,
        "temperature": temperature,
        "message": f"目标温度已调整为 {temperature:g}℃",
    }


if __name__ == "__main__":
    # 默认 stdio 传输：stdin/stdout 承载 JSON-RPC，进程阻塞等待客户端。
    mcp.run()
