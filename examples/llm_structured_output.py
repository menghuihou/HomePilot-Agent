"""一次 LLM 调用 + 结构化输出（ChatAnthropic + Pydantic，走智谱 GLM 兼容接口）。

环境约定：
- ANTHROPIC_AUTH_TOKEN：智谱 GLM 的 API Key（Anthropic 兼容 Bearer 鉴权，SDK 自动读取）
- 可选 LLM_MODEL 覆盖默认模型

运行方式：uv run python examples/llm_structured_output.py
"""

import json
import os
from typing import Literal

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field, field_validator


# 1) 用 Pydantic 描述"想要的输出结构"，它会被编译成 JSON Schema 交给模型。
class Intent(BaseModel):
    """用户意图识别结果（HomePilot Router 的最小版）。"""

    intent: Literal["chat", "diagnose", "inspect", "report", "home_mode"] = Field(
        description="识别出的用户意图，只能取枚举之一",
    )
    device_hint: str | None = Field(
        default=None,
        description="用户提到的设备或位置描述，如'客厅的灯'；未提到则为 null",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="判断置信度，0~1")

    @field_validator("device_hint", mode="before")
    @classmethod
    def _normalize_none(cls, value: object) -> object:
        """真实坑：模型可能把 null 输出成字符串 'null'，统一归一化为 None。"""
        if isinstance(value, str) and value.strip().lower() in {"null", "none", "无"}:
            return None
        return value


def build_llm() -> ChatAnthropic:
    """GLM 的 Anthropic 兼容端点；Key 从 ANTHROPIC_AUTH_TOKEN 读取。"""
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise SystemExit("请先设置环境变量 ANTHROPIC_AUTH_TOKEN（智谱 GLM 的 API Key）")
    return ChatAnthropic(
        model=os.environ.get("LLM_MODEL", "glm-4.6"),
        base_url=os.environ.get(
            "ANTHROPIC_BASE_URL",
            "https://open.bigmodel.cn/api/anthropic",
        ),
        api_key=api_key,  # langchain-anthropic 只认 api_key 参数/ANTHROPIC_API_KEY，
        # 所以把 ANTHROPIC_AUTH_TOKEN 显式传进来
        temperature=0,
        max_tokens=1024,
    )


def main() -> None:
    # 2) 结构化输出第一步：看看 Pydantic 模型被编译成的 JSON Schema。
    print("== Intent 的 JSON Schema（发给模型的输出约束）==")
    print(json.dumps(Intent.model_json_schema(), ensure_ascii=False, indent=2))

    # 3) with_structured_output：把 Schema 绑定给模型，返回值自动反序列化 + 校验成 Intent 实例。
    structured = build_llm().with_structured_output(Intent)

    for utterance in ("客厅的灯不亮了，帮我看看", "我出门了"):
        result = structured.invoke(utterance)  # 一次 LLM 调用
        assert isinstance(result, Intent)  # 类型收敛，IDE 补全/静态检查都可用
        print(f"\n输入: {utterance}")
        print(
            f"输出: intent={result.intent!r} "
            f"device_hint={result.device_hint!r} confidence={result.confidence}"
        )


if __name__ == "__main__":
    main()
