"""家庭模式子图：模式迁移 plan → dry_run → gate → execute → readback。

挂载与安全口径：
- 直挂主图（与 diagnose 同款）：messages / home_id 共享 channel，迁移
  过程状态（planned_changes 等）为子图私有，不回流主图；
- 风险分级 / 冲突检测 / 静音策略 / 回读汇总全部复用 home_mode.py
  纯函数与 risk_rules 规则表，确定性优先——模式槽位抽取也用确定性
  关键词（「我出门了」「准备睡觉」这类指令关键词完全可枚举，不引入
  LLM 不确定性）；LLM 只做结果摘要转写，且失败降级为确定性模板；
- 高风险动作（如离家落锁）在 gate 节点 interrupt 挂起，审批审计行
  与恢复协议复用 diagnose 同款（approvals API 按 thread 通用恢复）；
- 空调域 MCP Server（第 6 步交付）未就位前，对应动作按执行失败
  兜底并进入未完成清单，Agent 侧零改动。
"""

import functools
import json
import logging
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from opentelemetry.trace import get_tracer
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent.context import DEFAULT_HOME_MODE, HOME_MODE_KEY
from app.agent.diagnose import (
    # 审批审计与恢复协议与 diagnose 同款：私有助手同包内复用，
    # approvals API 按 thread 恢复对两类子图通用。
    _ensure_pending_approval,
    _record_approval_decision,
    approval_allows_execution,
)
from app.agent.home_mode import (
    # 确定性核心全部来自纯函数模块；_device_id_for_risk 供 gate 复审
    # 代表性高风险动作时复用同一 id 归一口径。
    _device_id_for_risk,
    apply_sleep_mute_policy,
    detect_conflicts,
    plan_mode_transition,
    requires_approval,
    summarize_readback,
    unmatched_exclusions,
)
from app.agent.risk_rules import judge_risk
from app.agent.state import last_human_message
from app.database import session_factory
from app.llm_client import build_chat_model, extract_text_content
from app.mcp.client import fetch_device_status, get_ac_tools, get_light_tools
from app.models import Device, HomeModeRecord, HomePolicy, Preference

logger = logging.getLogger(__name__)

MODE_LABELS = {"home": "在家", "away": "离家", "night": "夜间", "sleep": "睡眠"}

# 确定性模式槽位抽取：指令关键词可完全枚举，按声明顺序首个命中生效
# （「我出门了」命中 away，不会被末尾的「家」抢走）。
_MODE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("away", ("出门", "离家", "离开")),
    ("sleep", ("睡觉", "睡眠", "入睡")),
    ("night", ("夜间", "夜晚模式", "晚上模式")),
    ("home", ("回家", "到家", "回来")),
)

_SUMMARY_SYSTEM_PROMPT = """你是家庭模式迁移的结果转写器。给你一份结构化迁移结果（JSON），把它转写成给用户看的中文摘要。

规则：
- 只转写给定信息，不得新增、删除任何设备或动作；
- 待确认与未生效项必须如实列出设备与原因；
- 全文 100 字以内，先说结果，再说待确认 / 未完成项。"""


# 节点级 OTel tracer：经 observability.py 的全局 TracerProvider（OTLP）
# 导出到 Langfuse，start_as_current_span 挂到环境请求上下文——节点在
# 请求 trace 树中嵌套展示。不用 langfuse @observe：其对 langgraph 节点
# 会脱离请求上下文，每个节点变成独立根 trace（2026-11-19 trace 审查实证）。
_tracer = get_tracer("home-pilot.home-mode")


def _traced(name: str):
    """节点装饰器：为节点执行建一个 OTel span（异常自动记录到 span）。"""

    def _deco(fn):
        @functools.wraps(fn)
        async def _wrapped(state, *args, **kwargs):
            with _tracer.start_as_current_span(name):
                return await fn(state, *args, **kwargs)

        return _wrapped

    return _deco


async def _fetch_status_for(device_type: str, device_id: int) -> dict:
    """按设备域路由状态查询；域未接入时向上抛，由调用方离线兜底。

    状态源口径：light → 通用 get_device_status；ac → 空调域
    get_ac_status（第 6 步交付，交付前启动失败按离线兜底）。
    """
    if device_type == "ac":
        tools = await get_ac_tools()
        tool = {item.name: item for item in tools}.get("get_ac_status")
        if tool is None:
            return {"error": "ac 域状态工具 get_ac_status 不可用"}
        result = await tool.ainvoke({"device_id": str(device_id)})
        return result if isinstance(result, dict) else {"error": "MCP 返回结构异常"}
    return await fetch_device_status(device_id)


