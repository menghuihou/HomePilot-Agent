"""chat 子图：带偏好记录工具的多轮对话，以及 Agent 主图编排。

主图流程：load_context -> route ->（chat 子图 | 诊断子图 | direct_reply）
-> to_answer。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
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
from app.agent.diagnose import build_checkpoint_pool, build_diagnose_graph
from app.agent.inspect import build_inspect_graph, detect_report_type
from app.agent.router import classify_intent, reply_for_intent, summarize_home_context
from app.agent.state import AgentState, last_human_message
from app.database import session_factory
from app.llm_client import build_chat_model, default_model_name, extract_text_content
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

logger = logging.getLogger(__name__)

# 工具元数据显式声明哪些参数来自图状态；run_tools 不再通过
# 参数名猜测注入点，避免 MCP 工具合法自带同名参数时产生歧义。
_STATE_INJECTION_KEY = "inject_from_state"


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


# InjectedState 已让 home_id 从 LLM tool_call schema 中排除；元数据
# 再向手写执行节点声明注入来源，保持状态注入约定显式可见。
record_preference.metadata = {_STATE_INJECTION_KEY: ("home_id",)}


def build_chat_graph(
    chat_model=None,
    extra_tools_provider: Callable[[], Awaitable[list]] | None = None,
    checkpointer=None,
):
    """构建 chat 子图：reply ⇄ tools 循环，直到模型给出最终回复。

    chat_model 供测试注入桩模型；生产路径按需构建真实模型。
    extra_tools_provider 返回动态发现的 MCP 工具（第 6 步），None 表示不挂载。
    checkpointer=None 时子图作为节点挂进主图直接继承主图持久化，
    与 diagnose / inspect 子图保持同一生命周期口径。
    """

    @observe(as_type="generation", name="chat-reply")
    async def _invoke_reply(bound_model, prompt_messages: list, config=None):
        response = await bound_model.ainvoke(prompt_messages, config=config)
        # with_structured_output / bind_tools 不回填模型名，这里手动补上。
        get_client().update_current_generation(model=default_model_name())
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
                for field in (selected.metadata or {}).get(_STATE_INJECTION_KEY, ()):
                    call_args[field] = state[field]
                result = await selected.ainvoke(call_args)
                outputs.append(
                    ToolMessage(content=str(result), tool_call_id=call["id"])
                )
            except Exception as exc:
                # Server 崩溃等失败不中断对话；本地日志兜底观测链路不可用的场景。
                logger.warning("MCP 工具 %s 调用失败", call["name"], exc_info=True)
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
    return builder.compile(checkpointer=checkpointer)


def build_agent_graph(
    router_model=None,
    chat_model=None,
    extra_tools_provider=None,
    diagnose_graph=None,
    inspect_graph=None,
    checkpointer=None,
):
    """构建 Agent 主图；模型与子图参数仅供测试注入桩实现。

    diagnose_graph 缺省按项目默认构建且不带自有 checkpointer：
    作为节点挂进主图后继承主图 checkpointer，审批 interrupt 由主图
    统一持久化（第 9/10 步的跨进程续跑依赖这一点）。
    """
    chat_graph = build_chat_graph(chat_model, extra_tools_provider)
    diagnose_graph = diagnose_graph or build_diagnose_graph()
    # 巡检子图缺省不带自有 checkpointer：继承主图持久化（与 diagnose 同口径）。
    inspect_graph = inspect_graph or build_inspect_graph()

    async def load_context(state: AgentState) -> dict:
        async with session_factory() as session:
            context = await resolve_home_context(state["home_id"], session)
        return {"home_context": context}

    async def route(state: AgentState) -> dict:
        intent = await classify_intent(
            state["messages"], state["home_context"], chat_model=router_model
        )
        _route_observation(intent, state["home_id"])
        updates: dict = {"intent": intent}
        if intent == "inspect":
            # 话术细分（第 9 步口径）：日报请求升格为 daily_report 模式，
            # 巡检子图才会把待确认操作与提醒拼进报告（确定性关键词，零 LLM）。
            human_message = last_human_message(state["messages"])
            report_type = detect_report_type(
                human_message.content if human_message else ""
            )
            updates["report_type"] = report_type
        return updates

    @observe(name="agent-route")
    def _route_observation(intent: str, home_id: int) -> dict:
        """观测点：意图分发结果，供按意图筛选 trace。"""
        return {"intent": intent, "home_id": home_id}

    def _dispatch(state: AgentState) -> str:
        if state["intent"] == "chat":
            return "chat"
        if state["intent"] == "diagnose":
            return "diagnose"
        if state["intent"] == "inspect":
            return "inspect"
        return "direct_reply"

    def to_answer(state: AgentState) -> dict:
        # chat 路径必然以模型最终回复结尾，取最后一条消息正文即可。
        return {"answer": extract_text_content(state["messages"][-1].content)}

    @observe(name="direct-reply")
    def _direct_reply_observation(intent: str, reply: str) -> str:
        """直答回复的观测点：非 LLM 路径的回复也进入当前请求 trace。

        intent 不参与回复生成，仅作为观测入参留在 Langfuse input 里，
        便于按意图筛选直答记录。
        """
        return reply

    def direct_reply(state: AgentState) -> dict:
        reply = _direct_reply_observation(
            state["intent"],
            reply_for_intent(state["intent"], state["home_context"]) or "",
        )
        return {
            "answer": reply,
            # 直答也追加 AI 消息：保持历史 human/ai 交替，避免连续
            # human 消息干扰 Router 与 chat 子图的上下文。
            "messages": [AIMessage(content=reply)],
        }

    builder = StateGraph(AgentState)
    builder.add_node("load_context", load_context)
    builder.add_node("route", route)
    # 编译后的子图直接作为节点：messages / home_context 走共享 channel。
    builder.add_node("chat", chat_graph)
    builder.add_node("diagnose", diagnose_graph)
    builder.add_node("inspect", inspect_graph)
    builder.add_node("to_answer", to_answer)
    builder.add_node("direct_reply", direct_reply)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "route")
    builder.add_conditional_edges(
        "route", _dispatch, ["chat", "diagnose", "inspect", "direct_reply"]
    )
    builder.add_edge("chat", "to_answer")
    builder.add_edge("diagnose", "to_answer")
    builder.add_edge("inspect", "to_answer")
    builder.add_edge("to_answer", END)
    builder.add_edge("direct_reply", END)
    # 缺省 MemorySaver 保持单测/本地行为不变；生产路径显式传
    # AsyncPostgresSaver（见 get_agent_graph）实现审批跨进程续跑。
    return builder.compile(checkpointer=checkpointer or MemorySaver())


# chat 主图唯一提供者：诊断挂载与审批恢复必须共用本实例——
# interrupt 状态存在发起诊断的 checkpointer 里，恢复接口换实例会
# 查不到 thread 导致审批死锁（第 8 步审查确认的架构不变量）。
_agent_graph_singleton = None


async def get_agent_graph():
    """惰性构建主图；checkpointer 为 AsyncPostgresSaver（审批跨进程续跑）。"""
    global _agent_graph_singleton
    if _agent_graph_singleton is None:
        # 延迟导入：CRUD 等非 chat 路径无需加载 checkpointer 依赖。
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        pool = await build_checkpoint_pool()
        saver = AsyncPostgresSaver(pool)
        # LangGraph 内部表（checkpoints 等），setup 幂等，独立于 Alembic。
        await saver.setup()
        _agent_graph_singleton = build_agent_graph(
            extra_tools_provider=get_light_tools,
            # 诊断子图不带自有 checkpointer：继承主图 saver，审批状态同源。
            diagnose_graph=build_diagnose_graph(),
            # 巡检走同一 LLM 转写能力（模板兜底），checkpointer 继承主图。
            inspect_graph=build_inspect_graph(chat_model=build_chat_model()),
            checkpointer=saver,
        )
    return _agent_graph_singleton
