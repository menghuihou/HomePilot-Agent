from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Home, Member, Preference
from app.schemas import PreferenceCreate, PreferenceRead

router = APIRouter(tags=["preferences"])


async def _get_home_or_404(home_id: int, session: AsyncSession) -> Home:
    home = await session.get(Home, home_id)
    if home is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="home not found"
        )
    return home


@router.put("/homes/{home_id}/preferences", response_model=PreferenceRead)
async def upsert_preference(
        home_id: int,
        payload: PreferenceCreate,
        session: Annotated[AsyncSession, Depends(get_session)],
) -> Preference:
    """按（家庭、成员、键）唯一写入偏好，键已存在时覆盖原值。"""
    await _get_home_or_404(home_id, session)

    member_id = payload.member_id
    if member_id is not None:
        member = await session.get(Member, member_id)
        if member is None or member.home_id != home_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="member not found in this home",
            )

    stmt = (
        pg_insert(Preference)
        .values(
            home_id=home_id,
            member_id=member_id,
            pref_key=payload.pref_key,
            pref_value=payload.pref_value,
        )
        .on_conflict_do_update(
            constraint="uq_preferences_home_member_key",
            set_={"pref_value": payload.pref_value, "updated_at": func.now()},
        )
        .returning(Preference)
    )
    preference = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return preference


@router.get("/homes/{home_id}/preferences", response_model=list[PreferenceRead])
async def list_preferences(
        home_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[Preference]:
    """列出指定家庭下的全部偏好。"""
    await _get_home_or_404(home_id, session)
    result = await session.execute(
        select(Preference).where(Preference.home_id == home_id).order_by(Preference.id)
    )
    return list(result.scalars())