class HomeModeState(TypedDict):
    """模式子图状态：messages / home_id 与主图共享，其余子图私有。

    home_context 挂载路径下由主图 load_context 提供，current_mode 优先
    取自它；独立运行（测试）时回退查偏好表。
    """

    messages: Annotated[list, add_messages]
    home_id: int
    home_context: Any
    current_mode: str
    target_mode: str | None
    # 非迁移出口的确定性回复文本（查询 / 策略缺失 / 无可执行 / 已取消）。
    mode_reply: str | None
    planned_changes: list[dict]
    blocked_changes: list[dict]
    # plan 产出传递给 dry_run / gate 的契约数据（子图内部流转）。
    current_states: dict
    device_rules: dict
    policy_warnings: list[str]
    approval_decision: str
    scope: dict
    execution_result: list[dict]
    unresolved_devices: list[dict]
    rollback_plan: dict | None
    mode_summary: dict
    answer: str


def _match_target_mode(text: str) -> str | None:
    """确定性模式抽取：关键词首个命中；无命中表示查询当前模式。"""
    for mode, keywords in _MODE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return mode
    return None


async def _read_current_mode(home_id: int, state: dict) -> str:
    """当前模式：优先主图已加载的 home_context，独立运行回退偏好表。"""
    context = state.get("home_context")
    if context is not None:
        return context.current_mode
    async with session_factory() as session:
        result = await session.execute(
            select(Preference.pref_value).where(
                Preference.home_id == home_id,
                Preference.pref_key == HOME_MODE_KEY,
            )
        )
        return result.scalars().first() or DEFAULT_HOME_MODE


def _render_summary(summary: dict) -> str:
    """确定性摘要模板：LLM 转写失败时的兜底口径。"""
    label = MODE_LABELS.get(summary["target_mode"], summary["target_mode"])
    lines = [
        f"已切换到「{label}」模式：{summary['applied_count']}/{summary['total_count']} 项动作生效。"
    ]
    if summary["blocked"]:
        names = "、".join(item["device_name"] for item in summary["blocked"])
        lines.append(f"{len(summary['blocked'])} 项待确认：{names}。")
    if summary["unresolved"]:
        names = "、".join(item["device_name"] for item in summary["unresolved"])
        lines.append(f"{len(summary['unresolved'])} 项未生效：{names}。")
    return "".join(lines)


def _render_blocked_reply(blocked: list[dict]) -> str:
    """全部动作被拦时的确定性回复：按原因分组、逐台列出设备。

    只报数量会让用户无法判断「待确认的到底是什么」；reason 文案本身
    是确定性契约（golden 钉住），这里原样透传不二次加工。
    """
    by_reason: dict[str, list[str]] = {}
    for item in blocked:
        name = str(item.get("device_name") or item.get("device_id") or "未知设备")
        reason = str(item.get("reason") or "原因未知")
        by_reason.setdefault(reason, []).append(name)
    details = "；".join(
        f"{'、'.join(names)}（{reason}）" for reason, names in by_reason.items()
    )
    return (
        f"本次迁移有 {len(blocked)} 项被暂停待确认：{details}。暂无可自动执行的动作。"
    )


async def _default_summarizer(summary: dict, chat_model=None) -> str:
    """默认摘要转写：LLM 转写，异常向上抛由调用方降级模板。"""
    model = chat_model or build_chat_model()
    messages = [
        SystemMessage(content=_SUMMARY_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(summary, ensure_ascii=False)),
    ]
    response = await model.ainvoke(messages)
    return extract_text_content(response.content)


def _build_summarizer(chat_model=None, summarizer=None):
    """组装摘要器：注入桩优先；默认 LLM 转写 + 确定性模板降级。"""
    if summarizer is not None:
        return summarizer

    async def _summarize(summary: dict) -> str:
        try:
            return await _default_summarizer(summary, chat_model=chat_model)
        except Exception:
            # 摘要属展示层：LLM 故障不吞迁移结果，降级确定性模板。
            logger.warning("模式迁移摘要 LLM 转写失败，已降级模板", exc_info=True)
            return _render_summary(summary)

    return _summarize


