"""Router 演示：对三条典型话术做真实意图判定，结果可在 Langfuse 查看。

运行：uv run python -m examples.router_demo
会真实调用 LLM（3 次），generation 名称固定为 intent-router。
"""

import asyncio

from langchain_core.messages import HumanMessage
from sqlalchemy import select

from app.agent.context import resolve_home_context
from app.agent.router import (
    classify_intent,
    reply_for_intent,
    summarize_home_context,
)
from app.database import session_factory
from app.main import application  # noqa: F401  导入即初始化 Langfuse/OTel 全局客户端
from app.models import Home
from db.seeds.load_family import SEED_HOME_NAME

SAMPLES = ["把客厅灯叫主灯", "客厅灯怎么不亮了", "现在是什么模式"]


async def _load_seed_context():
    """加载种子家庭上下文；未加载种子数据时返回 None。"""
    async with session_factory() as session:
        home_id = (
            (await session.execute(select(Home.id).where(Home.name == SEED_HOME_NAME)))
            .scalars()
            .first()
        )
        if home_id is None:
            return None
        return await resolve_home_context(home_id, session)


def main() -> None:
    context = asyncio.run(_load_seed_context())
    if context is None:
        print("种子数据未加载，请先执行 uv run python -m db.seeds.load_family")
        return

    print(f"家庭上下文摘要：\n{summarize_home_context(context)}\n")
    for text in SAMPLES:
        intent = classify_intent([HumanMessage(content=text)], context)
        reply = reply_for_intent(intent, context)
        print(f"{text!r} -> {intent}" + (f" | {reply}" if reply else ""))


if __name__ == "__main__":
    main()
