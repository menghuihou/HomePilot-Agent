"""chat 接口：同步回答 + SSE 流式回答。

校验约定：
- 参数校验（422）由 Pydantic 在进入业务代码前完成；
- 家庭不存在（404）在建立 SSE 流之前预先检查，失败返回普通 JSON；
- thread_id 可选，缺省由服务端生成并随响应返回。
"""

import json
import logging
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langfuse import get_client
from opentelemetry.trace import format_trace_id, get_current_span
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.chat import get_agent_graph as build_dependency_graph
from app.database import get_session
from app.models import Home

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# astream_events 元数据里用节点名区分 chat 回复与 Router 的模型调用。
_REPLY_NODE = "reply"


class ChatRequest(BaseModel):
    home_id: int
    message: str = Field(min_length=1, max_length=1000)
    thread_id: str | None = None

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message 不能为空白")
        return value


class ChatResponse(BaseModel):
    answer: str
    intent: str
    thread_id: str
    # 尽力获取：Langfuse 上下文不可用时为空串，前端据此省略展示。
    trace_id: str = ""


async def get_agent_graph() -> Any:
    """依赖注入出口：测试用 dependency_overrides 替换为桩图。

    生产路径走 chat 模块的异步单例（Postgres checkpointer）；
    审批挂起与决策恢复共用同一实例（同实例同 checkpointer 不变量）。
    """
    return await build_dependency_graph()


def _current_trace_id() -> str:
    """尽力获取本次请求的 Langfuse trace id；拿不到返回空串。

    必须在端点期调用（OTel server span 仍在上下文中）；流式生成器阶段
    中间件上下文已退出，拿不到。
    """
    try:
        trace_id = get_client().get_current_trace_id() or ""
    except Exception:  # noqa: BLE001
        trace_id = ""
    if trace_id:
        return trace_id
    span = get_current_span()
    span_context = span.get_span_context() if span is not None else None
    if span_context is not None and span_context.is_valid:
        return format_trace_id(span_context.trace_id)
    return ""


async def _get_home_or_404(home_id: int, session: AsyncSession) -> Home:
    home = await session.get(Home, home_id)
    if home is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="home not found"
        )
    return home


def _sse(event: str, payload: dict) -> str:
    """单条 SSE 数据帧：`data: {"event": ..., ...}\\n\\n`。"""
    return (
        "data: " + json.dumps({"event": event, **payload}, ensure_ascii=False) + "\n\n"
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    graph: Annotated[Any, Depends(get_agent_graph)],
) -> ChatResponse:
    """同步回答：等待完整结果返回（保底可用）。"""
    await _get_home_or_404(payload.home_id, session)
    thread_id = payload.thread_id or f"web-{uuid4().hex}"
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content=payload.message)],
            "home_id": payload.home_id,
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    if result.get("__interrupt__"):
        # 同步接口无法完成审批交互：明确指引用户走流式接口。
        answer = "该请求包含需要审批确认的高风险操作，请改用流式对话完成审批流程。"
    else:
        answer = result["answer"]
    return ChatResponse(
        answer=answer,
        intent=result["intent"],
        thread_id=thread_id,
        trace_id=_current_trace_id(),
    )


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    graph: Annotated[Any, Depends(get_agent_graph)],
) -> StreamingResponse:
    """SSE 流式回答：delta 推送回复 token，done 推送最终全文。"""
    await _get_home_or_404(payload.home_id, session)
    thread_id = payload.thread_id or f"web-{uuid4().hex}"
    trace_id = _current_trace_id()
    config = {"configurable": {"thread_id": thread_id}}

    async def event_stream():
        yield _sse("meta", {"thread_id": thread_id})
        chunks: list[str] = []
        final_answer = ""
        try:
            async for event in graph.astream_events(
                {
                    "messages": [HumanMessage(content=payload.message)],
                    "home_id": payload.home_id,
                },
                config=config,
                version="v2",
                subgraphs=True,
            ):
                kind = event.get("event")
                node = event.get("metadata", {}).get("langgraph_node")
                if kind == "on_chat_model_stream":
                    if node != _REPLY_NODE:
                        # Router 的结构化输出不向用户流式输出。
                        continue
                    raw = event["data"]["chunk"].content
                    if isinstance(raw, list):
                        # 流式分片里 thinking 块没有 text 键：只取正文块，
                        # 过滤推理内容，避免把列表 repr 当文本下发。
                        text = "".join(
                            block.get("text", "")
                            for block in raw
                            if isinstance(block, dict)
                        )
                    else:
                        text = raw if isinstance(raw, str) else ""
                    if text:
                        chunks.append(text)
                        yield _sse("delta", {"text": text})
                elif kind == "on_tool_start":
                    yield _sse("tool_start", {"name": event.get("name", "")})
                elif kind == "on_tool_end":
                    yield _sse("tool_end", {"name": event.get("name", "")})
        except HTTPException as exc:
            yield _sse("error", {"message": str(exc.detail), "status": exc.status_code})
            return
        except Exception as exc:
            # 流已建立后无法改状态码，错误以事件形式下发并记录日志。
            logger.exception("chat stream 处理失败")
            yield _sse("error", {"message": f"处理失败：{exc}"})
            return
        # 直接读最终状态：chat 与 direct_reply 两条路径都覆盖，
        # 只靠 delta/to_answer 事件会漏掉非 chat 路径的答案。
        state = None
        try:
            # 主图 checkpointer 为 AsyncPostgresSaver：必须用异步 aget_state，
            # 同步 get_state 会抛错被吞掉，导致审批事件丢失 + 空气泡。
            state = await graph.aget_state(config)
            final_answer = (state.values or {}).get("answer", "") or final_answer
        except Exception:  # noqa: BLE001
            final_answer = final_answer or "".join(chunks)
        # 审批挂起：图在 approval_gate 停下等用户决策，下发审批事件供
        # 前端渲染审批卡片（决策走 /approvals/{thread_id}/decision）。
        if state is not None and state.next:
            for task in state.tasks or ():
                fired = False
                for graph_interrupt in getattr(task, "interrupts", ()) or ():
                    fired = True
                    interrupt_payload = graph_interrupt.value or {}
                    yield _sse(
                        "approval_required",
                        {
                            "thread_id": thread_id,
                            "action": interrupt_payload.get("action"),
                            "risk_level": interrupt_payload.get("risk_level"),
                            "reason": interrupt_payload.get("reason", ""),
                            "suggestion": interrupt_payload.get("suggestion", ""),
                        },
                    )
                if fired:
                    break
        yield _sse(
            "done",
            {"answer": final_answer or "".join(chunks), "trace_id": trace_id},
        )

    return StreamingResponse(event_stream(), media_type="text/event-stream")