# 动作 → (域, MCP 工具名) 注册表：单一事实来源。灯光域已接入；空调域
# 指向第 6 步交付的 ac_server.py（就位前启动失败按执行失败兜底）。
# 亮度字段由灯光工具按默认亮度处理，Server MVP 不支持精确调光。
_ACTION_TOOL_BY_ID: dict[str, tuple[str, str]] = {
    "turn_light_on": ("light", "turn_light_on"),
    "turn_light_off": ("light", "turn_light_off"),
    "turn_ac_on": ("ac", "set_ac_power"),
    "turn_ac_off": ("ac", "set_ac_power"),
    "set_ac_temperature": ("ac", "set_temperature"),
}


def _build_tool_args(action: dict) -> dict:
    """按动作 id 构造 MCP 工具参数；id 统一转字符串（server 按主键查询）。"""
    device_id = str(action.get("device_id"))
    action_id = action.get("action")
    if action_id == "turn_ac_on":
        return {"device_id": device_id, "on": True}
    if action_id == "turn_ac_off":
        return {"device_id": device_id, "on": False}
    if action_id == "set_ac_temperature":
        return {
            "device_id": device_id,
            "temperature": action.get("target", {}).get("temperature"),
        }
    return {"device_id": device_id}


async def _default_execute_action(action: dict) -> dict:
    """默认执行器：查注册表分发 MCP 工具；故障统一降级为失败结果。

    MCP Server 返回结构化错误（dict 带 error）也视为失败——不冒充成功，
    失败动作由 readback 转入未完成清单如实呈现。
    """
    action_id = action.get("action")
    spec = _ACTION_TOOL_BY_ID.get(action_id)
    if spec is None:
        return {"ok": False, "detail": f"动作 {action_id} 暂无执行器，已跳过"}
    domain, tool_name = spec
    try:
        tools = await get_light_tools() if domain == "light" else await get_ac_tools()
        tool = {item.name: item for item in tools}.get(tool_name)
        if tool is None:
            return {"ok": False, "detail": f"{domain} 域工具 {tool_name} 不可用"}
        result = await tool.ainvoke(_build_tool_args(action))
    except Exception:
        logger.warning("模式迁移动作 %s 执行失败", action_id, exc_info=True)
        return {"ok": False, "detail": "工具暂不可用，执行失败"}
    if isinstance(result, dict) and "error" in result:
        return {"ok": False, "detail": str(result["error"])}
    return {"ok": True, "detail": json.dumps(result, ensure_ascii=False, default=str)}


def _build_state_loader():
    """当前状态装配：DB 设备清单 + 按域路由的 MCP 状态归一。

    状态携带的 power / temperature 透传给纯函数——幂等跳过与回读比对
    共用同一事实源，本层不合成、不编造；light 的 brightness 额外归一
    出 power 布尔。
    """

    async def _fetch_states(devices: list[Device]) -> dict[str, dict]:
        states: dict[str, dict] = {}
        for device in devices:
            try:
                status = await _fetch_status_for(device.device_type, device.id)
            except Exception:  # noqa: BLE001 —— MCP 故障降级为离线兜底，不中断整次迁移
                # 查询失败 fail-safe 视同离线：状态未知设备不允许迁移动作，
                # 由 detect_conflicts 拦下待确认。
                logger.warning("设备 %s 状态查询失败，按离线兜底", device.id)
                status = {"online": False}
            state = {
                "device_id": str(device.id),
                "name": device.name,
                "device_type": device.device_type,
                "online": bool(status.get("online")),
            }
            # light 域归一：server 输出 brightness，补 power 布尔供纯函数比对。
            if "brightness" in status:
                state["brightness"] = status["brightness"]
                state["power"] = status["brightness"] > 0
            if "power" in status:
                # 状态源自带 power（如 ac 域）：透传并保持布尔归一。
                state["power"] = bool(status["power"])
            if status.get("temperature") is not None:
                state["temperature"] = status["temperature"]
            states[str(device.id)] = state
        return states

    return _fetch_states


