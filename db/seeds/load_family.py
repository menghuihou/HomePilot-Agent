"""加载演示种子数据。

用法：

    uv run python -m db.seeds.load_family

幂等约定（第 4 周起为按条目增量升级，而非发现家庭即整体跳过）：
- 家庭 / 房间 / 成员 / 设备按唯一键存在性检查，缺失才创建；
- 演示事件按 (device_id, event_type) 存在性检查，缺失才补写；
- 重复执行不产生重复数据；已存在条目（含用户手工新增设备）不会被修改或删除。
synthetic / source_type 由模型默认值写入，全部标记为演示数据。
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.database import session_factory
from app.models import Device, DeviceEvent, Home, Member, Room

SEED_HOME_NAME = "演示之家"

# 房间名 → 房间类型。家庭级设备（如网关）不分配房间，不在本表。
SEED_ROOMS: tuple[tuple[str, str], ...] = (
    ("客厅", "living_room"),
    ("卧室", "bedroom"),
    ("厨房", "kitchen"),
    ("书房", "study"),
    ("走廊", "corridor"),
)

SEED_MEMBERS: tuple[tuple[str, str], ...] = (
    ("小明", "admin"),
    ("小红", "member"),
)

# 种子设备：(设备名, 设备类型, 房间名)。房间名必须在 SEED_ROOMS 中；None 表示家庭级设备。
# 共 8 台：4 灯 + 2 传感器 + 1 温控 + 1 网关，其中客厅温湿度传感器（低电量）
# 与家庭网关（离线 3 小时）是第 4 周巡检演示的两条预置异常。
SEED_DEVICES: tuple[tuple[str, str, str | None], ...] = (
    ("客厅灯", "light", "客厅"),
    ("卧室灯", "light", "卧室"),
    ("书房灯", "light", "书房"),
    ("走廊灯", "light", "走廊"),
    ("客厅温湿度传感器", "sensor", "客厅"),
    ("卧室人体传感器", "sensor", "卧室"),
    ("卧室空调", "thermostat", "卧室"),
    ("家庭网关", "gateway", None),
)


def _utcnow() -> datetime:
    """种子时间基准一律 UTC aware，与巡检离线判定口径保持一致。"""
    return datetime.now(UTC)


def _seed_events(now: datetime) -> tuple[tuple[str, str, dict, datetime | None], ...]:
    """演示事件：(设备名, 事件类型, payload, occurred_at)。

    occurred_at 为 None 时用入库当前时间；家庭网关的 offline 事件刻意
    回拨 3 小时，保证第 4 周「离线超时」异常规则在巡检演示中必然命中。
    """
    return (
        ("客厅灯", "power_on", {"brightness": 80}, None),
        ("客厅灯", "state_change", {"brightness": 40}, None),
        ("卧室灯", "power_on", {"brightness": 70}, None),
        ("书房灯", "power_on", {"brightness": 90}, None),
        ("走廊灯", "power_on", {"brightness": 60}, None),
        ("客厅温湿度传感器", "battery_low", {"battery": 8}, None),
        ("卧室人体传感器", "heartbeat", {"online": True}, None),
        ("卧室空调", "power_on", {"target_temperature": 26}, None),
        ("家庭网关", "heartbeat", {"online": True}, None),
        ("家庭网关", "offline", {"online": False}, now - timedelta(hours=3)),
    )


async def _ensure_home(session) -> Home:
    result = await session.execute(select(Home).where(Home.name == SEED_HOME_NAME))
    home = result.scalars().first()
    if home is None:
        home = Home(name=SEED_HOME_NAME, address="示例市示例区示例路 1 号")
        session.add(home)
        # flush 后才能拿到自增主键，供子表外键使用。
        await session.flush()
    return home


async def _ensure_rooms(session, home: Home) -> dict[str, Room]:
    result = await session.execute(select(Room).where(Room.home_id == home.id))
    rooms = {room.name: room for room in result.scalars()}
    created = [
        Room(name=name, room_type=room_type, home_id=home.id)
        for name, room_type in SEED_ROOMS
        if name not in rooms
    ]
    session.add_all(created)
    # flush 后房间才有自增 id，设备的 room_id 外键才能正确赋值。
    await session.flush()
    for room in created:
        rooms[room.name] = room
    return rooms


async def _ensure_members(session, home: Home) -> int:
    result = await session.execute(select(Member.name).where(Member.home_id == home.id))
    existing = set(result.scalars())
    created = [
        Member(name=name, role=role, home_id=home.id)
        for name, role in SEED_MEMBERS
        if name not in existing
    ]
    session.add_all(created)
    return len(created)


async def _ensure_devices(
    session, home: Home, rooms: dict[str, Room]
) -> dict[str, Device]:
    result = await session.execute(select(Device).where(Device.home_id == home.id))
    devices = {device.name: device for device in result.scalars()}
    created = [
        Device(
            name=name,
            device_type=device_type,
            vendor="acme",
            home_id=home.id,
            # 网关等家庭级设备 room 名为 None，刻意覆盖 room_id 为空的场景。
            room_id=rooms[room].id if room else None,
        )
        for name, device_type, room in SEED_DEVICES
        if name not in devices
    ]
    session.add_all(created)
    await session.flush()
    for device in created:
        devices[device.name] = device
    return devices


async def _ensure_events(session, devices: dict[str, Device], now: datetime) -> int:
    device_ids = [device.id for device in devices.values()]
    result = await session.execute(
        select(DeviceEvent.device_id, DeviceEvent.event_type).where(
            DeviceEvent.device_id.in_(device_ids)
        )
    )
    existing = set(result)

    pending: list[DeviceEvent] = []
    for device_name, event_type, payload, occurred_at in _seed_events(now):
        device = devices[device_name]
        if (device.id, event_type) in existing:
            continue
        event = DeviceEvent(device_id=device.id, event_type=event_type, payload=payload)
        if occurred_at is not None:
            event.occurred_at = occurred_at
        pending.append(event)
    session.add_all(pending)
    return len(pending)


async def _seed() -> None:
    async with session_factory() as session:
        now = _utcnow()
        home = await _ensure_home(session)
        rooms = await _ensure_rooms(session, home)
        members_created = await _ensure_members(session, home)
        devices = await _ensure_devices(session, home, rooms)
        events_created = await _ensure_events(session, devices, now)

        await session.commit()
        if not members_created and not events_created:
            print("种子数据已齐全，跳过写入。")
        else:
            print(
                f"本次增量写入：成员 {members_created} 人，事件 {events_created} 条。"
            )
        await _print_summary(session, home.id)


async def _print_summary(session, home_id: int) -> None:
    """输出种子结果统计，便于人工确认写入成功。"""

    async def count(model, *conditions):
        result = await session.execute(
            select(func.count()).select_from(model).where(*conditions)
        )
        return result.scalar_one()

    homes = await count(Home, Home.id == home_id)
    rooms = await count(Room, Room.home_id == home_id)
    members = await count(Member, Member.home_id == home_id)
    devices = await count(Device, Device.home_id == home_id)
    events = await count(
        DeviceEvent,
        DeviceEvent.device_id.in_(select(Device.id).where(Device.home_id == home_id)),
    )
    print(
        "种子数据写入完成："
        f"家庭 {homes} 个，房间 {rooms} 个，成员 {members} 人，"
        f"设备 {devices} 台，事件 {events} 条。"
    )


def main() -> None:
    asyncio.run(_seed())


if __name__ == "__main__":
    main()
