"""审批决策接口：按会话恢复被 interrupt 挂起的高风险操作审批。"""

import logging

from fastapi import APIRouter, HTTPException, status
from langgraph.types import Command
from sqlalchemy import select

from app.agent.chat import get_agent_graph
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
        latest = (
            (
                await session.execute(
                    select(Approval)
                    .where(Approval.thread_id == thread_id)
                    .order_by(Approval.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    if pending is None:
        if latest is not None and latest.decision is not None:
            # 幂等：同一审批重复提交（双击/刷新）返回已处理状态，
            # 而不是 404 让前端弹失败提示。
            logger.info(
                "审批重复提交：thread=%s 已处理（%s）", thread_id, latest.decision
            )
            return ApprovalDecisionResponse(
                thread_id=thread_id,
                decision=latest.decision,
                status="already_processed",
                report=None,
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no pending approval for thread",
        )

    # 恢复必须用发起诊断的同一份主图实例（同实例同 checkpointer），
    # interrupt 状态持久化在主图的 Postgres checkpointer 里。
    graph = await get_agent_graph()
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
