from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Home(Base):
    """家庭，设备数据的根聚合。"""

    __tablename__ = "homes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(200))
    synthetic: Mapped[bool] = mapped_column(default=True)
    source_type: Mapped[str] = mapped_column(String(20), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    rooms: Mapped[list[Room]] = relationship(back_populates="home")
    members: Mapped[list[Member]] = relationship(back_populates="home")
    devices: Mapped[list[Device]] = relationship(back_populates="home")
    preferences: Mapped[list[Preference]] = relationship(back_populates="home")


class Room(Base):
    """家庭内的房间。"""

    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    home_id: Mapped[int] = mapped_column(ForeignKey("homes.id"))
    name: Mapped[str] = mapped_column(String(50))
    # 房间类型，如 living_room / bedroom / kitchen。
    room_type: Mapped[str | None] = mapped_column(String(30))
    synthetic: Mapped[bool] = mapped_column(default=True)
    source_type: Mapped[str] = mapped_column(String(20), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    home: Mapped[Home] = relationship(back_populates="rooms")
    devices: Mapped[list[Device]] = relationship(back_populates="room")


class Member(Base):
    """家庭成员。"""

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    home_id: Mapped[int] = mapped_column(ForeignKey("homes.id"))
    name: Mapped[str] = mapped_column(String(50))
    # 家庭内的角色，如 admin / member。
    role: Mapped[str] = mapped_column(String(20), default="member")
    synthetic: Mapped[bool] = mapped_column(default=True)
    source_type: Mapped[str] = mapped_column(String(20), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    home: Mapped[Home] = relationship(back_populates="members")
    preferences: Mapped[list[Preference]] = relationship(back_populates="member")


class Device(Base):
    """智能设备，必须归属一个家庭，房间可以后续再分配。"""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    home_id: Mapped[int] = mapped_column(ForeignKey("homes.id"))
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"))
    name: Mapped[str] = mapped_column(String(100))
    # 设备类型，如 light / thermostat / sensor。
    device_type: Mapped[str] = mapped_column(String(30))
    vendor: Mapped[str | None] = mapped_column(String(50))
    synthetic: Mapped[bool] = mapped_column(default=True)
    source_type: Mapped[str] = mapped_column(String(20), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    home: Mapped[Home] = relationship(back_populates="devices")
    room: Mapped[Room | None] = relationship(back_populates="devices")
    events: Mapped[list[DeviceEvent]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class DeviceEvent(Base):
    """设备上报的事件，第 1 周只做落库，不做实时处理。"""

    __tablename__ = "device_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    # 事件类型，如 power_on / state_change / heartbeat。
    event_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    synthetic: Mapped[bool] = mapped_column(default=True)
    source_type: Mapped[str] = mapped_column(String(20), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    device: Mapped[Device] = relationship(back_populates="events")


class Preference(Base):
    """用户偏好，同时承载家庭当前模式（pref_key = home_mode）。"""

    __tablename__ = "preferences"

    __table_args__ = (
        # PostgreSQL 默认把唯一约束中的 NULL 视为互不相等；家庭级偏好
        # member_id 为空，必须声明 NULL 不去重，否则同 key 可重复插入。
        UniqueConstraint(
            "home_id",
            "member_id",
            "pref_key",
            name="uq_preferences_home_member_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    home_id: Mapped[int] = mapped_column(ForeignKey("homes.id"))
    # 家庭级偏好为空，成员级偏好指向具体成员。
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    pref_key: Mapped[str] = mapped_column(String(100))
    pref_value: Mapped[str] = mapped_column(String(100))
    synthetic: Mapped[bool] = mapped_column(default=True)
    source_type: Mapped[str] = mapped_column(String(20), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    home: Mapped[Home] = relationship(back_populates="preferences")
    member: Mapped[Member | None] = relationship(back_populates="preferences")
