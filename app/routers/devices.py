from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Device, DeviceEvent, Room
from app.schemas import DeviceEventCreate, DeviceEventRead, DeviceRead, DeviceUpdate

router = APIRouter(tags=["devices"])


async def _get_device_or_404(device_id: int, session: AsyncSession) -> Device:
    device = await session.get(Device, device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="device not found"
        )
    return device


@router.get("/devices/{device_id}", response_model=DeviceRead)
async def get_device(
    device_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> Device:
    """查询单个设备详情。"""
    return await _get_device_or_404(device_id, session)


@router.patch("/devices/{device_id}", response_model=DeviceRead)
async def update_device(
    device_id: int,
    payload: DeviceUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Device:
    """部分更新设备信息，改名时仍需满足家庭内设备名唯一。"""
    device = await _get_device_or_404(device_id, session)
    changes = payload.model_dump(exclude_unset=True)

    room_id = changes.get("room_id")
    if room_id is not None:
        room = await session.get(Room, room_id)
        if room is None or room.home_id != device.home_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="room not found in this home",
            )

    new_name = changes.get("name")
    if new_name is not None and new_name != device.name:
        duplicate = await session.execute(
            select(Device).where(
                Device.home_id == device.home_id,
                Device.name == new_name,
                Device.id != device.id,
            )
        )
        if duplicate.scalars().first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="device name already exists in this home",
            )

    for field, value in changes.items():
        setattr(device, field, value)

    try:
        await session.commit()
    except IntegrityError:
        # 并发改名可能绕过应用层预检查；数据库唯一约束兜底为业务冲突。
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="device name already exists in this home",
        ) from None
    await session.refresh(device)
    return device


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> None:
    """删除设备，其事件通过 ORM 级联一并删除。"""
    device = await _get_device_or_404(device_id, session)
    await session.delete(device)
    await session.commit()


@router.post(
    "/devices/{device_id}/events",
    response_model=DeviceEventRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_device_event(
    device_id: int,
    payload: DeviceEventCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceEvent:
    """为设备写入一条事件。"""
    await _get_device_or_404(device_id, session)
    event = DeviceEvent(**payload.model_dump(), device_id=device_id)
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


@router.get("/devices/{device_id}/events", response_model=list[DeviceEventRead])
async def list_device_events(
    device_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[DeviceEvent]:
    """按时间倒序列出设备事件。"""
    await _get_device_or_404(device_id, session)
    result = await session.execute(
        select(DeviceEvent)
        .where(DeviceEvent.device_id == device_id)
        .order_by(DeviceEvent.id.desc())
    )
    return list(result.scalars())
