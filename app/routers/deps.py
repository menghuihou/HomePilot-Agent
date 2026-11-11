"""Router 层共享依赖：把领域数据校验转换为 HTTP 语义。"""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Home


async def get_home_or_404(home_id: int, session: AsyncSession) -> Home:
    """加载家庭；不存在时返回 HTTP 404。"""
    home = await session.get(Home, home_id)
    if home is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="home not found"
        )
    return home
