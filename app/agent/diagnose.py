"""诊断子图：症状抽取 → 候选假设 → 分层排查（3 轮硬上限）→ 结论 → 审批。

已交付：DiagnoseState、症状抽取节点、候选假设计算与分层排查循环。
后续步骤在同一骨架上插入诊断结论、审批门控与报告转写节点。

序列化约束：DiagnoseState 全部字段为 JSON 原生类型，集合语义用
list 表达，Pydantic 对象 model_dump 后再入状态，保证 Postgres
checkpointer 断点续跑可序列化。
"""

import asyncio
import json
import logging
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langfuse import get_client, observe
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel
from sqlalchemy import select

from app.agent.hypotheses import (
    build_final_diagnosis,
    compute_hypotheses,
    select_actions,
    should_conclude,
    shrink,
)
from app.agent.state import last_human_message
from app.database import session_factory
from app.llm_client import build_chat_model, default_model_name
from app.mcp.client import get_light_tools
from app.models import Device, DeviceEvent, FaultCause, FaultSymptom, SymptomCause

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

# 过度断言词的确定性替换表：长词在前，避免子串二次命中。
ASSERTION_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("一定是", "可能是"),
    ("肯定是", "可能是"),
    ("必然是", "可能是"),
    ("绝对是", "可能是"),
    ("必然", "可能"),
    ("已修复", "建议检查"),
    ("坏了", "故障"),
)


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
        tools = {tool.name: tool for tool in await get_light_tools()}
        tool = tools.get("get_device_status")
        if tool is None:
            return None
        # 模拟设备 id 与库内自增 id 的近似映射（light-{id}），演示数据已对齐。
        result = await tool.ainvoke({"device_id": f"light-{device_id}"})
    except Exception, asyncio.CancelledError:
        # CancelledError 非 Exception 子类：跨事件循环复用 MCP 客户端时
        # anyio 会以取消形式报错，诊断链路不能被 MCP 故障打断。
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


def _filter_assertions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """过度断言词确定性替换；返回 (过滤后文本, 替换记录)。"""
    replaced: list[tuple[str, str]] = []
    filtered = text
    for old, new in ASSERTION_REPLACEMENTS:
        if old in filtered:
            filtered = filtered.replace(old, new)
            replaced.append((old, new))
    return filtered, replaced


@observe(name="wording-filter")
def _apply_wording_filter(text: str) -> str:
    """措辞过滤观测点：替换事件通过 metadata 进入当前 trace。"""
    filtered, replacements = _filter_assertions(text)
    if replacements:
        get_client().update_current_span(
            metadata={"assertion_replacements": replacements}
        )
    return filtered


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
    return (
        f"经过 {diagnosis['investigation_rounds']} 轮排查未能定位具体原因，"
        f"{diagnosis['fix_suggestion']}"
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
    devices: list,
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
            (d for d in devices if d.name.strip() == slots.device_name.strip()),
            None,
        )
    domain = DOMAIN_BY_DEVICE_TYPE.get(device.device_type, "") if device else ""
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
        "device_id": device.id if device else None,
        "device_type": device.device_type if device else None,
        "domain": domain or "unknown",
        "symptom_key": symptom_key,
        "fallback": fallback,
    }


def build_diagnose_graph(chat_model=None, investigator=None):
    """构建诊断子图；chat_model / investigator 供测试注入桩实现。

    investigator 签名 (layer, symptom, home_id) -> list[str]。
    子图不自带 checkpointer：作为节点挂进主图时直接继承主图的
    Postgres checkpointer，避免 checkpoint namespace 双层持久化。
    """
    investigator = investigator or default_investigator

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

        valid_keys: dict[str, set[str]] = {}
        for row in symptom_rows:
            valid_keys.setdefault(row.device_type, set()).add(row.symptom_key)

        if target is None:
            logger.warning("诊断子图缺少用户消息，使用通用兜底症状")
            slots = None
        else:
            device_list = (
                "、".join(f"{d.name}(id={d.id}, {d.device_type})" for d in devices)
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

        symptom = _resolve_symptom(slots, devices, valid_keys)
        return {
            "symptom": symptom,
            "scope": {
                "home_id": state["home_id"],
                "room": symptom["room"],
                "authorized_device_ids": [d.id for d in devices],
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
        return {"final_diagnosis": diagnosis}

    async def _report(state: DiagnoseState, config) -> dict:
        """LLM 转写诊断报告；失败或越权结论一律回退规则模板并过滤措辞。"""
        diagnosis = dict(state.get("final_diagnosis") or {})
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
        diagnosis["report"] = text
        # 报告追加 AI 消息：挂进主图后由 to_answer 取正文返回。
        return {"final_diagnosis": diagnosis, "messages": [AIMessage(content=text)]}

    builder = StateGraph(DiagnoseState)
    builder.add_node("extract_symptom", extract_symptom)
    builder.add_node("compute_hypotheses", _compute_candidates)
    builder.add_node("investigate", _investigate_round)
    builder.add_node("conclude", _conclude)
    builder.add_node("report", _report)
    builder.add_edge(START, "extract_symptom")
    builder.add_edge("extract_symptom", "compute_hypotheses")
    builder.add_conditional_edges(
        "compute_hypotheses", _after_compute, ["investigate", "conclude"]
    )
    builder.add_conditional_edges(
        "investigate", _after_investigate, ["investigate", "conclude"]
    )
    builder.add_edge("conclude", "report")
    builder.add_edge("report", END)
    return builder.compile()
