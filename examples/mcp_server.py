"""最小 MCP Server：FastMCP 4.x，两个工具（家庭灯光域）。

由 MCP Client 以 stdio 子进程方式拉起，协议消息走 stdin/stdout（JSON-RPC 2.0）。
"""

from fastmcp import FastMCP

mcp = FastMCP("home-device-server")


@mcp.tool
def get_device_status(device_id: str) -> dict:
    """查询设备当前状态（示例返回模拟数据）。"""
    return {"device_id": device_id, "online": True, "brightness": 80}


@mcp.tool
def set_light_brightness(device_id: str, level: int) -> str:
    """设置灯光亮度，level 取值 0-100。"""
    if not 0 <= level <= 100:
        raise ValueError("level 必须在 0-100 之间")
    return f"{device_id} 亮度已设置为 {level}%"


if __name__ == "__main__":
    mcp.run()  # 默认 stdio 传输
