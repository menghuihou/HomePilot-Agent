"""PostgreSQL 长期记忆后端：复用既有 preferences 表的默认实现。"""

from langfuse import observe
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Home, Member, Preference
from app.schemas import MemoryItem, MemoryQuery, MemoryScope


class MemoryStoreError(ValueError):
    """记忆数据在当前后端无法安全写入；错误内容面向用户兜底提示。"""


class DatabaseMemoryBackend:
    """PostgreSQL 偏好记忆后端，家庭级和成员级偏好共存。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def store(self, item: MemoryItem) -> MemoryItem:
        """按（家庭、成员、键）做 PostgreSQL upsert，返回落库后的值。"""
        async with self.session_factory() as session:
            await self._validate_scope(session, item)
            stmt = (
                pg_insert(Preference)
                .values(
                    home_id=item.scope.home_id,
                    member_id=item.scope.member_id,
                    pref_key=item.key,
                    pref_value=item.value,
                    source_type=item.source,
                )
                .on_conflict_do_update(
                    constraint="uq_preferences_home_member_key",
                    set_={
                        "pref_value": item.value,
                        "source_type": item.source,
                        "updated_at": func.now(),
                    },
                )
                .returning(Preference)
            )
            preference = (await session.execute(stmt)).scalar_one()
            await session.commit()

        return MemoryItem(
            scope=item.scope,
            key=preference.pref_key,
            value=preference.pref_value,
            source=preference.source_type,
            updated_at=preference.updated_at,
        )

    async def retrieve(self, query: MemoryQuery) -> list[MemoryItem]:
        """按家庭读取偏好；member_id 未传时不额外限定成员。"""
        async with self.session_factory() as session:
            result = await session.execute(
                select(Preference)
                .where(*_memory_conditions(query))
                .order_by(Preference.updated_at.desc(), Preference.id.desc())
                .limit(query.limit)
            )
            preferences = result.scalars().all()

        items = [
            MemoryItem(
                scope=_scope_from_preference(preference),
                key=preference.pref_key,
                value=preference.pref_value,
                source=preference.source_type,
                updated_at=preference.updated_at,
            )
            for preference in preferences
        ]
        _memory_retrieve_observation(
            home_id=query.scope.home_id,
            member_id=query.scope.member_id,
            key=query.key,
            key_prefix=query.key_prefix,
            limit=query.limit,
            result_count=len(items),
        )
        return items

    async def forget(self, query: MemoryQuery) -> int:
        """删除命中偏好并返回数量；查询条件与 retrieve 保持一致。"""
        conditions = _memory_conditions(query)
        async with self.session_factory() as session:
            result = await session.execute(
                delete(Preference).where(*conditions).returning(Preference.id)
            )
            deleted_ids = result.scalars().all()
            await session.commit()
        return len(deleted_ids)

    @staticmethod
    async def _validate_scope(session: AsyncSession, item: MemoryItem) -> None:
        """写入前校验家庭与成员关系，避免把外键冲突转成难懂的运行时错误。"""
        home = await session.get(Home, item.scope.home_id)
        if home is None:
            raise MemoryStoreError(f"home {item.scope.home_id} not found")

        if item.scope.member_id is None:
            return
        member = await session.get(Member, item.scope.member_id)
        if member is None or member.home_id != item.scope.home_id:
            raise MemoryStoreError("member not found in this home")


def _memory_conditions(query: MemoryQuery) -> list:
    """把统一记忆查询转成 preferences 表过滤条件。"""
    conditions = [Preference.home_id == query.scope.home_id]
    if query.scope.member_id is not None:
        conditions.append(Preference.member_id == query.scope.member_id)
    if query.key is not None:
        conditions.append(Preference.pref_key == query.key)
    elif query.key_prefix is not None:
        conditions.append(Preference.pref_key.startswith(query.key_prefix))
    return conditions


@observe(name="memory-retrieve")
def _memory_retrieve_observation(
    *,
    home_id: int,
    member_id: int | None,
    key: str | None,
    key_prefix: str | None,
    limit: int,
    result_count: int,
) -> dict:
    """记录检索摘要；偏好值属于原始上下文，不进入可观测性输出。"""
    return {
        "home_id": home_id,
        "member_id": member_id,
        "key": key,
        "key_prefix": key_prefix,
        "limit": limit,
        "result_count": result_count,
    }


def _scope_from_preference(preference: Preference) -> MemoryScope:
    """从数据库行还原完整 scope，成员级偏好不会退化成家庭级记忆。"""
    return MemoryScope(
        home_id=preference.home_id,
        member_id=preference.member_id,
    )
