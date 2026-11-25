"""空调域 MCP 动态发现验证：tools/list + tools/call。

对应第 6 周实施计划「第 1 步：学习 spike」第 2 件事的验证侧。

运行方式：uv run python examples/ac_client_demo.py

跑完后能回答两个问题：
1. 为什么 Agent 侧零代码就能用上新设备域？
   客户端事先不写死任何工具，启动时 tools/list 运行时拉清单——新增
   ac_server.py 只是「多启动一个子进程」，Agent 的工具定义一行没改。
2. tools/call 的参数从哪来？
   从 tools/list 返回的 input_schema 来（FastMCP 按类型注解生成），
   所以工具签名只用 str/int/float/bool 简单注解，schema 才简单可靠。
"""

import asyncio
from pathlib import Path

from fastmcp import Client

SERVER_PATH = Path(__file__).resolve().with_name("ac_server.py")


async def main() -> None:
    # 传入服务器脚本路径 => stdio 传输：Client 把 ac_server.py 拉起为子进程。
    async with Client(SERVER_PATH) as client:
        # 1) 动态发现：清单来自服务器运行时返回，客户端没有写死任何工具。
        tools = await client.list_tools()
        print("== tools/list 发现的工具 ==")
        for tool in tools:
            print(f"- {tool.name}: {(tool.description or '').splitlines()[0]}")

        # 2) 按发现到的工具名 + 参数调用，覆盖读 / 合法写 / 越界错误三条路径。
        status = await client.call_tool("get_ac_status", {"device_id": "ac-001"})
        print("\n== get_ac_status(ac-001) ==")
        print(status.data)

        changed = await client.call_tool(
            "set_temperature", {"device_id": "ac-001", "temperature": 24.0}
        )
        print("\n== set_temperature(ac-001, 24.0) ==")
        print(changed.data)

        blocked = await client.call_tool(
            "set_temperature", {"device_id": "ac-001", "temperature": 35.0}
        )
        print("\n== set_temperature(ac-001, 35.0) 越界 ==")
        print(blocked.data)

        powered = await client.call_tool(
            "set_ac_power", {"device_id": "ac-002", "on": True}
        )
        print("\n== set_ac_power(ac-002, on) ==")
        print(powered.data)

        # 3) 回读：确认写操作真的改了服务器内存态（“状态回读”的雏形）。
        again = await client.call_tool("get_ac_status", {"device_id": "ac-002"})
        print("\n== 回读 get_ac_status(ac-002) ==")
        print(again.data)


if __name__ == "__main__":
    asyncio.run(main())
