"""巡检子图：并行查询设备状态 → 确定性异常检测 → 汇总排序 → 转写。

数据流（与第 4 周实施计划第 5-6 步骨架一致，persist 落库节点在第 9 步
接入子图尾部）：

    load_devices → parallel_query → detect_and_sort → summarize

设计要点：
- 异常检测 / 排序 / 计数全程零 LLM（app/agent/anomaly_rules.py 规则表），
  LLM 只在 summarize 节点把结构化结果转写成中文（第 8 步接入；本步先落
  确定性模板，它同时是 LLM 不可用时的降级路径，巡检能力不因 LLM 挂而不可用）；
- 并行查询用 asyncio.gather + 单设备 asyncio.timeout：超时必须包在每个
  任务内部而不是只包整体 gather，否则一台设备卡死会拖住全部巡检；
  gather 严格保序（结果[i] 对应 devices[i]），结果按传入顺序对回设备；
- 失败设备统一降级为 {"device_id", "name", "error"} 结构，由规则表按
  query_failed 兜底（宁可标记不可漏报）；
- 状态全部 JSON 可序列化类型，复用第 3 周诊断子图踩平的 checkpointer
  序列化口径。
"""

import asyncio
from datetime import UTC, datetime
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langfuse import get_client, observe
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent import anomaly_rules as rules
from app.agent.wording import apply_wording_filter as _apply_wording_filter
from app.database import session_factory
from app.mcp.client import fetch_device_status
from app.models import Device

QUERY_TIMEOUT_SECONDS = 5.0

# 同 severity 内按规则表固定顺序输出，保证排序结果确定性、测试可精确断言。
RULE_ORDER: tuple[str, ...] = (
    rules.RULE_LOW_BATTERY,
    rules.RULE_OFFLINE_TIMEOUT,
    rules.RULE_OFFLINE_BRIEF,
    rules.RULE_NEVER_REPORTED,
    rules.RULE_SENSOR_STALE,
    rules.RULE_QUERY_FAILED,
)

_SEVERITY_RANK = {
    rules.SEVERITY_HIGH: 0,
    rules.SEVERITY_MEDIUM: 1,
    rules.SEVERITY_LOW: 2,
}
_RULE_RANK = {rule: index for index, rule in enumerate(RULE_ORDER)}


class InspectState(TypedDict):
    # messages 用 add_messages 增量合并，与主图 AgentState / DiagnoseState 口径一致。
    messages: Annotated[list[AnyMessage], add_messages]
    home_id: int
    devices: list[dict]  # 待巡检设备列表 [{id, name, device_type}]
    raw_statuses: list[dict]  # 并行查询原始结果（含失败结构），与 devices 保序对齐
    anomalies: list[dict]  # 确定性异常项（已排序）
    total_count: int
    normal_count: int
    summary_text: str | None


def sort_anomalies(anomalies: list[dict]) -> list[dict]:
    """severity 高→低；同 severity 按规则表固定顺序；同序稳定保留设备顺序。"""
    return sorted(
        anomalies,
        key=lambda a: (
            _SEVERITY_RANK.get(a["severity"], len(_SEVERITY_RANK)),
            _RULE_RANK.get(a["rule"], len(_RULE_RANK)),
        ),
    )


