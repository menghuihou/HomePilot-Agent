"""MCP 客户端封装：拉起 Server 子进程、动态发现工具并转为 LangChain 工具。

已知限制（本周不做自动重连）：Server 进程异常退出后，已缓存的工具调用会抛
MCPError，由调用方转成错误信息；重启应用即恢复。
"""

import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from langchain_core.tools import BaseTool, StructuredTool
from opentelemetry.trace import get_tracer
from pydantic import BaseModel, create_model

from app.config import BASE_DIR
from app.observability import observation_payload

logger = logging.getLogger(__name__)

_mcp_tracer = get_tracer("home-pilot.mcp")

_JSON_TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool}


def _resolve_py_type(spec: dict) -> type:
    """JSON Schema 属性 → Python 类型。

    可选参数的 anyOf 形态（如 int | null → [{"type": "integer"},
    {"type": "null"}]）取首个非 null 类型；无法识别的兜底 str，交给
    pydantic 校验给出明确错误。
    """
    if "anyOf" in spec:
        for sub_spec in spec["anyOf"]:
            sub_type = sub_spec.get("type") if isinstance(sub_spec, dict) else None
            if sub_type and sub_type != "null":
                return _JSON_TYPE_MAP.get(sub_type, str)
        return str
    return _JSON_TYPE_MAP.get(spec.get("type", "string"), str)


def _schema_to_args_model(tool_name: str, schema: dict) -> type[BaseModel]:
    """把 MCP inputSchema（JSON Schema 常用子集）转成动态 Pydantic 模型。"""
    fields: dict[str, Any] = {}
    required = set(schema.get("required", []))
    for prop, spec in schema.get("properties", {}).items():
        py_type = _resolve_py_type(spec)
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
        transport = StdioTransport(
            self._command,
            self._args,
            # MCP SDK 默认只给子进程传最小环境白名单（PATH/HOME 等），
            # DATABASE_URL/MODEL_* 会丢失：CI 上 server 启动即崩（Connection
            # closed），本地则静默回退 .env 连到错误库。同机可信子进程，
            # 显式透传完整父环境。
            env=dict(os.environ),
            cwd=str(self._cwd),
        )
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
            # 业务层观测 span：LangChain TOOL / FastMCP 协议 span 不写
            # input/output（Langfuse UI 显示 undefined），这里补实际参数
            # 和返回值。所有子图的 MCP 调用统一经过此闭包，一处覆盖全部。
            with _mcp_tracer.start_as_current_span(f"mcp-tool:{tool_name}") as span:
                if span.is_recording():
                    span.set_attribute("langfuse.observation.type", "span")
                    span.set_attribute(
                        "langfuse.observation.input", observation_payload(kwargs)
                    )
                result = await client.call_tool(tool_name, kwargs)
                data = getattr(result, "data", None)
                if data is None:
                    data = result.content[0].text
                if span.is_recording():
                    span.set_attribute(
                        "langfuse.observation.output", observation_payload(data)
                    )
                return data

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

# 空调域 Server 单例（第 6 步交付 mcp_servers/ac_server.py）。Server 文件
# 就位前惰性启动会失败，由调用方按「执行失败」兜底——客户端接线先行，
# 第 6 步新增 Server 文件即零 Agent 代码接入（可扩展性实证的一部分）。
ac_mcp_client = McpToolClient(
    command="uv",
    args=["run", "python", "mcp_servers/ac_server.py"],
    cwd=BASE_DIR,
)


async def get_light_tools() -> list[BaseTool]:
    """Agent 惰性初始化入口：首次调用拉起 Server，之后返回缓存工具。"""
    return await light_mcp_client.ensure_started()


async def get_ac_tools() -> list[BaseTool]:
    """空调域工具惰性初始化入口；Server 未交付前抛错由调用方兜底。"""
    return await ac_mcp_client.ensure_started()


async def fetch_device_status(device_id: int) -> dict:
    """按业务库主键查询任意设备域的当前状态（get_device_status）。

    巡检子图与诊断子图的共享数据源：id 约定统一为业务库 devices.id，
    在 MCP 调用参数处转字符串；MCP 侧故障的降级策略（标记查询失败 /
    本轮不产出证据）由调用方决定。本函数只做结构归一——非 dict 返回
    统一转为 error 结构；MCP Server 启动失败和工具调用异常都会向外传播。
    """
    tools = {tool.name: tool for tool in await get_light_tools()}
    tool = tools.get("get_device_status")
    if tool is None:
        return {"error": "MCP 工具 get_device_status 未注册"}
    # MCP Server 按业务库 devices.id 查询：直接传库内主键。
    result = await tool.ainvoke({"device_id": str(device_id)})
    return result if isinstance(result, dict) else {"error": "MCP 返回结构异常"}


__all__ = [
    "McpToolClient",
    "ac_mcp_client",
    "fetch_device_status",
    "get_ac_tools",
    "get_light_tools",
    "light_mcp_client",
]
