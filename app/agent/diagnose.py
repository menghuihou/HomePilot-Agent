"""诊断子图：症状抽取 → 候选假设 → 分层排查 → 结论 → 审批门控 → 报告。

已交付：DiagnoseState、症状抽取、候选假设、分层排查（3 轮硬上限）、
确定性结论、报告转写与措辞过滤、审批门控（interrupt/Command）。

序列化约束：DiagnoseState 全部字段为 JSON 原生类型，集合语义用
list 表达，Pydantic 对象 model_dump 后再入状态，保证 Postgres
checkpointer 断点续跑可序列化。
"""

import asyncio
import json
import logging
import operator
from collections.abc import Awaitable, Callable
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langfuse import get_client, observe
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel
from sqlalchemy import func, select

from app.agent.hypotheses import (
    build_final_diagnosis,
    compute_hypotheses,
    high_confidence_count,
    select_actions,
    should_conclude,
    shrink,
)
from app.agent.risk_rules import judge_risk
from app.agent.state import last_human_message
from app.agent.wording import apply_wording_filter as _apply_wording_filter
from app.config import get_settings
from app.database import session_factory
from app.llm_client import build_chat_model, default_model_name
from app.mcp.client import fetch_device_status, get_light_tools
from app.models import (
    Approval,
    Device,
    DeviceEvent,
    FaultCause,
    FaultSymptom,
    Preference,
    SymptomCause,
)

logger = logging.getLogger(__name__)

# devices.device_type → 知识库设备域；thermostat 归入空调域（种子历史口径）。
DOMAIN_BY_DEVICE_TYPE = {
    "light": "light",
    "thermostat": "ac",
    "ac": "ac",
    "sensor": "sensor",
    "gateway": "gateway",
}

# 症状抽取失败或 key 无法验证时的通用兜底症状；四个设备域各有一条种子数据，
# 且映射到本域全部原因，保证兜底后候选假设仍不为空。
FALLBACK_SYMPTOM_KEY = "device_abnormal"

# 设备离线证据在各域症状字典中的 key（网关域用 gw_ 前缀）。
OFFLINE_SYMPTOM_BY_DOMAIN = {
    "light": "light_offline",
    "ac": "ac_offline",
    "sensor": "sensor_offline",
    "gateway": "gw_offline",
}

_EXTRACTION_SYSTEM_PROMPT = """你是家庭智能助手的故障诊断前处理模块，只负责抽取症状槽位，不诊断、不给建议。

用户家庭中的设备（device_name 必须从下面选择，找不到就原样保留用户说法）：
{device_list}

症状字典（symptom_key 必须从下面选择，不要编造）：
{symptom_dict}
"""

_REPORT_SYSTEM_PROMPT = """你是诊断报告转写器。给你一份结构化诊断结论（JSON），把它转写成给用户看的中文诊断报告。

规则：
- 只转写给定结论与建议，不得新增、删除或改变任何结论；
- primary_cause_key 字段必须原样复述输入 JSON 中的同名取值（输入为
  null 则输出 null），不得改动或编造；
- 措辞使用「可能」「建议检查」「建议观察」等非断言表达，
  禁止出现「一定」「必然」「肯定是」「已修复」等过度断言词；
- 先说结论，再说依据证据，最后说建议，全文 150 字以内。"""


