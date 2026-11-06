"""审批决策接口：按会话恢复被 interrupt 挂起的高风险操作审批。"""

import logging

from fastapi import APIRouter, HTTPException, status
from langgraph.types import Command
from sqlalchemy import select

from app.agent.diagnose import get_diagnose_graph
from app.database import session_factory
from app.models import Approval
from app.schemas import ApprovalDecisionRequest, ApprovalDecisionResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["approvals"])


@router.post(
    "/approvals/{thread_id}/decision",
    response_model=ApprovalDecisionResponse,
)
async def decide_approval(
    thread_id: str, body: ApprovalDecisionRequest
) -> ApprovalDecisionResponse:
    """按会话恢复挂起的审批：approved 执行，rejected 放弃执行。

    多会话隔离：只按 thread_id 查找本会话的 pending 审批，无挂起记录
    返回 404，防止误恢复其他会话的审批。
    """
    async with session_factory() as session:
        pending = (
            (
                await session.execute(
                    select(Approval).where(
                        Approval.thread_id == thread_id,
                        Approval.decision.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no pending approval for thread",
        )

    graph = await get_diagnose_graph()
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(Command(resume=body.model_dump()), config=config)
    diagnosis = result.get("final_diagnosis") or {}
    logger.info("审批决策完成：thread=%s decision=%s", thread_id, body.decision)
    return ApprovalDecisionResponse(
        thread_id=thread_id,
        decision=body.decision,
        status="resumed",
        report=diagnosis.get("report"),
    )
