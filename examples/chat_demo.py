"""chat 子图演示：真实调用 LLM，验证「把客厅灯叫主灯」落库与覆盖。

运行：uv run python -m examples.chat_demo
两轮相同话术分别用独立会话执行，第二轮应仍是同一行偏好（upsert 覆盖）。
"""

import asyncio
import uuid

from langchain_core.messages import HumanMessage
from sqlalchemy import select

from app.agent.chat import agent_graph
from app.database import session_factory
from app.main import application  # noqa: F401  导入即初始化 Langfuse/OTel 全局客户端
from app.models import Home, Preference
from db.seeds.load_family import SEED_HOME_NAME

QUESTION = "把客厅灯叫主灯"


async def _load_seed_home_id() -> int | None:
    async with session_factory() as session:
        return (
            (await session.execute(select(Home.id).where(Home.name == SEED_HOME_NAME)))
            .scalars()
            .first()
        )


async def _print_preferences(home_id: int) -> None:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Preference)
                    .where(Preference.home_id == home_id)
                    .order_by(Preference.id)
                )
            )
            .scalars()
            .all()
        )
    print(
        "当前偏好：",
        [(p.pref_key, p.pref_value, f"synthetic={p.synthetic}") for p in rows] or "无",
    )


async def main() -> None:
    home_id = await _load_seed_home_id()
    if home_id is None:
        print("种子数据未加载，请先执行 uv run python -m db.seeds.load_family")
        return

    for round_no in (1, 2):
        print(f"--- 第 {round_no} 轮：{QUESTION}")
        result = await agent_graph.ainvoke(
            {"messages": [HumanMessage(content=QUESTION)], "home_id": home_id},
            config={"configurable": {"thread_id": f"demo-{uuid.uuid4()}"}},
        )
        print("助手：", result["answer"], "| intent:", result["intent"])
        await _print_preferences(home_id)


if __name__ == "__main__":
    asyncio.run(main())
