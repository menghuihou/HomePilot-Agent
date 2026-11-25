"""能耗分析子图：按设备 / 按天聚合空调能耗并生成一句摘要（第 7 步）。

aggregate_energy 为纯函数（零 DB 零 LLM）：输入归一化的能耗事件列表，
输出按设备 / 按天聚合与用电最高的设备。子图三节点：取数（查
energy_report 种子事件）→ 聚合 → 文案（LLM 一句摘要，失败 / 空回复
降级确定性模板）。

主图挂载口径：Router 意图集 5 → 6（新增 energy），条件边进入本子图
——「子图注册 = 主图微调」边界的实证：MCP 加设备零代码，加能力要动
主图路由。
"""

import json
import logging
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy import text

from app.database import session_factory
from app.llm_client import build_chat_model, extract_text_content

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 7

_ENERGY_SUMMARY_PROMPT = """你是家庭能耗助手。给你一份结构化能耗聚合（JSON），用中文写一句摘要（80 字内）。

规则：只转写给定数据，数值原样引用；不新增设备，不添加节能建议。"""


def aggregate_energy(events: list[dict], scope: dict | None = None) -> dict:
    """纯函数聚合能耗：输入归一化事件列表，输出按设备 / 按天聚合。

    - events: [{"device_id", "device_name", "kwh", "date"}]，date 为
      "YYYY-MM-DD" 文本（零 DB 零 LLM，可安全进 checkpointer 状态）；
    - scope: {"days": N} 仅保留数据中最新的 N 个自然日（缺省全量）。
    输出 {total_kwh, by_device, by_day, top_consumer, device_count}，
    数值 round(2)，by_device 按名称排序、by_day 按日期排序（确定性）。
    """
    days = (scope or {}).get("days")
    scoped = list(events)
    if days and scoped:
        dates = sorted({str(item.get("date")) for item in scoped}, reverse=True)
        keep = set(dates[: int(days)])
        scoped = [item for item in scoped if str(item.get("date")) in keep]

    by_device: dict[str, float] = {}
    by_day: dict[str, float] = {}
    for item in scoped:
        name = str(item.get("device_name") or item.get("device_id"))
        kwh = float(item.get("kwh") or 0)
        day = str(item.get("date"))
        by_device[name] = round(by_device.get(name, 0) + kwh, 2)
        by_day[day] = round(by_day.get(day, 0) + kwh, 2)

    total = round(sum(by_device.values()), 2)
    top = (
        max(sorted(by_device.items()), key=lambda pair: pair[1]) if by_device else None
    )
    return {
        "total_kwh": total,
        "by_device": dict(sorted(by_device.items())),
        "by_day": dict(sorted(by_day.items())),
        "top_consumer": {"device_name": top[0], "kwh": top[1]} if top else None,
        "device_count": len(by_device),
    }


def _render_energy_summary(summary: dict) -> str:
    """确定性模板：LLM 不可用 / 空回复时的兜底摘要。"""
    days = summary.get("days", DEFAULT_DAYS)
    total = summary.get("total_kwh", 0)
    top = summary.get("top_consumer")
    if not top:
        return f"近 {days} 天暂无空调用电数据。"
    return (
        f"近 {days} 天空调总用电 {total} kWh，"
        f"用电最高的是{top['device_name']}（{top['kwh']} kWh）。"
    )


class EnergyState(TypedDict):
    """能耗子图状态：home_id 与主图共享，其余子图私有。"""

    messages: Annotated[list, add_messages]
    home_id: int
    events: list[dict]
    energy_summary: dict
    answer: str


def build_energy_graph(chat_model=None):
    """构建能耗子图：取数 → 聚合 → 文案（LLM 一句摘要，失败降级模板）。

    chat_model 供测试注入桩实现；缺省按环境配置构建真实模型。
    """

    async def _load_events(state) -> dict:
        """取数节点：查 ac 域 energy_report 事件（近 7 天，含设备名）。"""
        async with session_factory() as session:
            result = await session.execute(
                text("""
                    SELECT d.id AS device_id, d.name AS device_name,
                           e.payload->>'kwh' AS kwh,
                           to_char(e.occurred_at, 'YYYY-MM-DD') AS day
                    FROM device_events e
                      JOIN devices d ON d.id = e.device_id
                    WHERE d.home_id = :home_id AND d.device_type = 'ac'
                      AND e.event_type = 'energy_report'
                      AND e.occurred_at >= now() - interval '7 days'
                    ORDER BY e.occurred_at
                """),
                {"home_id": state["home_id"]},
            )
            rows = result.mappings().all()
        events = [
            {
                "device_id": str(row.device_id),
                "device_name": row.device_name,
                "kwh": float(row.kwh or 0),
                "date": row.day,
            }
            for row in rows
        ]
        return {"events": events}

    async def _aggregate(state) -> dict:
        """聚合节点：纯函数汇总（scope 固定近 7 天，与取数窗口一致）。"""
        return {
            "energy_summary": aggregate_energy(
                state.get("events") or [], {"days": DEFAULT_DAYS}
            )
        }

    async def _reply(state, config) -> dict:
        """文案节点：LLM 一句摘要；失败 / 空回复降级确定性模板。"""
        summary = state.get("energy_summary") or {}
        try:
            model = chat_model or build_chat_model()
            response = await model.ainvoke(
                [
                    SystemMessage(content=_ENERGY_SUMMARY_PROMPT),
                    HumanMessage(content=json.dumps(summary, ensure_ascii=False)),
                ],
                config=config,
            )
            text = extract_text_content(response.content)
        except Exception:
            logger.warning("能耗摘要 LLM 转写失败，降级模板", exc_info=True)
            text = ""
        if not text.strip():
            text = _render_energy_summary(summary)
        return {"answer": text, "messages": [AIMessage(content=text)]}

    builder = StateGraph(EnergyState)
    builder.add_node("load_events", _load_events)
    builder.add_node("aggregate", _aggregate)
    builder.add_node("reply", _reply)
    builder.add_edge(START, "load_events")
    builder.add_edge("load_events", "aggregate")
    builder.add_edge("aggregate", "reply")
    builder.add_edge("reply", END)
    return builder.compile()
