from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    rooms: Mapped[list["Room"]] = relationship(back_populates="home")
    members: Mapped[list["Member"]] = relationship(back_populates="home")
    devices: Mapped[list["Device"]] = relationship(back_populates="home")


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    home: Mapped["Home"] = relationship(back_populates="rooms")
    devices: Mapped[list["Device"]] = relationship(back_populates="room")


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    home: Mapped["Home"] = relationship(back_populates="members")


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    home: Mapped["Home"] = relationship(back_populates="devices")
    room: Mapped["Room | None"] = relationship(back_populates="devices")
    events: Mapped[list["DeviceEvent"]] = relationship(back_populates="device")


class DeviceEvent(Base):
    """设备上报的事件，第 1 周只做落库，不做实时处理。"""

    __tablename__ = "device_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    # 事件类型，如 power_on / state_change / heartbeat。
    event_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    synthetic: Mapped[bool] = mapped_column(default=True)
    source_type: Mapped[str] = mapped_column(String(20), default="synthetic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    device: Mapped["Device"] = relationship(back_populates="events")
