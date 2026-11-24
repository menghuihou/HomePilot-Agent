"""审批流持久化共享件：diagnose 与 home_mode 子图共用的审计落库与放行口径。

- ensure_pending_approval：挂起时确保 pending 审批行（恢复重跑幂等复用）；
- record_approval_decision：决策后更新审批行（只更新不删除，审计基础数据）；
- record_auto_approval：低中风险自动放行与范围阻断的留痕（auto/blocked）；
- approval_allows_execution：fail-close 放行口径（仅 approved 执行），
  评估集据此断言「审批拒绝 → 工具调用数为 0」。
"""

import logging

from sqlalchemy import func, select

from app.database import session_factory
from app.models import Approval

logger = logging.getLogger(__name__)


async def ensure_pending_approval(
    thread_id: str,
    home_id: int,
    decision_info,
    target_device_id: int | None,
) -> int:
    """确保该会话存在 pending 审批记录；恢复时节点重跑直接复用同一行。

    已知并发限制：同会话并发首次进入时，后到的 insert 会违反 pending
    唯一索引（不变量仍由数据库兜底）；MVP 单用户下不处理该竞态。
    """
    async with session_factory() as session:
        existing = (
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
        if existing is not None:
            return existing.id
        approval = Approval(
            thread_id=thread_id,
            home_id=home_id,
            action=decision_info.action,
            target_device_id=target_device_id,
            risk_level=decision_info.risk_level,
            decision=None,
        )
        session.add(approval)
        await session.commit()
        return approval.id


async def record_approval_decision(
    approval_id: int, decision: str, reason: str | None
) -> None:
    """决策后更新审批行；approvals 是审计基础数据，只更新不删除。"""
    async with session_factory() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            logger.warning("审批记录不存在：%s", approval_id)
            return
        approval.decision = decision
        approval.reason = reason
        approval.decided_at = func.now()
        await session.commit()


async def record_auto_approval(
    thread_id: str,
    home_id: int,
    decision_info,
    target_device_id: int | None,
    decision: str,
) -> None:
    """低中风险自动放行与范围阻断的审计落库（decision=auto/blocked）。"""
    async with session_factory() as session:
        approval = Approval(
            thread_id=thread_id,
            home_id=home_id,
            action=decision_info.action,
            target_device_id=target_device_id,
            risk_level=decision_info.risk_level,
            decision=decision,
            reason=decision_info.reason,
            decided_at=func.now(),
        )
        session.add(approval)
        await session.commit()


def approval_allows_execution(decision: str | None) -> bool:
    """审批决策 → 是否执行工具；仅 approved 放行（fail-close 口径）。

    红线接缝：rejected / blocked / 缺失决策一律不执行，评估集据此
    断言「审批拒绝 → 工具调用数为 0」。决策字段缺失同样视为拒绝。
    """
    return decision == "approved"
