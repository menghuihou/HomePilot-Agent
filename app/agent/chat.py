"""chat 子图：带偏好记录工具的多轮对话，以及 Agent 主图编排。

主图流程：load_context -> route ->（chat 子图 | direct_reply）-> to_answer。
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langfuse import get_client, observe
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent.context import HomeContext, resolve_home_context
from app.agent.router import classify_intent, reply_for_intent, summarize_home_context
from app.agent.state import AgentState
from app.config import get_settings
from app.database import session_factory
from app.llm_client import build_chat_model, extract_text_content
from app.mcp.client import get_light_tools
from app.models import Home, Preference

_CHAT_SYSTEM_PROMPT = """你是家庭智能助手「HomePilot」，用中文口语化回复。

当前家庭上下文：
{home_summary}

偏好记录约定：
- 用户给设备起称呼（如「把客厅灯叫主灯」）时，必须调用 record_preference 工具，
  键格式为 device_alias:{{设备id}}（设备 id 见上下文设备列表），值就是称呼。
- 用户表达其他需要记住的偏好时也可调用该工具，键用简短英文。
- 只能使用上下文里列出的设备，不要编造。工具执行成功后，向用户确认记录结果。"""


class ChatState(TypedDict):
    """chat 子图状态：只需消息与家庭上下文。"""

    # 注入 record_preference 工具，LLM 不感知也不可篡改。
    home_id: int
    messages: Annotated[list[AnyMessage], add_messages]
    home_context: HomeContext


@observe(as_type="tool", name="record_preference")
async def _record_preference(home_id: int, pref_key: str, pref_value: str) -> str:
    # 工具自管会话与事务；家庭不存在时返回失败说明，由 LLM 转告用户。
    async with session_factory() as session:
        home = await session.get(Home, home_id)
        if home is None:
            return f"记录失败：家庭 {home_id} 不存在"
        stmt = (
            pg_insert(Preference)
            .values(
                home_id=home_id,
                member_id=None,
                pref_key=pref_key,
                pref_value=pref_value,
            )
            .on_conflict_do_update(
                constraint="uq_preferences_home_member_key",
                set_={"pref_value": pref_value, "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
        await session.commit()
    return f"已记录偏好：{pref_key}={pref_value}"


@tool
async def record_preference(
    pref_key: str,
    pref_value: str,
    home_id: Annotated[int, InjectedState("home_id")],
) -> str:
    """记录用户偏好，同一键重复设置时覆盖旧值。

    设备称呼类偏好的键格式为 device_alias:{设备id}，例如 device_alias:3 表示 3 号设备的称呼。
    """
    return await _record_preference(home_id, pref_key, pref_value)


def build_chat_graph(
    chat_model=None, extra_tools_provider: Callable[[], Awaitable[list]] | None = None
):
    """构建 chat 子图：reply ⇄ tools 循环，直到模型给出最终回复。

    chat_model 供测试注入桩模型；生产路径按需构建真实模型。
    extra_tools_provider 返回动态发现的 MCP 工具（第 6 步），None 表示不挂载。
    """

    @observe(as_type="generation", name="chat-reply")
    async def _invoke_reply(bound_model, prompt_messages: list, config=None):
        response = await bound_model.ainvoke(prompt_messages, config=config)
        # with_structured_output / bind_tools 不回填模型名，这里手动补上。
        get_client().update_current_generation(
            model=get_settings().light_model or "claude-sonnet-4-5"
        )
        return response

    async def reply(state: ChatState, config: RunnableConfig) -> dict:
        model = chat_model or build_chat_model()
        tools = [record_preference]
        if extra_tools_provider is not None:
            tools += list(await extra_tools_provider())
        bound = model.bind_tools(tools)
        system = SystemMessage(
            content=_CHAT_SYSTEM_PROMPT.format(
                home_summary=summarize_home_context(state["home_context"])
            )
        )
        response = await _invoke_reply(
            bound, [system, *state["messages"]], config=config
        )
        return {"messages": [response]}

    async def run_tools(state: ChatState, config) -> dict:
        """执行最后一条 AI 消息里的全部工具调用（含动态发现的 MCP 工具）。

        单个工具失败只把错误文本回给模型，不中断整个对话。
        """
        tools = [record_preference]
        if extra_tools_provider is not None:
            tools += list(await extra_tools_provider())
        available = {t.name: t for t in tools}

        outputs = []
        for call in getattr(state["messages"][-1], "tool_calls", None) or []:
            selected = available.get(call["name"])
            if selected is None:
                outputs.append(
                    ToolMessage(
                        content=f"未知工具：{call['name']}", tool_call_id=call["id"]
                    )
                )
                continue
            try:
                call_args = dict(call["args"])
                # ToolNode 会注入 InjectedState 字段；手写节点需自行补齐。
                # 本周注入字段仅 home_id，来自图状态，模型不可见不可改。
                if (
                    "home_id" in selected.args_schema.model_fields
                    and "home_id" not in call_args
                ):
                    call_args["home_id"] = state["home_id"]
                result = await selected.ainvoke(call_args)
                outputs.append(
                    ToolMessage(content=str(result), tool_call_id=call["id"])
                )
            except Exception as exc:  # noqa: BLE001
                # Server 崩溃等失败不中断对话：错误文本作为工具结果回给模型。
                outputs.append(
                    ToolMessage(content=f"工具调用失败：{exc}", tool_call_id=call["id"])
                )
        return {"messages": outputs}

    def _after_reply(state: ChatState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder = StateGraph(ChatState)
    builder.add_node("reply", reply)
    builder.add_node("tools", run_tools)
    builder.add_edge(START, "reply")
    builder.add_conditional_edges("reply", _after_reply, ["tools", END])
    builder.add_edge("tools", "reply")
    return builder.compile(checkpointer=MemorySaver())


def build_agent_graph(router_model=None, chat_model=None, extra_tools_provider=None):
    """构建 Agent 主图；两个模型参数仅供测试注入桩。"""
    chat_graph = build_chat_graph(chat_model, extra_tools_provider)

    async def load_context(state: AgentState) -> dict:
        async with session_factory() as session:
            context = await resolve_home_context(state["home_id"], session)
        return {"home_context": context}

    def route(state: AgentState) -> dict:
        intent = classify_intent(
            state["messages"], state["home_context"], chat_model=router_model
        )
        return {"intent": intent}

    def _dispatch(state: AgentState) -> str:
        return "chat" if state["intent"] == "chat" else "direct_reply"

    def to_answer(state: AgentState) -> dict:
        # chat 路径必然以模型最终回复结尾，取最后一条消息正文即可。
        return {"answer": extract_text_content(state["messages"][-1].content)}

    def direct_reply(state: AgentState) -> dict:
        return {
            "answer": reply_for_intent(state["intent"], state["home_context"]) or ""
        }

    builder = StateGraph(AgentState)
    builder.add_node("load_context", load_context)
    builder.add_node("route", route)
    # 编译后的子图直接作为节点：messages / home_context 走共享 channel。
    builder.add_node("chat", chat_graph)
    builder.add_node("to_answer", to_answer)
    builder.add_node("direct_reply", direct_reply)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "route")
    builder.add_conditional_edges("route", _dispatch, ["chat", "direct_reply"])
    builder.add_edge("chat", "to_answer")
    builder.add_edge("to_answer", END)
    builder.add_edge("direct_reply", END)
    return builder.compile(checkpointer=MemorySaver())


# 供接口层（第 7 步 chat 路由）直接导入使用。
agent_graph = build_agent_graph(extra_tools_provider=get_light_tools)
