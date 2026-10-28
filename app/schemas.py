from datetime import datetime

from pydantic import BaseModel, ConfigDict


class HomeCreate(BaseModel):
    name: str
    address: str | None = None


class HomeRead(HomeCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class RoomCreate(BaseModel):
    name: str
    room_type: str | None = None


class RoomRead(RoomCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    home_id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class MemberCreate(BaseModel):
    name: str
    role: str = "member"


class MemberRead(MemberCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    home_id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class DeviceCreate(BaseModel):
    name: str
    device_type: str
    vendor: str | None = None
    room_id: int | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    device_type: str | None = None
    vendor: str | None = None
    room_id: int | None = None


class DeviceRead(DeviceCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    home_id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class DeviceEventCreate(BaseModel):
    event_type: str
    payload: dict | None = None


class DeviceEventRead(DeviceEventCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    occurred_at: datetime
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime
