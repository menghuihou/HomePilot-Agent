"""Home Context Resolver：把家庭上下文组装成 LLM 可用的结构。

本模块只做数据组装，不调用 LLM；Router 与 chat 子图消费这里的产物。
"""

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Home, Member, Preference, Room
from app.schemas import HomeRead, MemberRead, PreferenceRead, RoomRead

# 家庭当前模式固定用该键存入 preferences 表。
HOME_MODE_KEY = "home_mode"
# 无 home_mode 偏好时的默认模式。
DEFAULT_HOME_MODE = "normal"


class HomeContext(BaseModel):
    """Resolver 产物：组装后供 Router 与子图构建 prompt 使用。"""

    home: HomeRead
    rooms: list[RoomRead]
    members: list[MemberRead]
    preferences: list[PreferenceRead]
    # 当前家庭模式，取自 pref_key=home_mode 的偏好行，缺省 normal。
    current_mode: str = DEFAULT_HOME_MODE


async def resolve_home_context(home_id: int, session: AsyncSession) -> HomeContext:
    """加载指定家庭的完整上下文，家庭不存在时抛 404（沿用项目既有异常风格）。

    每类实体一次批量查询，禁止在循环内访问数据库。
    """
    home = await session.get(Home, home_id)
    if home is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="home not found"
        )

    result = await session.execute(
        select(Room).where(Room.home_id == home_id).order_by(Room.id)
    )
    rooms = result.scalars().all()

    result = await session.execute(
        select(Member).where(Member.home_id == home_id).order_by(Member.id)
    )
    members = result.scalars().all()

    result = await session.execute(
        select(Preference).where(Preference.home_id == home_id).order_by(Preference.id)
    )
    preferences = result.scalars().all()

    current_mode = next(
        (p.pref_value for p in preferences if p.pref_key == HOME_MODE_KEY),
        DEFAULT_HOME_MODE,
    )

    return HomeContext(
        home=HomeRead.model_validate(home),
        rooms=[RoomRead.model_validate(room) for room in rooms],
        members=[MemberRead.model_validate(member) for member in members],
        preferences=[PreferenceRead.model_validate(p) for p in preferences],
        current_mode=current_mode,
    )
