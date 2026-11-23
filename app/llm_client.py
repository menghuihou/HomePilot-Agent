"""聊天模型入口：Router 与各子图共用同一构建方式，避免重复配置。

模型适配层（第 8 步）：config/models.yaml 声明角色 → 模型映射，换模型
只改 yaml；yaml 缺失时回退环境变量路径（向后兼容）。
invoke_with_fallback 在主模型异常或空回复时降级备用模型一次，降级
事件记 warning 并打 fallback 标签进 trace。
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.config import get_settings

DEFAULT_LIGHT_MODEL = "claude-sonnet-4-5"
DEFAULT_MODELS_YAML = Path(__file__).resolve().parent.parent / "config" / "models.yaml"

logger = logging.getLogger(__name__)


class ModelSpec(BaseModel):
    """yaml roles.<role>.primary / fallback 的单模型声明。

    当前仅支持 Anthropic 兼容端点（含 GLM via base_url），
    多 provider 分派为后续扩展方向。
    """

    model: str


class RoleConfig(BaseModel):
    """一个角色的主 / 备模型；fallback 可缺省。"""

    primary: ModelSpec
    fallback: ModelSpec | None = None


class ModelsConfig(BaseModel):
    """整份 yaml 配置：角色映射 + 默认角色必须真实存在。"""

    default_role: str = "light"
    roles: dict[str, RoleConfig]

    def validate_default_role(self) -> ModelsConfig:
        if self.default_role not in self.roles:
            # ModelsConfigError 是 _load_models_config_safe 的捕获范围：
            # 坏 yaml 走降级路径而非崩溃。
            raise ModelsConfigError(
                f"default_role '{self.default_role}' 不在 roles 中"
            )
        return self


def _models_yaml_path() -> Path:
    """yaml 路径解析：LLM_MODELS_YAML env > settings.llm_models_yaml > 缺省路径。"""
    settings = get_settings()
    env_path = os.environ.get("LLM_MODELS_YAML")
    if env_path:
        return Path(env_path)
    if settings.llm_models_yaml:
        return Path(settings.llm_models_yaml)
    return DEFAULT_MODELS_YAML


def load_model_config(path: Path | None = None) -> ModelsConfig:
    """加载并校验 yaml 模型配置；缺失 / 非法抛 ModelsConfigError。"""
    p = path or _models_yaml_path()
    if not p.exists():
        raise ModelsConfigNotFoundError(f"模型配置不存在：{p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ModelsConfigError(f"模型配置必须是映射结构：{p}")
    config = ModelsConfig.model_validate(raw)
    return config.validate_default_role()


class ModelsConfigNotFoundError(FileNotFoundError):
    """模型配置文件缺失。"""


class ModelsConfigError(ValueError):
    """模型配置内容非法（结构 / 字段 / 默认角色）。"""


def _load_models_config_safe() -> ModelsConfig | None:
    """yaml 加载的安全包装：失败记 warning 返回 None（不阻断模型构建）。"""
    try:
        return load_model_config()
    except (
        ModelsConfigNotFoundError,
        ModelsConfigError,
        yaml.YAMLError,
    ):
        logger.warning("模型配置加载失败，回退环境变量路径", exc_info=True)
        return None


@lru_cache(maxsize=1)
def _cached_model_config() -> ModelsConfig | None:
    """缓存 yaml 配置加载结果；需要热更新时调用 cache_clear。"""
    return _load_models_config_safe()


def default_model_name() -> str:
    """返回当前生效的轻量模型名，供构建模型与观测元数据共用。"""
    settings = get_settings()
    config = _cached_model_config()
    if config:
        role = config.roles.get(config.default_role)
        if role:
            return role.primary.model
    return settings.light_model or DEFAULT_LIGHT_MODEL


def build_chat_model():
    """按 yaml / 环境配置构建轻量模型客户端。"""
    settings = get_settings()
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


def invoke_with_fallback(messages: list, config=None):
    """主模型调用；异常或空回复时降级备用模型重试一次。

    - messages / config: 透传给模型 ainvoke；
    - 返回 AIMessage；两次都失败 / 空回复时返回 AIMessage("")，
      由调用方的模板兜底处理。
    降级事件记 warning 并通过 langfuse metadata 打 fallback 标签。
    """
    primary = build_chat_model()
    try:
        response = primary.ainvoke(messages, config=config)
        text = extract_text_content(response.content)
        if text.strip():
            return response
        logger.warning("主模型空回复，降级 fallback")
    except Exception:
        logger.warning("主模型调用失败，降级 fallback", exc_info=True)

    config_obj = _load_models_config_safe()
    fallback_model = (
        config_obj.roles.get(config_obj.default_role).fallback if config_obj else None
    )
    if not fallback_model:
        return AIMessage(content="")
    try:
        fb = build_chat_model_for(fallback_model)
        response = fb.ainvoke(messages, config=config)
        text = extract_text_content(response.content)
        if text.strip():
            logger.info("已降级至 fallback 模型 %s", fallback_model.model)
            return response
    except Exception:
        logger.warning("fallback 模型也失败", exc_info=True)
    return AIMessage(content="")


def build_chat_model_for(spec):
    """按 ModelSpec 构建模型实例（fallback 用）。"""
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    return ChatAnthropic(
        model=spec.model,
        api_key=settings.model_api_key,
        max_tokens=1024,
        timeout=60,
        **({"base_url": settings.model_base_url} if settings.model_base_url else {}),
    )


def extract_text_content(content: object) -> str:
    """提取消息正文；推理模型可能返回 thinking + text 多块内容，只保留正文。

    提取不到文本（如 max_tokens 截断后只剩 thinking / 签名块）时返回空串，
    由调用方降级——绝不把原始 content 转成字符串返回，避免推理过程
    整包泄漏给用户。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            parts.append(str(block.get("text") or ""))
        elif hasattr(block, "text"):
            parts.append(str(block.text or ""))
    return "".join(parts)
