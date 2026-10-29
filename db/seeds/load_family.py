"""加载演示种子数据。

用法：

    uv run python -m db.seeds.load_family

幂等约定：种子家庭已存在时直接跳过，重复执行不会产生重复数据。
synthetic / source_type 由模型默认值写入，全部标记为演示数据。
"""

import asyncio

from sqlalchemy import func, select

from app.database import session_factory
from app.models import Device, DeviceEvent, Home, Member, Room

SEED_HOME_NAME = "演示之家"


async def _already_seeded(session) -> bool:
    result = await session.execute(select(Home.id).where(Home.name == SEED_HOME_NAME))
    return result.scalars().first() is not None


async def _seed() -> None:
    async with session_factory() as session:
        if await _already_seeded(session):
            print("种子数据已存在，跳过写入。")
            return

        home = Home(name=SEED_HOME_NAME, address="示例市示例区示例路 1 号")
        session.add(home)
        # flush 后才能拿到自增主键，供子表外键使用。
        await session.flush()

        rooms = {
            name: Room(name=name, room_type=room_type, home_id=home.id)
            for name, room_type in (
                ("客厅", "living_room"),
                ("卧室", "bedroom"),
                ("厨房", "kitchen"),
            )
        }
        session.add_all(rooms.values())
        # flush 后房间才有自增 id，设备的 room_id 外键才能正确赋值。
        await session.flush()

        session.add_all(
            [
                Member(name="小明", role="admin", home_id=home.id),
                Member(name="小红", role="member", home_id=home.id),
            ]
        )

        # 网关是家庭级设备，刻意不分配房间，覆盖 room_id 为空的场景。
        devices = [
            Device(
                name="客厅灯",
                device_type="light",
                vendor="acme",
                home_id=home.id,
                room_id=rooms["客厅"].id,
            ),
            Device(
                name="卧室空调",
                device_type="thermostat",
                vendor="acme",
                home_id=home.id,
                room_id=rooms["卧室"].id,
            ),
            Device(
                name="家庭网关",
                device_type="gateway",
                vendor="acme",
                home_id=home.id,
            ),
        ]
        session.add_all(devices)
        await session.flush()

        session.add_all(
            [
                DeviceEvent(
                    device_id=devices[0].id,
                    event_type="power_on",
                    payload={"brightness": 80},
                ),
                DeviceEvent(
                    device_id=devices[0].id,
                    event_type="state_change",
                    payload={"brightness": 40},
                ),
                DeviceEvent(
                    device_id=devices[1].id,
                    event_type="power_on",
                    payload={"target_temperature": 26},
                ),
                DeviceEvent(
                    device_id=devices[2].id,
                    event_type="heartbeat",
                    payload={"online": True},
                ),
            ]
        )

        await session.commit()
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
