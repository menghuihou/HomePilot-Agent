from datetime import datetime
from typing import Any, Literal

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


class MemoryScope(BaseModel):
    """长期记忆的隔离范围；home_id 必填，家庭级偏好可不传 member_id。"""

    home_id: int = Field(gt=0)
    member_id: int | None = Field(default=None, gt=0)
    device_type: str | None = Field(default=None, max_length=30)


class MemoryItem(BaseModel):
    """跨记忆后端统一的数据边界，Agent 不直接暴露 Mem0 的返回结构。"""

    scope: MemoryScope
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)
    source: str = Field(default="chat_tool", max_length=50)
    updated_at: datetime | None = None


class MemoryQuery(BaseModel):
    """记忆读取 / 删除条件；key 与 key_prefix 同时给出时 key 优先。"""

    scope: MemoryScope
    key: str | None = Field(default=None, min_length=1, max_length=100)
    key_prefix: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


class Citation(BaseModel):
    """RAG 引用来源；字段缺失或跨域都会被确定性校验拒绝。"""

    source_id: int = Field(gt=0)
    chunk_id: int = Field(gt=0)
    source_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=200)
    chunk_index: int = Field(ge=0)
    device_type: str = Field(min_length=1, max_length=30)
    knowledge_version: str = Field(min_length=1, max_length=30)
    score: float = Field(ge=0.0, le=1.0)


class RetrievalHit(BaseModel):
    """单条融合后的知识 chunk；citation 与正文绑定，不能被 LLM 单独改写。"""

    citation: Citation
    chunk_text: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    """一次诊断 / 查询的检索结果；citations 是 hits 的确定性投影。"""

    query: str = Field(min_length=1)
    hits: list[RetrievalHit] = Field(max_length=20)

    @property
    def citations(self) -> list[Citation]:
        """供诊断状态与 LLM prompt 使用，避免手动复制 citation 字段。"""
        return [hit.citation for hit in self.hits]
