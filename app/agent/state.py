"""主图共享状态：Router 与各子图共用的数据结构。"""

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.agent.context import HomeContext


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
