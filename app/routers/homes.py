from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Device, Home, Member, Room
from app.schemas import DeviceCreate, DeviceRead, HomeCreate, HomeRead, MemberCreate, MemberRead, RoomCreate, RoomRead

router = APIRouter(tags=["homes"])


@router.post("/homes", response_model=HomeRead, status_code=status.HTTP_201_CREATED)
async def create_home(payload: HomeCreate, session: AsyncSession = Depends(get_session)) -> Home:
    """创建家庭。"""
    home = Home(**payload.model_dump())
    session.add(home)
    await session.commit()
    await session.refresh(home)
    return home


@router.get("/homes", response_model=list[HomeRead])
async def list_homes(session: AsyncSession = Depends(get_session)) -> list[Home]:
    """列出全部家庭。"""
    result = await session.execute(select(Home).order_by(Home.id))
    return list(result.scalars())


async def _get_home_or_404(home_id: int, session: AsyncSession) -> Home:
    home = await session.get(Home, home_id)
    if home is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="home not found")
    return home


@router.post("/homes/{home_id}/rooms", response_model=RoomRead, status_code=status.HTTP_201_CREATED)
async def create_room(home_id: int, payload: RoomCreate, session: AsyncSession = Depends(get_session)) -> Room:
    """在指定家庭下创建房间。"""
    await _get_home_or_404(home_id, session)
    room = Room(**payload.model_dump(), home_id=home_id)
    session.add(room)
    await session.commit()
    await session.refresh(room)
    return room


@router.get("/homes/{home_id}/rooms", response_model=list[RoomRead])
async def list_rooms(home_id: int, session: AsyncSession = Depends(get_session)) -> list[Room]:
    """列出指定家庭下的房间。"""
    await _get_home_or_404(home_id, session)
    result = await session.execute(select(Room).where(Room.home_id == home_id).order_by(Room.id))
    return list(result.scalars())


@router.post("/homes/{home_id}/members", response_model=MemberRead, status_code=status.HTTP_201_CREATED)
async def create_member(home_id: int, payload: MemberCreate, session: AsyncSession = Depends(get_session)) -> Member:
    """在指定家庭下添加成员。"""
    await _get_home_or_404(home_id, session)
    member = Member(**payload.model_dump(), home_id=home_id)
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


@router.get("/homes/{home_id}/members", response_model=list[MemberRead])
async def list_members(home_id: int, session: AsyncSession = Depends(get_session)) -> list[Member]:
    """列出指定家庭下的成员。"""
    await _get_home_or_404(home_id, session)
    result = await session.execute(select(Member).where(Member.home_id == home_id).order_by(Member.id))
    return list(result.scalars())


@router.post("/homes/{home_id}/devices", response_model=DeviceRead, status_code=status.HTTP_201_CREATED)
async def create_device(home_id: int, payload: DeviceCreate, session: AsyncSession = Depends(get_session)) -> Device:
    """在指定家庭下创建设备，校验房间归属和设备名唯一。"""
    await _get_home_or_404(home_id, session)

    room_id = payload.room_id
    if room_id is not None:
        room = await session.get(Room, room_id)
        if room is None or room.home_id != home_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="room not found in this home")

    duplicate = await session.execute(
        select(Device).where(Device.home_id == home_id, Device.name == payload.name)
    )
    if duplicate.scalars().first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="device name already exists in this home")

    device = Device(**payload.model_dump(), home_id=home_id)
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return device


@router.get("/homes/{home_id}/devices", response_model=list[DeviceRead])
async def list_devices(home_id: int, session: AsyncSession = Depends(get_session)) -> list[Device]:
    """列出指定家庭下的设备。"""
    await _get_home_or_404(home_id, session)
    result = await session.execute(select(Device).where(Device.home_id == home_id).order_by(Device.id))
    return list(result.scalars())