def build_home_mode_graph(
    chat_model=None,
    summarizer=None,
    executor=None,
    checkpointer=None,
):
    """构建模式子图；summarizer / executor 供测试注入桩实现。

    summarizer 签名 async (summary: dict) -> str；executor 签名
    async (action: dict) -> dict（返回 {ok, detail}）。checkpointer=None
    时子图作为节点挂进主图直接继承主图持久化，interrupt 由主图统一保存。
    """
    summarize = _build_summarizer(chat_model, summarizer)
    run_action = executor or _default_execute_action
    fetch_states = _build_state_loader()

    @_traced(name="home-mode-extract")
    async def _extract(state: dict) -> dict:
        message = last_human_message(state["messages"])
        target = _match_target_mode(str(message.content)) if message else None
        updates: dict = {"target_mode": target}
        if target is None:
            # 查询出口：与第 2 周占位口径同文，替换占位实现。
            current = await _read_current_mode(state["home_id"], state)
            updates["current_mode"] = current
            updates["mode_reply"] = f"当前家庭模式是：{current}。"
        return updates

    @_traced(name="home-mode-plan")
    async def _plan(state: dict) -> dict:
        home_id = state["home_id"]
        target = state["target_mode"]
        async with session_factory() as session:
            policy_row = (
                (
                    await session.execute(
                        select(HomePolicy).where(
                            HomePolicy.home_id == home_id,
                            HomePolicy.mode == target,
                            HomePolicy.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .first()
            )
            devices = (
                (
                    await session.execute(
                        select(Device)
                        .where(Device.home_id == home_id)
                        .order_by(Device.id)
                    )
                )
                .scalars()
                .all()
            )
        if policy_row is None:
            label = MODE_LABELS.get(target, target)
            return {
                "mode_reply": f"还没有配置「{label}」模式的迁移策略，请先在策略表中添加。"
            }
        # 范围口径：本家庭全部设备都在授权清单内（越界动作由纯函数阻断）。
        scope = {"authorized_device_ids": [device.id for device in devices]}
        current_states = await fetch_states(devices)
        planned, scope_blocked = plan_mode_transition(
            policy_row.device_rules, current_states, scope
        )
        return {
            "planned_changes": planned,
            "blocked_changes": scope_blocked,
            "current_states": current_states,
            "device_rules": policy_row.device_rules,
            "policy_warnings": unmatched_exclusions(
                policy_row.device_rules, current_states
            ),
            "scope": scope,
        }

    @_traced(name="home-mode-dry-run")
    async def _dry_run(state: dict) -> dict:
        current_states = state.get("current_states") or {}
        rules = state.get("device_rules") or {}
        planned = list(state.get("planned_changes") or [])
        blocked = list(state.get("blocked_changes") or [])
        planned, conflict_blocked = detect_conflicts(planned, current_states, rules)
        blocked = blocked + conflict_blocked
        if state.get("target_mode") == "sleep":
            planned, muted = apply_sleep_mute_policy(planned, rules)
            blocked = blocked + muted
        updates: dict = {"planned_changes": planned, "blocked_changes": blocked}
        if not planned:
            # 无可执行动作：冲突 / 离线 / 静音全拦，或全部已达标（幂等）。
            if blocked:
                updates["mode_reply"] = _render_blocked_reply(blocked)
            else:
                updates["mode_reply"] = "所有设备都已处于目标状态，无需调整。"
        return updates

    @_traced(name="home-mode-gate")
    async def _gate(state: dict, config) -> dict:
        planned = list(state.get("planned_changes") or [])
        thread_id = (config or {}).get("configurable", {}).get("thread_id", "")
        if not requires_approval(planned):
            return {"approval_decision": "auto"}
        # 代表性动作复审：迁移批次的审批以首个需审批动作为代表（如离家
        # 落锁），确定性重判保证恢复重跑幂等（同 diagnose 门控口径）。
        representative = next(a for a in planned if a.get("require_approval"))
        # 目标设备归一一次、两处共用：judge_risk 做范围校验，审计行记录
        # 设备级目标（家庭级动作为 None，如离家落锁）。
        target_id = _device_id_for_risk(representative.get("device_id"))
        decision_info = judge_risk(
            representative["action"],
            target_id,
            state.get("scope") or {},
        )
        approval_id = await _ensure_pending_approval(
            thread_id, state["home_id"], decision_info, target_id
        )
        resume_payload = interrupt(
            {
                "type": "approval_required",
                "thread_id": thread_id,
                "approval_id": approval_id,
                "action": representative["action"],
                "risk_level": decision_info.risk_level,
                "reason": decision_info.reason,
            }
        )
        decision = resume_payload.get("decision")
        await _record_approval_decision(
            approval_id, decision, resume_payload.get("reason")
        )
        if approval_allows_execution(decision):
            return {"approval_decision": "approved"}
        return {
            "approval_decision": decision or "rejected",
            "mode_reply": "已取消本次模式迁移，家庭保持当前模式。",
        }

    @_traced(name="home-mode-execute")
    async def _execute(state: dict) -> dict:
        results = []
        for action in state.get("planned_changes") or []:
            outcome = await run_action(action)
            results.append(
                {
                    "device_id": action.get("device_id"),
                    "action": action.get("action"),
                    "ok": bool(outcome.get("ok")),
                    "detail": outcome.get("detail"),
                }
            )
        return {"execution_result": results}

    @_traced(name="home-mode-readback")
    async def _readback(state: dict) -> dict:
        planned = list(state.get("planned_changes") or [])
        target = state["target_mode"]
        # 家庭级动作（lock_door，无 device_id）不参与状态回读：成功与否
        # 以执行器结果为准，状态回读只覆盖有实体设备的动作。
        device_planned = [a for a in planned if a.get("device_id") is not None]
        readback_states: dict[str, dict] = {}
        for action in device_planned:
            device_id = str(action["device_id"])
            try:
                status = await _fetch_status_for(
                    str(action.get("device_type")), int(device_id)
                )
            except Exception:  # noqa: BLE001 —— 回读失败按未生效如实呈现，不静默成功
                logger.warning("回读设备 %s 状态失败", device_id)
                status = {}
            if "brightness" in status:
                status = {**status, "power": status["brightness"] > 0}
            readback_states[device_id] = status

        summary = summarize_readback(device_planned, readback_states)
        execution_result = list(state.get("execution_result") or [])
        failed = [item for item in execution_result if not item["ok"]]
        summary_dict = {
            "target_mode": target,
            "total_count": len(planned),
            "applied_count": len(summary["applied"]),
            "blocked": state.get("blocked_changes") or [],
            "unresolved": summary["unresolved"],
            "failed": failed,
        }

        # 持久化：关闭历史未结束记录 → 写入新模式记录 → 同步偏好
        # （home_mode 偏好是 Router / 上下文摘要的 current_mode 数据源）。
        message = last_human_message(state["messages"])
        async with session_factory() as session:
            await session.execute(
                update(HomeModeRecord)
                .where(
                    HomeModeRecord.home_id == state["home_id"],
                    HomeModeRecord.ended_at.is_(None),
                )
                .values(ended_at=func.now())
            )
            session.add(
                HomeModeRecord(
                    home_id=state["home_id"],
                    mode=target,
                    state={
                        "planned_changes": planned,
                        "blocked_changes": state.get("blocked_changes") or [],
                        "execution_result": execution_result,
                        "unresolved": summary["unresolved"],
                        "rollback_plan": summary["rollback_plan"],
                    },
                    reason=message.content if message else None,
                )
            )
            await session.execute(
                pg_insert(Preference)
                .values(
                    home_id=state["home_id"],
                    member_id=None,
                    pref_key=HOME_MODE_KEY,
                    pref_value=target,
                )
                .on_conflict_do_update(
                    constraint="uq_preferences_home_member_key",
                    set_={"pref_value": target, "updated_at": func.now()},
                )
            )
            await session.commit()

        try:
            text = await summarize(summary_dict)
        except Exception:
            logger.warning("模式迁移摘要失败，已降级确定性模板", exc_info=True)
            text = _render_summary(summary_dict)
        return {
            "unresolved_devices": summary["unresolved"],
            "rollback_plan": summary["rollback_plan"],
            "mode_summary": summary_dict,
            "answer": text,
            "messages": [AIMessage(content=text)],
        }

    def _reply(state: dict) -> dict:
        text = state.get("mode_reply") or ""
        return {"answer": text, "messages": [AIMessage(content=text)]}

    def _after_extract(state: dict) -> str:
        return "reply" if state.get("mode_reply") else "plan"

    def _after_plan(state: dict) -> str:
        return "reply" if state.get("mode_reply") else "dry_run"

    def _after_dry_run(state: dict) -> str:
        return "reply" if state.get("mode_reply") else "gate"

    def _after_gate(state: dict) -> str:
        return "reply" if state.get("mode_reply") else "execute"

    builder = StateGraph(HomeModeState)
    builder.add_node("extract", _extract)
    builder.add_node("plan", _plan)
    builder.add_node("dry_run", _dry_run)
    builder.add_node("gate", _gate)
    builder.add_node("execute", _execute)
    builder.add_node("readback", _readback)
    builder.add_node("reply", _reply)
    builder.add_edge(START, "extract")
    builder.add_conditional_edges("extract", _after_extract, ["plan", "reply"])
    builder.add_conditional_edges("plan", _after_plan, ["dry_run", "reply"])
    builder.add_conditional_edges("dry_run", _after_dry_run, ["gate", "reply"])
    builder.add_conditional_edges("gate", _after_gate, ["execute", "reply"])
    builder.add_edge("execute", "readback")
    builder.add_edge("readback", END)
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=checkpointer)