class DiagnoseState(TypedDict):
    """诊断子图状态：与主图共享 messages / home_id，其余为子图私有。

    - messages 用 add_messages 增量合并，与 AgentState 口径一致；
    - observed_evidence 跨排查轮累积，用 operator.add 拼接（去重交给消费方）。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    home_id: int
    device_topology: dict
    symptom: dict
    observed_evidence: Annotated[list[str], operator.add]
    candidate_hypotheses: list[dict]
    investigation_round: int
    risk_level: str
    scope: dict
    final_diagnosis: dict | None


class SymptomSlots(BaseModel):
    """症状抽取的结构化输出：设备、房间与症状字典 key。"""

    device_name: str
    room: str = ""
    symptom_key: str


class DiagnosisReport(BaseModel):
    """诊断报告的结构化输出：转写文本 + 复述主结论 key（供越权校验）。"""

    report: str
    primary_cause_key: str | None = None


def _format_symptom_dictionary(rows: list[FaultSymptom]) -> str:
    """把症状字典按设备域分组压缩成 prompt 可读的文本。"""
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row.device_type, []).append(
            f"{row.symptom_key}={row.description}"
        )
    return "\n".join(
        f"{domain}：{'；'.join(items)}" for domain, items in sorted(groups.items())
    )


async def _load_domain_knowledge(domain: str) -> tuple[list, list[tuple[str, str]]]:
    """加载本域原因与症状-原因映射对；未知域返回空数据。"""
    async with session_factory() as session:
        causes = (
            (
                await session.execute(
                    select(FaultCause).where(FaultCause.device_type == domain)
                )
            )
            .scalars()
            .all()
        )
        symptoms = (
            (
                await session.execute(
                    select(FaultSymptom).where(FaultSymptom.device_type == domain)
                )
            )
            .scalars()
            .all()
        )
        symptom_key_by_id = {s.id: s.symptom_key for s in symptoms}
        mappings = (
            (
                await session.execute(
                    select(SymptomCause).where(
                        SymptomCause.symptom_id.in_(symptom_key_by_id or {0})
                    )
                )
            )
            .scalars()
            .all()
        )

    cause_key_by_id = {c.id: c.cause_key for c in causes}
    pairs = [
        (symptom_key_by_id[m.symptom_id], cause_key_by_id[m.cause_id])
        for m in mappings
        if m.cause_id in cause_key_by_id
    ]
    return list(causes), pairs


async def _query_mcp_device_status(domain: str, device_id: int) -> dict | None:
    """MVP 仅灯光域接入 MCP 状态查询；其他域工具随第 6 周域扩展补齐。"""
    if domain != "light":
        return None
    try:
        result = await fetch_device_status(device_id)
    except asyncio.CancelledError as exc:
        # 拆成两个子句：避免依赖 PEP 758 的无括号多异常语法（3.14 专属，
        # 且与 ruff format 的规范化行为冲突）。CancelledError 非 Exception
        # 子类：跨事件循环复用 MCP 客户端时 anyio 会以取消形式报错，
        # 诊断链路不能被 MCP 故障打断。
        logger.warning("MCP 设备状态查询失败，本轮不产出证据", exc_info=exc)
        return None
    except Exception:
        logger.warning("MCP 设备状态查询失败，本轮不产出证据", exc_info=True)
        return None
    return result if isinstance(result, dict) else None


async def _check_device_status_evidence(domain: str, device_id: int) -> list[str]:
    """第一层：设备状态检查 → 离线观测转为本域离线证据。"""
    status = await _query_mcp_device_status(domain, device_id)
    if not isinstance(status, dict) or "error" in status:
        return []
    if status.get("online") is False:
        key = OFFLINE_SYMPTOM_BY_DOMAIN.get(domain)
        return [key] if key else []
    return []


async def _check_device_events_evidence(domain: str, device_id: int) -> list[str]:
    """第二层：事件检查 → 从未上报任何事件的设备等同离线（MVP 启发式）。"""
    async with session_factory() as session:
        latest = await session.execute(
            select(DeviceEvent.event_type)
            .where(DeviceEvent.device_id == device_id)
            .order_by(DeviceEvent.occurred_at.desc())
            .limit(1)
        )
    if latest.scalars().first() is None:
        key = OFFLINE_SYMPTOM_BY_DOMAIN.get(domain)
        return [key] if key else []
    return []


async def default_investigator(layer: str, symptom: dict, home_id: int) -> list[str]:
    """生产排查器：按层执行确定性检查，产出本域症状 key 形式的证据。

    测试注入桩排查器替换本实现；证据必须是本域症状字典内的 key，
    字典外的 token 在假设收缩时自然不命中任何候选。
    """
    device_id = symptom.get("device_id")
    if device_id is None:
        return []
    domain = symptom.get("domain", "unknown")
    if layer == "device_status":
        return await _check_device_status_evidence(domain, device_id)
    if layer == "device_events":
        return await _check_device_events_evidence(domain, device_id)
    # gateway_status 层：MCP 尚无网关状态查询工具（第 6 周域扩展补齐），
    # 网关假设当前靠 prevalence 排序进入结论候选。
    return []


async def _ensure_pending_approval(
    thread_id: str,
    home_id: int,
    decision_info,
    target_device_id: int | None,
) -> int:
    """确保该会话存在 pending 审批记录；恢复时节点重跑直接复用同一行。

    已知并发限制：同会话并发首次进入时，后到的 insert 会违反 pending
    唯一索引（不变量仍由数据库兜底）；MVP 单用户下不处理该竞态。
    """
    async with session_factory() as session:
        existing = (
            (
                await session.execute(
                    select(Approval).where(
                        Approval.thread_id == thread_id,
                        Approval.decision.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return existing.id
        approval = Approval(
            thread_id=thread_id,
            home_id=home_id,
            action=decision_info.action,
            target_device_id=target_device_id,
            risk_level=decision_info.risk_level,
            decision=None,
        )
        session.add(approval)
        await session.commit()
        return approval.id


async def _record_approval_decision(
    approval_id: int, decision: str, reason: str | None
) -> None:
    """决策后更新审批行；approvals 是审计基础数据，只更新不删除。"""
    async with session_factory() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            logger.warning("审批记录不存在：%s", approval_id)
            return
        approval.decision = decision
        approval.reason = reason
        approval.decided_at = func.now()
        await session.commit()


async def _record_auto_approval(
    thread_id: str,
    home_id: int,
    decision_info,
    target_device_id: int | None,
    decision: str,
) -> None:
    """低中风险自动放行与范围阻断的审计落库（decision=auto/blocked）。"""
    async with session_factory() as session:
        approval = Approval(
            thread_id=thread_id,
            home_id=home_id,
            action=decision_info.action,
            target_device_id=target_device_id,
            risk_level=decision_info.risk_level,
            decision=decision,
            reason=decision_info.reason,
            decided_at=func.now(),
        )
        session.add(approval)
        await session.commit()


async def _home_gateway_device_id(home_id: int) -> int | None:
    """解析家庭网关设备主键（重启网关的执行目标）。"""
    async with session_factory() as session:
        row = await session.execute(
            select(Device.id).where(
                Device.home_id == home_id,
                Device.device_type == "gateway",
            )
        )
        return row.scalars().first()


# 系统可自动执行的动作注册表：动作 → 执行器（单一事实来源）。
# 门控据此区分可执行动作与人工建议：不在集合内 = 人工建议类动作，
# 只作为报告建议呈现，不走审批门控。新增执行器用 @_registers 登记
# 一个函数即可，EXECUTABLE_ACTIONS 自动派生；动作仍需在
# app/agent/risk_rules.py 规则表登记——未登记的可执行动作会被
# fail-safe 判 high 走审批，安全兜底仍在。
_ACTION_EXECUTORS: dict[str, Callable[[dict], Awaitable[str]]] = {}


def _registers(action: str):
    """登记执行器；action 由此自动进入 EXECUTABLE_ACTIONS。"""

    def _deco(fn):
        _ACTION_EXECUTORS[action] = fn
        return fn

    return _deco


@_registers("restart_gateway")
async def _execute_restart_gateway(diagnosis: dict) -> str:
    """重启网关：网关 id 从业务库按家庭解析，传入 MCP 工具。"""
    gateway_id = await _home_gateway_device_id(diagnosis.get("home_id"))
    if gateway_id is None:
        return "执行失败：当前家庭未配置网关设备，请人工处理"
    tools = {tool.name: tool for tool in await get_light_tools()}
    tool = tools.get("restart_gateway")
    if tool is None:
        return "执行失败：重启网关工具不可用，请人工处理"
    result = await tool.ainvoke({"gateway_id": str(gateway_id)})
    return f"网关重启完成：{result}"


EXECUTABLE_ACTIONS = frozenset(_ACTION_EXECUTORS)


async def default_executor(action: str, diagnosis: dict) -> str:
    """MVP 执行器：查注册表分发；未登记动作如实提示需人工。

    执行失败不冒充成功——异常统一兜底为失败说明，由报告转述给用户；
    执行器只写快乐路径与资源缺失的优雅返回，无需各自处理异常。
    """
    executor = _ACTION_EXECUTORS.get(action)
    if executor is None:
        return f"动作 {action} 暂无自动执行器，建议已记录，请人工处理"
    try:
        return await executor(diagnosis)
    except asyncio.CancelledError as exc:
        # 拆分理由同 _query_mcp_device_status：不依赖 3.14 专属语法。
        logger.warning("%s 执行失败", action, exc_info=exc)
        return "执行失败：工具暂不可用，请人工处理"
    except Exception:
        logger.warning("%s 执行失败", action, exc_info=True)
        return "执行失败：工具暂不可用，请人工处理"


def _hypotheses_top(hypotheses: list[dict], limit: int = 5) -> list[dict]:
    """候选假设摘要：观测入参/输出统一使用，避免 ORM/大对象进 trace。"""
    keys = (
        "cause_key",
        "cause_name",
        "prevalence",
        "matched_evidence_count",
        "matched_symptom_keys",
    )
    return [{key: h[key] for key in keys} for h in hypotheses[:limit]]


@observe(name="compute-hypotheses")
def _hypotheses_observation(
    domain: str, hypotheses: list[dict], round_index: int
) -> dict:
    """观测点：候选假设集与排序（评估口径与排查定位依据）。"""
    return {
        "domain": domain,
        "candidate_count": len(hypotheses),
        "round": round_index,
        "top": _hypotheses_top(hypotheses),
    }


@observe(name="investigate-round")
def _investigation_observation(
    round_index: int,
    layer: str,
    new_evidence: list[str],
    hypotheses: list[dict],
) -> dict:
    """观测点：每轮排查的层、新证据与收缩后领先组。"""
    return {
        "round": round_index,
        "layer": layer,
        "new_evidence": new_evidence,
        "leader_count": high_confidence_count(hypotheses),
        "top": _hypotheses_top(hypotheses, limit=3),
    }


@observe(name="conclude")
def _conclusion_observation(diagnosis: dict) -> dict:
    """观测点：最终结论摘要（状态、主结论、依据与建议）。"""
    keys = (
        "status",
        "primary_cause_key",
        "primary_cause_name",
        "matched_symptom_keys",
        "fix_action",
        "fix_suggestion",
        "investigation_rounds",
    )
    return {key: diagnosis.get(key) for key in keys}


@observe(name="approval-gate")
def _approval_observation(
    decision_info, decision: str, execution_result: str | None
) -> dict:
    """观测点：审批门控判定、用户决策与执行结果。"""
    if decision_info is None:
        return {
            "action": None,
            "risk_level": "low",
            "decision": decision,
            "execution_result": execution_result,
        }
    return {
        "action": decision_info.action,
        "risk_level": decision_info.risk_level,
        "allowed": decision_info.allowed,
        "require_approval": decision_info.require_approval,
        "decision": decision,
        "reason": decision_info.reason,
        "execution_result": execution_result,
    }


@observe(name="report-fallback")
def _rule_template_report(diagnosis: dict) -> str:
    """LLM 失败或越权时的规则模板报告（fallback 事件进 trace）。"""
    get_client().update_current_span(metadata={"report_fallback": True})
    if diagnosis["status"] == "concluded":
        alternative_names = "；".join(
            alternative["cause_name"] for alternative in diagnosis["alternatives"]
        )
        alternative_text = (
            f"（其他可能：{alternative_names}）" if alternative_names else ""
        )
        return (
            f"根据排查，你描述的问题可能由{diagnosis['primary_cause_name']}导致，"
            f"建议：{diagnosis['fix_suggestion']}{alternative_text}"
        )
    # unresolved 两种子情况：有 top 假设（低置信建议）与完全无头绪。
    if diagnosis["primary_cause_name"]:
        return (
            f"经过 {diagnosis['investigation_rounds']} 轮排查暂未能确定具体原因，"
            f"最可能的原因为「{diagnosis['primary_cause_name']}」（低置信度），"
            f"建议：{diagnosis['fix_suggestion']} 如仍无法解决，请人工介入进一步检查。"
        )
    return (
        f"经过 {diagnosis['investigation_rounds']} 轮排查未能识别设备或症状，"
        "建议人工介入进一步检查。"
    )


@observe(as_type="generation", name="diagnosis-report")
async def _invoke_report(model, prompt_messages: list, config=None):
    """调用 LLM 转写诊断报告；None/解析失败由 report 节点兜底为模板。"""
    report = await model.with_structured_output(DiagnosisReport).ainvoke(
        prompt_messages, config=config
    )
    # with_structured_output 不回填模型名，Langfuse generation 里手动补上。
    get_client().update_current_generation(model=default_model_name())
    return report


@observe(as_type="generation", name="symptom-extract")
async def _invoke_extraction(model, prompt_messages: list, config=None):
    """调用 LLM 抽取症状槽位；None/解析失败由 extract_symptom 兜底。"""
    slots = await model.with_structured_output(SymptomSlots).ainvoke(
        prompt_messages, config=config
    )
    # with_structured_output 不回填模型名，Langfuse generation 里手动补上。
    get_client().update_current_generation(model=default_model_name())
    return slots


@observe(name="symptom-resolve")
def _resolve_symptom(
    slots: SymptomSlots | None,
    devices: list[dict],
    valid_keys: dict[str, set[str]],
) -> dict:
    """确定性解析：设备匹配 → 域映射 → symptom_key 知识库验证。

    兜底策略：LLM 返回 None、设备解析失败或 key 不属于该域时，
    统一落回通用症状 device_abnormal，并打 symptom_fallback 观测标记。
    """
    device = None
    if slots is not None:
        # strip 归一：消除 LLM 输出或设备名首尾空白造成的误兜底。
        device = next(
            (d for d in devices if d["name"].strip() == slots.device_name.strip()),
            None,
        )
    domain = DOMAIN_BY_DEVICE_TYPE.get(device["device_type"], "") if device else ""
    known_keys = valid_keys.get(domain, set())
    fallback = slots is None or domain == "" or slots.symptom_key not in known_keys
    symptom_key = FALLBACK_SYMPTOM_KEY if fallback else slots.symptom_key
    if fallback:
        # 当前观测是 span（非 generation），用 update_current_span 打兜底标记。
        get_client().update_current_span(
            metadata={
                "symptom_fallback": True,
                "raw_symptom_key": slots.symptom_key if slots else "",
            }
        )

    return {
        "device_name": slots.device_name if slots else "",
        "room": (slots.room or None) if slots else None,
        "device_id": device["id"] if device else None,
        "device_type": device["device_type"] if device else None,
        "domain": domain or "unknown",
        "symptom_key": symptom_key,
        "fallback": fallback,
    }


def build_diagnose_graph(
    chat_model=None,
    investigator=None,
    executor=None,
    checkpointer=None,
):
    """构建诊断子图；chat_model / investigator / executor 供测试注入桩实现。

    investigator 签名 (layer, symptom, home_id) -> list[str]；
    executor 签名 (action, diagnosis) -> str（审批通过后的执行器）。
    checkpointer=None 时子图作为节点挂进主图直接继承主图持久化；
    独立运行（含 interrupt 恢复测试）需显式传入 checkpointer。
    """
    investigator = investigator or default_investigator
    executor = executor or default_executor

    async def extract_symptom(state: DiagnoseState, config: RunnableConfig) -> dict:
        """抽取症状槽位并用知识库验证；失败统一落回通用兜底症状。"""
        target = last_human_message(state["messages"])
        async with session_factory() as session:
            # 各一次批量查询，禁止循环内访问数据库。
            devices = (
                (
                    await session.execute(
                        select(Device)
                        .where(Device.home_id == state["home_id"])
                        .order_by(Device.id)
                    )
                )
                .scalars()
                .all()
            )
            symptom_rows = (await session.execute(select(FaultSymptom))).scalars().all()

        # 观察点边界统一转纯 dict：ORM 对象进 Langfuse 入参会被序列化成
        # 空 dict，破坏 trace 可读性。
        device_dicts = [
            {"id": d.id, "name": d.name, "device_type": d.device_type} for d in devices
        ]
        # 偏好别名（device_alias:{id} = 别名）并入匹配表：
        # 「主灯」等用户称呼可直接进入诊断，无需 LLM 猜测映射。
        alias_prefs = (
            (
                await session.execute(
                    select(Preference).where(
                        Preference.home_id == state["home_id"],
                        Preference.pref_key.like("device_alias:%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        device_by_id = {d.id: d for d in devices}
        for pref in alias_prefs:
            _, _, raw_id = pref.pref_key.partition(":")
            if not raw_id.isdigit() or int(raw_id) not in device_by_id:
                continue
            alias_target = device_by_id[int(raw_id)]
            device_dicts.append(
                {
                    "id": alias_target.id,
                    "name": pref.pref_value,
                    "device_type": alias_target.device_type,
                    "is_alias": True,
                }
            )

        valid_keys: dict[str, set[str]] = {}
        for row in symptom_rows:
            valid_keys.setdefault(row.device_type, set()).add(row.symptom_key)

        if target is None:
            logger.warning("诊断子图缺少用户消息，使用通用兜底症状")
            slots = None
        else:
            device_list = (
                "、".join(
                    f"{d['name']}(id={d['id']}, {d['device_type']})"
                    for d in device_dicts
                )
                or "无"
            )
            system_prompt = _EXTRACTION_SYSTEM_PROMPT.format(
                device_list=device_list,
                symptom_dict=_format_symptom_dictionary(symptom_rows),
            )
            try:
                model = chat_model or build_chat_model()
                slots = await _invoke_extraction(
                    model,
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=target.content),
                    ],
                    config=config,
                )
            except Exception:
                logger.warning("症状抽取失败，使用通用兜底症状", exc_info=True)
                slots = None

        symptom = _resolve_symptom(slots, device_dicts, valid_keys)
        return {
            "symptom": symptom,
            "scope": {
                "home_id": state["home_id"],
                "room": symptom["room"],
                "authorized_device_ids": [d["id"] for d in device_dicts],
            },
        }

    async def _compute_candidates(state: DiagnoseState, config) -> dict:
        """确定性候选假设计算：症状 + 已有证据反查知识库（不碰 LLM）。"""
        symptom = state.get("symptom") or {}
        domain = symptom.get("domain", "unknown")
        if domain == "unknown":
            return {"candidate_hypotheses": [], "investigation_round": 0}
        causes, pairs = await _load_domain_knowledge(domain)
        matched = [symptom["symptom_key"], *(state.get("observed_evidence") or [])]
        hypotheses = compute_hypotheses(matched, causes, pairs)
        _hypotheses_observation(domain, hypotheses, 0)
        return {"candidate_hypotheses": hypotheses, "investigation_round": 0}

    async def _investigate_round(state: DiagnoseState, config) -> dict:
        """执行当层确定性检查并收缩候选；轮数由状态机边条件控制。"""
        symptom = state.get("symptom") or {}
        round_index = state.get("investigation_round", 0) + 1
        actions = select_actions(state.get("candidate_hypotheses") or [])
        layer = actions[min(round_index - 1, len(actions) - 1)]["layer"]
        raw_evidence = await investigator(layer, symptom, state["home_id"])
        # 去重保序：同轮重复观测只计一次。
        new_evidence = list(dict.fromkeys(raw_evidence))
        domain = symptom.get("domain", "unknown")
        _, pairs = await _load_domain_knowledge(domain)
        updated = shrink(state.get("candidate_hypotheses") or [], new_evidence, pairs)
        _investigation_observation(round_index, layer, new_evidence, updated)
        return {
            "observed_evidence": new_evidence,
            "candidate_hypotheses": updated,
            "investigation_round": round_index,
        }

    def _after_compute(state: DiagnoseState) -> str:
        # 未知域无候选可排查，直接出结论（第 7 步的结论节点）。
        return "investigate" if state.get("candidate_hypotheses") else "conclude"

    def _after_investigate(state: DiagnoseState) -> str:
        if should_conclude(
            state.get("candidate_hypotheses") or [], state.get("investigation_round", 0)
        ):
            return "conclude"
        return "investigate"

    async def _conclude(state: DiagnoseState, config) -> dict:
        """确定性结论：领先候选为主结论；无区分性证据则建议人工介入。"""
        diagnosis = build_final_diagnosis(
            state.get("candidate_hypotheses") or [],
            list(state.get("observed_evidence") or []),
            state.get("investigation_round", 0),
        )
        _conclusion_observation(diagnosis)
        return {"final_diagnosis": diagnosis}

    async def _approval_gate(state: DiagnoseState, config) -> dict:
        """确定性审批门控：无动作放行；越界阻断；高风险 interrupt 等审批。

        interrupt 挂起后进程可重启（checkpointer 持久化），恢复时节点重跑：
        judge_risk 确定性重判 + pending 记录复用，保证幂等。
        """
        diagnosis = dict(state.get("final_diagnosis") or {})
        symptom = state.get("symptom") or {}
        scope = state.get("scope") or {}
        # home_id 是审批审计的必填字段：缺失属于装配错误，直接失败快停。
        home_id = state["home_id"]
        # 供执行器解析网关等目标设备（写入结论随 trace 可观测）。
        diagnosis["home_id"] = home_id
        action = diagnosis.get("fix_action")
        thread_id = (config or {}).get("configurable", {}).get("thread_id", "")
        target_device_id = symptom.get("device_id")

        if action is None:
            # 无可执行动作（未定位/纯建议）：不产生审批记录，直接放行报告。
            diagnosis["risk_level"] = "low"
            diagnosis["approval"] = {
                "action": None,
                "risk_level": "low",
                "decision": "auto",
                "reason": "无可执行动作，跳过审批门控",
            }
            _approval_observation(None, "auto", None)
            return {"final_diagnosis": diagnosis, "risk_level": "low"}
        if action not in EXECUTABLE_ACTIONS:
            # 人工建议类动作（重插电源线/更换灯泡/联系电工等）：
            # 系统无执行器，审批无意义——只作为报告建议呈现给用户，
            # 不产生审批记录，也不做风险判定。
            diagnosis["risk_level"] = "low"
            diagnosis["approval"] = {
                "action": action,
                "risk_level": "low",
                "decision": "advice",
                "reason": "人工建议类动作，无需审批，直接呈现给用户",
            }
            _approval_observation(None, "advice", None)
            return {"final_diagnosis": diagnosis, "risk_level": "low"}

        decision_info = judge_risk(action, target_device_id, scope)
        diagnosis["risk_level"] = decision_info.risk_level

        if not decision_info.allowed:
            # 范围越界：禁止执行（与审批是两回事），审计落库后直接放行报告。
            decision = "blocked"
            execution_result = "操作超出授权范围，禁止自动执行"
            await _record_auto_approval(
                thread_id,
                home_id,
                decision_info,
                target_device_id,
                decision,
            )
        elif decision_info.require_approval:
            approval_id = await _ensure_pending_approval(
                thread_id, home_id, decision_info, target_device_id
            )
            resume_payload = interrupt(
                {
                    "type": "approval_required",
                    "thread_id": thread_id,
                    "approval_id": approval_id,
                    "action": action,
                    "risk_level": decision_info.risk_level,
                    "reason": decision_info.reason,
                    "suggestion": diagnosis.get("fix_suggestion"),
                }
            )
            decision = resume_payload.get("decision")
            await _record_approval_decision(
                approval_id, decision, resume_payload.get("reason")
            )
            if decision == "approved":
                execution_result = await executor(action, diagnosis)
            else:
                execution_result = "未执行"
        else:
            decision = "auto"
            execution_result = None
            await _record_auto_approval(
                thread_id,
                home_id,
                decision_info,
                target_device_id,
                decision,
            )

        diagnosis["approval"] = {
            "action": action,
            "risk_level": decision_info.risk_level,
            "require_approval": decision_info.require_approval,
            "allowed": decision_info.allowed,
            "decision": decision,
            "reason": decision_info.reason,
        }
        diagnosis["execution_result"] = execution_result
        _approval_observation(decision_info, decision, execution_result)
        return {"final_diagnosis": diagnosis, "risk_level": decision_info.risk_level}

    async def _report(state: DiagnoseState, config) -> dict:
        """LLM 转写诊断报告；失败或越权结论一律回退规则模板并过滤措辞。

        审批决策改写报告：rejected 固定话术；approved 追加执行结果；
        blocked 追加越界说明。
        """
        diagnosis = dict(state.get("final_diagnosis") or {})
        candidate_hypotheses = state.get("candidate_hypotheses") or []
        symptom = state.get("symptom") or {}
        approval = diagnosis.get("approval") or {}
        decision = approval.get("decision", "auto")
        if decision == "rejected":
            # 计划口径：拒绝 → 报告改写为固定话术，不再转写。
            text = "已记录，未执行，建议人工处理。"
            diagnosis["report"] = text
            return {"final_diagnosis": diagnosis, "messages": [AIMessage(content=text)]}
        if not candidate_hypotheses:
            # 查无设备/症状未识别：跳过 LLM 转写，直接给指名道姓的提示。
            device_name = symptom.get("device_name")
            text = (
                f"未在当前家庭找到「{device_name}」，请确认设备名称，"
                "或在家庭中添加该设备后再试。"
                if device_name
                else "未能识别设备或症状，建议人工介入进一步检查。"
            )
            diagnosis["report"] = text
            return {"final_diagnosis": diagnosis, "messages": [AIMessage(content=text)]}
        payload = json.dumps(diagnosis, ensure_ascii=False)
        try:
            model = chat_model or build_chat_model()
            report = await _invoke_report(
                model,
                [
                    SystemMessage(content=_REPORT_SYSTEM_PROMPT),
                    HumanMessage(content=payload),
                ],
                config=config,
            )
        except Exception:
            logger.warning("诊断报告转写失败，使用规则模板", exc_info=True)
            report = None
        # 越权守卫：输出 schema 不对或复述的结论 key 与输入不一致，
        # 说明 LLM 新增/篡改了结论，丢弃转写文本回退模板。
        if (
            not isinstance(report, DiagnosisReport)
            or report.primary_cause_key != diagnosis["primary_cause_key"]
        ):
            if report is not None:
                logger.warning("诊断报告越权或结论不一致，已回退规则模板")
            report = None
        text = (
            _apply_wording_filter(report.report)
            if report is not None
            else _apply_wording_filter(_rule_template_report(diagnosis))
        )
        if not text.strip():
            # LLM「成功」返回空串也兜底：报告对用户必须非空（空气泡事故）。
            logger.warning("诊断报告转写为空，回退规则模板")
            text = _apply_wording_filter(_rule_template_report(diagnosis))
        if decision == "approved":
            text = f"{text}\n操作已执行：{diagnosis.get('execution_result', '')}"
        elif decision == "blocked":
            text = f"{text}\n该操作超出授权范围，未自动执行，建议人工处理。"
        diagnosis["report"] = text
        # 报告追加 AI 消息：挂进主图后由 to_answer 取正文返回。
        return {"final_diagnosis": diagnosis, "messages": [AIMessage(content=text)]}

    builder = StateGraph(DiagnoseState)
    builder.add_node("extract_symptom", extract_symptom)
    builder.add_node("compute_hypotheses", _compute_candidates)
    builder.add_node("investigate", _investigate_round)
    builder.add_node("conclude", _conclude)
    builder.add_node("approval_gate", _approval_gate)
    builder.add_node("report", _report)
    builder.add_edge(START, "extract_symptom")
    builder.add_edge("extract_symptom", "compute_hypotheses")
    builder.add_conditional_edges(
        "compute_hypotheses", _after_compute, ["investigate", "conclude"]
    )
    builder.add_conditional_edges(
        "investigate", _after_investigate, ["investigate", "conclude"]
    )
    builder.add_edge("conclude", "approval_gate")
    builder.add_edge("approval_gate", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)


def checkpoint_conninfo() -> str:
    """AsyncConnectionPool 需要 psycopg 原生 URI：去掉 SQLAlchemy 方言前缀。"""
    return get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


async def build_checkpoint_pool():
    """审批续跑共用的 Postgres 连接池（autocommit；saver setup 必需）。

    池跟随进程生命周期，由调用方负责关闭（生产环境即进程退出）。
    """
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        checkpoint_conninfo(), open=False, kwargs={"autocommit": True}
    )
    await pool.open()
    return pool
