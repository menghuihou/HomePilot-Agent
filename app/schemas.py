from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HomeCreate(BaseModel):
    name: str = Field(max_length=100)
    address: str | None = Field(default=None, max_length=200)


class HomeRead(HomeCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class RoomCreate(BaseModel):
    name: str = Field(max_length=50)
    room_type: str | None = Field(default=None, max_length=30)


class RoomRead(RoomCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    home_id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class MemberCreate(BaseModel):
    name: str = Field(max_length=50)
    role: str = Field(default="member", max_length=20)


class MemberRead(MemberCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    home_id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class DeviceCreate(BaseModel):
    name: str = Field(max_length=100)
    device_type: str = Field(max_length=30)
    vendor: str | None = Field(default=None, max_length=50)
    room_id: int | None = None


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    device_type: str | None = Field(default=None, max_length=30)
    vendor: str | None = Field(default=None, max_length=50)
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
    event_type: str = Field(max_length=30)
    payload: dict | None = None


class ApprovalDecisionRequest(BaseModel):
    """审批决策请求：approved 放行执行，rejected 拒绝执行。"""

    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


class ApprovalDecisionResponse(BaseModel):
    """审批决策响应：恢复后的最终状态摘要。"""

    thread_id: str
    decision: str
    status: str
    report: str | None = None


class DeviceEventRead(DeviceEventCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    occurred_at: datetime
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime


class PreferenceCreate(BaseModel):
    member_id: int | None = None
    pref_key: str = Field(max_length=100)
    pref_value: str = Field(max_length=100)


class PreferenceRead(PreferenceCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    home_id: int
    synthetic: bool
    source_type: str
    created_at: datetime
    updated_at: datetime
