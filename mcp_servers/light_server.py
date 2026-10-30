"""灯光域 MCP Server 骨架：3 个工具，全部基于内存模拟数据。

独立进程运行（stdio 传输）：

    uv run python mcp_servers/light_server.py

注意：stdio 模式下 stdout 是 JSON-RPC 协议通道，服务端代码禁止 print 到
stdout，日志一律走 stderr，否则会破坏客户端的协议解析。
"""

from fastmcp import FastMCP

mcp = FastMCP("home-light-server")

# 模拟设备表：进程内存活，服务重启即恢复初始值。
_LIGHTS: dict[str, dict] = {
    "light-1": {"name": "客厅灯", "online": True, "brightness": 80},
    "light-2": {"name": "卧室灯", "online": True, "brightness": 0},
    "light-3": {"name": "阳台灯", "online": False, "brightness": 0},
}

_AVAILABLE_IDS = ", ".join(_LIGHTS)


@mcp.tool
def get_device_status(device_id: str) -> dict:
    """查询灯光设备的当前状态（在线、亮度）。

    Args:
        device_id: 设备 id，可用值：light-1 / light-2 / light-3。
    """
    light = _LIGHTS.get(device_id)
    if light is None:
        return {"device_id": device_id, "error": f"设备不存在，可用：{_AVAILABLE_IDS}"}
    return {"device_id": device_id, **light}


@mcp.tool
def turn_light_on(device_id: str) -> str:
    """打开灯光设备；亮度为 0 时自动设为 80%。

    Args:
        device_id: 设备 id，可用值：light-1 / light-2 / light-3。
    """
    light = _LIGHTS.get(device_id)
    if light is None:
        return f"错误：设备 {device_id} 不存在，可用：{_AVAILABLE_IDS}"
    light["online"] = True
    if light["brightness"] == 0:
        light["brightness"] = 80
    return f"已打开{light['name']}，当前亮度 {light['brightness']}%"


@mcp.tool
def turn_light_off(device_id: str) -> str:
    """关闭灯光设备，亮度归零。

    Args:
        device_id: 设备 id，可用值：light-1 / light-2 / light-3。
    """
    light = _LIGHTS.get(device_id)
    if light is None:
        return f"错误：设备 {device_id} 不存在，可用：{_AVAILABLE_IDS}"
    light["online"] = False
    light["brightness"] = 0
    return f"已关闭{light['name']}"


if __name__ == "__main__":
    # 默认 stdio 传输：stdin/stdout 承载 JSON-RPC，进程阻塞等待客户端。
    mcp.run()
