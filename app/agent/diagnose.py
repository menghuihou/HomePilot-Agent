"""诊断子图：症状抽取 → 候选假设 → 分层排查 → 诊断结论 → 审批门控。

本步交付 DiagnoseState 定义、症状抽取节点与图骨架（START →
extract_symptom → END）；后续步骤在同一骨架上依次插入候选假设、
分层排查与审批门控节点。

序列化约束：DiagnoseState 全部字段为 JSON 原生类型，集合语义用
list 表达，Pydantic 对象 model_dump 后再入状态，保证 Postgres
checkpointer 断点续跑可序列化。
"""

import logging
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langfuse import get_client, observe
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel
from sqlalchemy import select

from app.agent.state import last_human_message
from app.database import session_factory
from app.llm_client import build_chat_model, default_model_name
from app.models import Device, FaultSymptom

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

_EXTRACTION_SYSTEM_PROMPT = """你是家庭智能助手的故障诊断前处理模块，只负责抽取症状槽位，不诊断、不给建议。

用户家庭中的设备（device_name 必须从下面选择，找不到就原样保留用户说法）：
{device_list}

症状字典（symptom_key 必须从下面选择，不要编造）：
{symptom_dict}
"""


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


def build_diagnose_graph(chat_model=None):
    """构建诊断子图骨架；chat_model 供测试注入桩模型。

    子图不自带 checkpointer：作为节点挂进主图时直接继承主图的
    Postgres checkpointer，避免 checkpoint namespace 双层持久化。
    """

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

    builder = StateGraph(DiagnoseState)
    builder.add_node("extract_symptom", extract_symptom)
    builder.add_edge(START, "extract_symptom")
    builder.add_edge("extract_symptom", END)
    return builder.compile()