async def _query_one(fetcher, device: dict, timeout_seconds: float) -> dict:
    """单设备查询：包 asyncio.timeout，失败统一降级为 error 结构不向外抛。

    正常路径下超时与调用异常都在这里消化；外层 gather 的
    return_exceptions=True 只做最后兜底（见 query_all_devices）。
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            status = await fetcher(device)
    except TimeoutError:
        return {
            "device_id": device["id"],
            "name": device["name"],
            "error": f"查询超时（>{timeout_seconds:.0f} 秒）",
        }
    except Exception as exc:  # noqa: BLE001 —— 任何 MCP 调用故障都降级为单设备失败
        return {
            "device_id": device["id"],
            "name": device["name"],
            "error": f"MCP 调用失败：{exc}",
        }
    if not isinstance(status, dict):
        return {
            "device_id": device["id"],
            "name": device["name"],
            "error": "MCP 返回结构异常",
        }
    # 设备标识统一以业务库为准（int）；MCP 返回的 str 形式在此归一，
    # 避免 raw_statuses 与 anomalies 里 device_id 类型双轨。
    status["device_id"] = device["id"]
    status["name"] = device["name"]
    return status


async def query_all_devices(
    devices: list[dict], fetcher, timeout_seconds: float = QUERY_TIMEOUT_SECONDS
) -> list[dict]:
    """并发查询全部设备状态；单设备超时不阻塞整体，结果与传入严格保序。"""
    results = await asyncio.gather(
        *(_query_one(fetcher, device, timeout_seconds) for device in devices),
        return_exceptions=True,
    )
    # 双保险：_query_one 已消化超时与调用异常，这里只兜底真意外异常
    # （如任务被取消后的残留），同样降级为 error 结构，绝不炸整体。
    return [
        (
            result
            if isinstance(result, dict)
            else {
                "device_id": device["id"],
                "name": device["name"],
                "error": f"意外异常：{result!r}",
            }
        )
        for result, device in zip(results, devices, strict=True)
    ]


def format_template_summary(total: int, normal: int, anomalies: list[dict]) -> str:
    """确定性摘要模板（话术口径对齐总计划功能一）；LLM 不可用时的兜底路径。

    多条异常时按规则合并出「异常分布」行（同类合并展示），逐台明细全部
    保留（第 7 步口径：合并展示但保留逐台明细）。
    """
    if not anomalies:
        return f"检查了 {total} 台设备，一切正常。"
    abnormal = total - normal
    lines = [f"检查了 {total} 台设备：{normal} 台正常，{abnormal} 台异常。"]
    if len(anomalies) > 1:
        lines.append(f"异常分布：{_rule_distribution(anomalies)}")
    lines.extend(
        f"{index}. {anomaly['detail']}" for index, anomaly in enumerate(anomalies, 1)
    )
    return "\n".join(lines)


def _rule_distribution(anomalies: list[dict]) -> str:
    """按规则合并计数（保持异常项已有顺序），如「离线超时 1 台；低电量 2 台」。

    「X 台」按异常项计数，依赖每设备每规则至多一条的规则表不变量
    （此时异常项数 == 设备数）；若将来某规则允许单设备多条异常，
    此处计数口径需同步改为按设备去重。
    """
    counts: dict[str, int] = {}
    for anomaly in anomalies:
        label = rules.RULE_LABELS.get(anomaly["rule"], anomaly["rule"])
        counts[label] = counts.get(label, 0) + 1
    return "；".join(f"{label} {count} 台" for label, count in counts.items())


async def _mcp_status_fetcher(device: dict) -> dict:
    """默认数据源：经 MCP 客户端动态发现调用 get_device_status。"""
    return await fetch_device_status(device["id"])


async def _default_summarizer(total: int, normal: int, anomalies: list[dict]) -> str:
    """默认转写：确定性模板。接缝契约统一为 async，供第 8 步替换 LLM 实现。"""
    return format_template_summary(total, normal, anomalies)


class InspectSummary(BaseModel):
    """巡检摘要的结构化输出：单文本字段，与模板结构一致，解析失败视为转写失败。"""

    summary_text: str = Field(description="面向用户的完整中文摘要")


def _transcription_covers(anomalies: list[dict], text: str) -> bool:
    """完整性守卫：每个异常设备名必须出现在转写文本中，防止 LLM 隐瞒异常。

    已知局限（诚实口径）：「新增 / 捏造设备」无法用确定性规则完全识别，
    由 prompt 约束 + 措辞过滤 + 第 5 周语义评估兜底。
    """
    return all(anomaly["device_name"] in text for anomaly in anomalies)


@observe(name="summary-fallback")
def _fallback_summary(
    reason: str, total: int, normal: int, anomalies: list[dict]
) -> str:
    """转写降级：LLM 失败或被完整性守卫拒绝时回落确定性模板（标记进 trace）。"""
    get_client().update_current_span(metadata={"summary_fallback": reason})
    return format_template_summary(total, normal, anomalies)


def _make_llm_summarizer(model):
    """构造 LLM 转写器：只许转写输入异常项；隐瞒 / 失败 / 不可用一律模板降级。

    model 需支持 with_structured_output（与诊断子图同一约定）。
    """

    async def summarize(total: int, normal: int, anomalies: list[dict]) -> str:
        # 无异常时确定性模板即可：LLM 不介入（零成本 + 测试无 LLM 依赖）。
        if not anomalies:
            return format_template_summary(total, normal, anomalies)

        anomaly_lines = "\n".join(
            f"{index}. [{anomaly['severity']}] {anomaly['device_name']}"
            f"（{rules.RULE_LABELS.get(anomaly['rule'], anomaly['rule'])}）："
            f"{anomaly['detail']}"
            for index, anomaly in enumerate(anomalies, 1)
        )
        messages = [
            SystemMessage(
                content=(
                    "你是家庭设备巡检摘要转写器。严格按输入转写：\n"
                    "1. 只允许转写输入异常列表中的设备与异常项，禁止新增、隐瞒"
                    "或改判异常数量与严重程度；\n"
                    "2. 禁止使用「一定」「必然」「已修复」「肯定」等过度断言词；\n"
                    "3. 中文输出；第一行固定为"
                    f"「检查了 {total} 台设备：{normal} 台正常，"
                    f"{total - normal} 台异常。」；\n"
                    "4. 每条异常保留设备名与建议动作。"
                )
            ),
            HumanMessage(
                content=(
                    f"设备总数 {total} 台，正常 {normal} 台。异常列表：\n"
                    f"{anomaly_lines}\n请转写为面向用户的摘要。"
                )
            ),
        ]
        try:
            report = await model.with_structured_output(InspectSummary).ainvoke(
                messages
            )
        except Exception:  # noqa: BLE001 —— LLM 任何故障都降级，巡检能力不受影响
            return _fallback_summary("llm_error", total, normal, anomalies)

        text = report.summary_text.strip() if report and report.summary_text else ""
        # 完整性守卫：隐瞒异常（如输入 2 条异常却输出「一切正常」）直接拒绝。
        if not text or not _transcription_covers(anomalies, text):
            return _fallback_summary("summary_rejected", total, normal, anomalies)
        return _apply_wording_filter(text)

    return summarize


def build_inspect_graph(
    status_fetcher=None,
    summarizer=None,
    chat_model=None,
    checkpointer=None,
    timeout_seconds: float = QUERY_TIMEOUT_SECONDS,
):
    """构建巡检子图；status_fetcher / summarizer / chat_model 供注入与测试。

    status_fetcher 签名 async (device) -> dict（get_device_status 结构）；
    summarizer 签名 async (total, normal, anomalies) -> str。
    转写器解析顺序：显式 summarizer > chat_model 提供时的 LLM 转写器
    （模板兜底）> 确定性模板。单测不传 chat_model 即零 LLM 依赖。
    checkpointer=None 时子图作为节点挂进主图直接继承主图持久化。
    """
    fetcher = status_fetcher or _mcp_status_fetcher
    if summarizer is not None:
        summarize = summarizer
    elif chat_model is not None:
        summarize = _make_llm_summarizer(chat_model)
    else:
        summarize = _default_summarizer

    async def load_devices(state: InspectState, config) -> dict:
        """按 home_id 一次批量加载设备列表，禁止循环内访问数据库。"""
        async with session_factory() as session:
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
        return {
            "devices": [
                {"id": d.id, "name": d.name, "device_type": d.device_type}
                for d in devices
            ]
        }

    async def parallel_query(state: InspectState, config) -> dict:
        raw_statuses = await query_all_devices(
            state["devices"], fetcher, timeout_seconds
        )
        return {"raw_statuses": raw_statuses}

    async def detect_and_sort(state: InspectState, config) -> dict:
        now = datetime.now(UTC)
        anomalies: list[dict] = []
        for device, status in zip(state["devices"], state["raw_statuses"], strict=True):
            try:
                device_anomalies = rules.detect_anomalies(
                    device["id"], device["name"], device["device_type"], status, now
                )
            except ValueError as exc:
                # 规则表对畸形时间戳等数据格式问题故意 fail-loud（防静默
                # 错判）；那是外部输入不是编程错误，在这里按设备降级为
                # query_failed，口径与查询失败一致，不让单设备脏数据炸掉
                # 整场巡检。
                device_anomalies = [
                    rules.mark_query_failed(device["id"], device["name"], str(exc))
                ]
            anomalies.extend(device_anomalies)
        anomalies = sort_anomalies(anomalies)
        abnormal_devices = {anomaly["device_id"] for anomaly in anomalies}
        total = len(state["devices"])
        return {
            "anomalies": anomalies,
            "total_count": total,
            # 计数口径按设备（与功能一示例一致）：一台设备可有多条异常项。
            "normal_count": total - len(abnormal_devices),
        }

    async def summarize_node(state: InspectState, config) -> dict:
        summary = await summarize(
            state["total_count"], state["normal_count"], state["anomalies"]
        )
        return {"summary_text": summary}

    builder = StateGraph(InspectState)
    builder.add_node(load_devices)
    builder.add_node(parallel_query)
    builder.add_node(detect_and_sort)
    builder.add_node(summarize_node)
    builder.add_edge(START, "load_devices")
    builder.add_edge("load_devices", "parallel_query")
    builder.add_edge("parallel_query", "detect_and_sort")
    builder.add_edge("detect_and_sort", "summarize_node")
    builder.add_edge("summarize_node", END)
    return builder.compile(checkpointer=checkpointer)
