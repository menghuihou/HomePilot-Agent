"""意图路由器：结构化输出判定意图，非法输出兜底 chat。

Router 只做分类，不回答业务问题；home_mode 直接读上下文回答，
control / diagnose / inspect 返回占位回复，chat 交给 chat 子图。
"""

from typing import Literal

from langchain_core.messages import BaseMessage, SystemMessage
from langfuse import get_client, observe
from pydantic import BaseModel

from app.agent.context import HOME_MODE_KEY, HomeContext
from app.config import get_settings
from app.llm_client import build_chat_model

Intent = Literal["chat", "control", "diagnose", "inspect", "home_mode"]

_SYSTEM_PROMPT_TEMPLATE = """你是智能家庭助手的意图路由器，只负责分类，不回答业务问题。可选意图：
- chat：闲聊、一般问答、设置偏好或称呼（如「把客厅灯叫主灯」）
- control：控制设备，例如开关灯、调节温度
- diagnose：设备故障排查，例如「灯不亮了」
- inspect：巡检全屋设备状态或索要家庭日报
- home_mode：询问家庭当前模式

当前家庭上下文（仅用于理解用户指代，不要向用户复述）：
{home_summary}"""

# 后续周次实现的意图先统一返回占位文案，避免提前实现子图。
_UNSUPPORTED_PLACEHOLDERS = {
    "control": "设备控制能力将在后续版本上线。",
    "diagnose": "故障诊断能力将在后续版本上线。",
    "inspect": "设备巡检能力将在后续版本上线。",
}


class RouterDecision(BaseModel):
    """Router 的结构化输出：五类意图之一。"""

    intent: Intent


def summarize_home_context(context: HomeContext) -> str:
    """把家庭上下文压缩成 prompt 可读的几行摘要。"""
    lines = [f"家庭：{context.home.name}（当前模式：{context.current_mode}）"]
    lines.append("房间：" + ("、".join(room.name for room in context.rooms) or "无"))
    lines.append(
        "成员：" + ("、".join(member.name for member in context.members) or "无")
    )
    prefs = [
        f"{p.pref_key}={p.pref_value}"
        for p in context.preferences
        if p.pref_key != HOME_MODE_KEY
    ]
    lines.append("偏好：" + ("；".join(prefs) or "无"))
    return "\n".join(lines)


@observe(as_type="generation", name="intent-router")
def _invoke_router(chat_model, prompt_messages: list) -> RouterDecision:
    """调用 LLM 并解析为结构化意图；解析失败由 classify_intent 兜底。"""
    decision = chat_model.with_structured_output(RouterDecision).invoke(prompt_messages)
    # with_structured_output 不回填模型名，Langfuse generation 里手动补上。
    get_client().update_current_generation(
        model=get_settings().light_model or "claude-sonnet-4-5"
    )
    return decision


def classify_intent(
    messages: list[BaseMessage],
    home_context: HomeContext,
    chat_model=None,
) -> str:
    """判定用户消息意图；LLM 输出非法或调用失败时兜底为 chat。"""
    model = chat_model or build_chat_model()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        home_summary=summarize_home_context(home_context)
    )
    try:
        decision = _invoke_router(
            model, [SystemMessage(content=system_prompt), *messages]
        )
    except Exception:  # noqa: BLE001
        # 兜底边界：结构化解析失败、网关超时等多种异常类型都不应中断对话，
        # 统一降级为闲聊意图；失败详情已在 Langfuse generation 中记录。
        return "chat"
    return decision.intent


def reply_for_intent(intent: str, home_context: HomeContext) -> str | None:
    """非 chat 意图的直接回复；chat 返回 None，交给 chat 子图处理。"""
    if intent == "home_mode":
        return f"当前家庭模式是：{home_context.current_mode}。"
    return _UNSUPPORTED_PLACEHOLDERS.get(intent)
