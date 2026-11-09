"""主图共享状态：Router 与各子图共用的数据结构。"""

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.agent.context import HomeContext


def last_human_message(messages: list[AnyMessage]) -> AnyMessage | None:
    """取最新一条用户消息；Router 分类与诊断抽取都只针对它。"""
    for message in reversed(messages):
        if getattr(message, "type", "") == "human":
            return message
    return None


class AgentState(TypedDict):
    """主图状态，各节点只读自己需要的字段并返回增量更新。"""

    # add_messages 按消息 id 追加或覆盖，支持多轮对话的消息合并。
    messages: Annotated[list[AnyMessage], add_messages]
    home_id: int
    home_context: HomeContext | None
    # Router 判定的意图。
    intent: str
    # 面向用户的最终回复。
    answer: str
    # 诊断子图产出（第 10 步）：审批风险等级与最终诊断结论。
    risk_level: str
    final_diagnosis: dict | None
