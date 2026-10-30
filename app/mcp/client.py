"""MCP 客户端封装：拉起 Server 子进程、动态发现工具并转为 LangChain 工具。

已知限制（本周不做自动重连）：Server 进程异常退出后，已缓存的工具调用会抛
MCPError，由调用方转成错误信息；重启应用即恢复。
"""

import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, create_model

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

_JSON_TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool}


def _schema_to_args_model(tool_name: str, schema: dict) -> type[BaseModel]:
    """把 MCP inputSchema（JSON Schema 常用子集）转成动态 Pydantic 模型。"""
    fields: dict[str, Any] = {}
    required = set(schema.get("required", []))
    for prop, spec in schema.get("properties", {}).items():
        py_type = _JSON_TYPE_MAP.get(spec.get("type", "string"), str)
        if prop in required:
            fields[prop] = (py_type, ...)
        else:
            fields[prop] = (py_type | None, None)
    return create_model(f"{tool_name}_args", **fields)


class McpToolClient:
    """单个 MCP Server 的长连接客户端：惰性启动、动态发现、工具缓存。

    会话绑定启动时的事件循环；换循环使用请新建实例（测试各自新建）。
    """

    def __init__(self, command: str, args: list[str], cwd: Path):
        self._command = command
        self._args = args
        self._cwd = cwd
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tools: list[BaseTool] = []

    async def ensure_started(self) -> list[BaseTool]:
        """首次调用时拉起 Server 并缓存工具列表；已连接则直接返回缓存。"""
        if self._client is not None:
            return self._tools
        self._stack = AsyncExitStack()
        transport = StdioTransport(self._command, self._args, cwd=str(self._cwd))
        self._client = await self._stack.enter_async_context(Client(transport))
        mcp_tools = await self._client.list_tools()
        self._tools = [self._convert(tool) for tool in mcp_tools]
        logger.info(
            "MCP 动态发现 %d 个工具：%s",
            len(self._tools),
            [tool.name for tool in self._tools],
        )
        return self._tools

    def _convert(self, mcp_tool) -> BaseTool:
        client = self._client
        tool_name = mcp_tool.name

        async def _acall(**kwargs):
            result = await client.call_tool(tool_name, kwargs)
            data = getattr(result, "data", None)
            if data is not None:
                return data
            return result.content[0].text

        def _sync_call(**kwargs):
            # from_function 不会自动识别 async func；同步路径显式拒绝，
            # 避免 ainvoke 拿到未 await 的协程对象。
            raise NotImplementedError("MCP 工具仅支持异步调用（ainvoke）")

        return StructuredTool.from_function(
            func=_sync_call,
            coroutine=_acall,
            name=tool_name,
            description=mcp_tool.description or "",
            args_schema=_schema_to_args_model(tool_name, mcp_tool.input_schema),
        )

    async def aclose(self) -> None:
        """关闭会话并终止 Server 子进程；下次 ensure_started 会重新拉起。"""
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None
        self._tools = []


# 灯光域 Server 单例：以 uv 拉起子进程（PATH 上没有裸 python，不能让 fastmcp 推断）。
light_mcp_client = McpToolClient(
    command="uv",
    args=["run", "python", "mcp_servers/light_server.py"],
    cwd=BASE_DIR,
)


async def get_light_tools() -> list[BaseTool]:
    """Agent 惰性初始化入口：首次调用拉起 Server，之后返回缓存工具。"""
    return await light_mcp_client.ensure_started()


__all__ = ["McpToolClient", "get_light_tools", "light_mcp_client"]
