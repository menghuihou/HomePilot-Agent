"""空调域 MCP Server（学习 spike 版）：FastMCP 4.x，3 个工具，内存模拟态。

对应第 6 周实施计划「第 1 步：学习 spike」的第 2 件事。

与正式版（第 6 步的 mcp_servers/ac_server.py）的差异：
- spike 版数据放内存字典，不接业务库、不需要 PostgreSQL；
- 正式版接入种子设备 + device_events 事件溯源，工具签名保持一致。

由 ac_client_demo.py 以 stdio 子进程方式拉起，也可独立运行：

    uv run python examples/ac_server.py
"""

from fastmcp import FastMCP

mcp = FastMCP("home-ac-server")

# 内存模拟态：spike 只验证「新 Server 能被动态发现」，不关心持久化。
_AC_STATE: dict[str, dict] = {
    "ac-001": {"name": "客厅空调", "online": True, "power": True, "temperature": 26.0},
    "ac-002": {
        "name": "卧室空调",
        "online": False,
        "power": False,
        "temperature": None,
    },
}
MIN_TEMPERATURE = 16.0
MAX_TEMPERATURE = 30.0


def _error(device_id: str, message: str) -> dict:
    """统一的结构化错误：Agent 能直接转述给用户，不需要解析异常栈。"""
    return {"device_id": device_id, "error": message}


@mcp.tool
def get_ac_status(device_id: str) -> dict:
    """查询空调当前状态（在线 / 开关 / 目标温度）。

    风险等级：low。

    Args:
        device_id: 空调设备 id，如 "ac-001"。
    """
    state = _AC_STATE.get(device_id)
    if state is None:
        return _error(device_id, f"空调不存在：{device_id}")
    return {"device_id": device_id, **state}


@mcp.tool
def set_temperature(device_id: str, temperature: float) -> dict:
    """设置空调目标温度，范围 16-30℃。

    越界返回结构化错误而不是抛异常，Agent 才能把原因转述给用户。

    风险等级：low。

    Args:
        device_id: 空调设备 id，如 "ac-001"。
        temperature: 目标温度（℃），16-30。
    """
    state = _AC_STATE.get(device_id)
    if state is None:
        return _error(device_id, f"空调不存在：{device_id}")
    if not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
        return _error(
            device_id,
            f"温度 {temperature}℃ 超出范围（{MIN_TEMPERATURE}-{MAX_TEMPERATURE}℃）",
        )
    state["temperature"] = temperature
    return {"device_id": device_id, "temperature": temperature, "message": "温度已设置"}


@mcp.tool
def set_ac_power(device_id: str, on: bool) -> dict:
    """开关空调电源。

    风险等级：medium——离家模式下关空调可能涉及冲突检测，须编排层审批门控裁决。

    Args:
        device_id: 空调设备 id，如 "ac-001"。
        on: True 开机 / False 关机。
    """
    state = _AC_STATE.get(device_id)
    if state is None:
        return _error(device_id, f"空调不存在：{device_id}")
    state["power"] = on
    return {
        "device_id": device_id,
        "power": on,
        "message": f"{'已开机' if on else '已关机'}",
    }


if __name__ == "__main__":
    mcp.run()  # 默认 stdio 传输：stdin/stdout 承载 JSON-RPC
