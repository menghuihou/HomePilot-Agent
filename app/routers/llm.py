"""最小 LLM 调用接口：产生带模型、输入输出与 token 用量的 Langfuse generation。"""

from fastapi import APIRouter
from langfuse import get_client, observe
from pydantic import BaseModel, Field

from app.llm_client import build_chat_model, default_model_name, extract_text_content

router = APIRouter(tags=["llm"])


class LlmDemoRequest(BaseModel):
    prompt: str = Field(
        default="用一句话说明智能家庭助手能做什么。",
        min_length=1,
        max_length=500,
    )


class LlmDemoResponse(BaseModel):
    answer: str
    model: str
    trace_id: str


@observe(as_type="generation", name="light-model-call")
async def _invoke_light_model(prompt: str) -> str:
    message = await build_chat_model().ainvoke(prompt)

    usage = getattr(message, "usage_metadata", None)
    if usage:
        get_client().update_current_generation(
            model=default_model_name(),
            usage_details={
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "total": usage.get("total_tokens", 0),
            },
        )

    return extract_text_content(message.content)


@router.post("/llm/demo", response_model=LlmDemoResponse)
async def llm_demo(payload: LlmDemoRequest) -> LlmDemoResponse:
    """触发一次轻量模型调用，返回回答与本次调用的 Langfuse trace id。"""
    answer = await _invoke_light_model(payload.prompt)
    trace_id = get_client().get_current_trace_id() or ""
    return LlmDemoResponse(
        answer=answer,
        model=default_model_name(),
        trace_id=trace_id,
    )
