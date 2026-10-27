"""最小 MCP Client：动态发现服务器工具（tools/list）并调用（tools/call）。

运行方式：uv run python examples/mcp_client.py
"""

import asyncio
from pathlib import Path

from fastmcp import Client

SERVER_PATH = Path(__file__).resolve().with_name("mcp_server.py")


async def main() -> None:
    # 传入服务器脚本路径 => stdio 传输：Client 用当前 Python 解释器把 Server 作为子进程拉起。
    # 路径相对本文件解析，避免工作目录不同（如 IDE 默认以脚本所在目录启动）导致找不到文件。
    async with Client(SERVER_PATH) as client:
        # 1) 动态发现：客户端事先不写死任何工具，运行时向服务器要工具清单。
        tools = await client.list_tools()
        print("== tools/list 发现的工具 ==")
        for tool in tools:
            print(f"- {tool.name}: {tool.description}")
            print(f"  参数 schema: {tool.input_schema}")

        # 2) 按发现到的工具名 + 参数调用。
        status = await client.call_tool("get_device_status", {"device_id": "light-001"})
        print("\n== tools/call get_device_status ==")
        print(status.data)

        changed = await client.call_tool(
            "set_light_brightness",
            {"device_id": "light-001", "level": 50},
        )
        print("\n== tools/call set_light_brightness ==")
        print(changed.data)


if __name__ == "__main__":
    asyncio.run(main())
