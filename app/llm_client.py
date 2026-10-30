"""聊天模型入口：Router 与各子图共用同一构建方式，避免重复配置。"""

from app.config import get_settings

DEFAULT_LIGHT_MODEL = "claude-sonnet-4-5"


def default_model_name() -> str:
    """返回当前生效的轻量模型名，供构建模型与观测元数据共用。"""
    return get_settings().light_model or DEFAULT_LIGHT_MODEL


def build_chat_model():
    """按环境配置构建轻量模型客户端。"""
    settings = get_settings()
    # 延迟导入：非 LLM 路径（健康检查、CRUD）无需加载重型 SDK。
    from langchain_anthropic import ChatAnthropic

    kwargs = {
        "model": default_model_name(),
        "api_key": settings.model_api_key,
        # GLM 等推理模型先输出 thinking 块，额度太小会截断正文。
        "max_tokens": 1024,
        # 限制单次调用时长，避免网关异常时请求无限挂起。
        "timeout": 60,
    }
    if settings.model_base_url:
        kwargs["base_url"] = settings.model_base_url
    return ChatAnthropic(**kwargs)


def extract_text_content(content: object) -> str:
    """提取消息正文；推理模型可能返回 thinking + text 多块内容，只保留正文。"""
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
        return text or str(content)
    return str(content)
