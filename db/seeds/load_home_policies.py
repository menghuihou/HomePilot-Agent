"""加载家庭模式种子数据（第 6 周第 2 步）。

用法：

    uv run python -m db.seeds.load_home_policies

幂等约定（对齐 load_family）：
- 空调设备按名称存在性检查，缺失才创建；
- 模式策略按 (home_id, mode) 存在性检查（数据库唯一约束兜底），缺失才插入；
- 初始模式历史：家庭没有任何 home_modes 记录时补一条 home 模式；
- 能耗演示事件按 (device_id, event_type=energy_report) 存在性检查，
  已有能耗事件的设备整体跳过补写。
重复执行不产生重复数据；已存在条目不会被修改或删除。
synthetic / source_type 由模型默认值写入，全部标记为演示数据。

device_rules 采用 by_device_type 口径（项目设备模型只有 device_type，
尚无 2A.4 能力模型），由第 3 步 plan_mode_transition 消费：
{设备类型: {目标状态字段: 值, ..., "risk": low/medium/high}}。
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.database import session_factory
from app.models import Device, DeviceEvent, Home, HomeModeRecord, HomePolicy

# 复用演示之家及其房间，保证本脚本在全新库上可独立跑通。
from db.seeds.load_family import _ensure_home, _ensure_rooms

# 第 6 周新增的空调域种子设备（device_type=ac），供空调 MCP Server
# （第 6 步）与能耗聚合（第 7 步）使用。网关等基础设施设备不参与模式迁移。
SEED_AC_DEVICES: tuple[tuple[str, str, str], ...] = (
    ("客厅空调", "ac", "客厅"),
    ("书房空调", "ac", "书房"),
)

# 4 个 P0 模式的期望状态规则。exclusions 统一排除网关（基础设施，
# 模式切换不允许把它关掉）——按设备名匹配，改名即失效，漂移由
# unmatched_exclusions 告警；conflict_checks 的标识由 detect_conflicts
# 的确定性实现消费（第 3 步）。
SEED_POLICIES: dict[str, dict] = {
    "home": {
        "by_device_type": {
            "light": {"power": "on", "risk": "low"},
            "ac": {"power": "on", "temperature": 26, "risk": "medium"},
        },
        "exclusions": ["家庭网关"],
        "conflict_checks": [],
    },
    "away": {
        "by_device_type": {
            "light": {"power": "off", "risk": "low"},
            "ac": {"power": "off", "risk": "medium"},
        },
        # 离家落锁是家庭级高风险动作：当前种子无锁设备，规则声明供
        # 纯函数测试与后续接入真实锁设备使用。
        "lock": {"target": "locked", "risk": "high"},
        "exclusions": ["家庭网关"],
        "conflict_checks": ["window_open_blocks_ac_off"],
    },
    "night": {
        "by_device_type": {
            "light": {"power": "on", "brightness": 20, "risk": "low"},
            "ac": {"power": "on", "temperature": 27, "risk": "medium"},
        },
        "exclusions": ["家庭网关"],
        "conflict_checks": [],
    },
    "sleep": {
        "by_device_type": {
            "light": {"power": "off", "risk": "low"},
            "ac": {"power": "on", "temperature": 27, "risk": "medium"},
        },
        # 断言 #19 口径：睡眠模式媒体设备不自动播放，夜灯允许低亮度开启。
        "mute_media": True,
        "allow_night_light": True,
        "exclusions": ["家庭网关"],
        "conflict_checks": [],
    },
}

# 能耗演示事件口径：7 天 × 每天 4 个采样点（2/8/14/20 点，6 小时区间），
# kwh 按白天高、夜间低的固定模式生成，供第 7 步按日聚合独立测试。
ENERGY_DAYS = 7
ENERGY_SAMPLE_HOURS: tuple[int, ...] = (2, 8, 14, 20)
ENERGY_KWH_BY_HOUR: dict[int, float] = {2: 0.5, 8: 1.1, 14: 1.4, 20: 0.7}


async def _ensure_ac_devices(session, home: Home, rooms: dict) -> dict[str, Device]:
    """按名称补齐空调域种子设备，返回现有 + 新建的全量映射。"""
    result = await session.execute(select(Device).where(Device.home_id == home.id))
    devices = {device.name: device for device in result.scalars()}
    created = [
        Device(
            name=name,
            device_type=device_type,
            vendor="acme",
            home_id=home.id,
            room_id=rooms[room].id,
        )
        for name, device_type, room in SEED_AC_DEVICES
        if name not in devices
    ]
    session.add_all(created)
    await session.flush()
    for device in created:
        devices[device.name] = device
    return devices


async def _ensure_policies(session, home: Home) -> int:
    result = await session.execute(
        select(HomePolicy.mode).where(HomePolicy.home_id == home.id)
    )
    existing = set(result.scalars())
    created = [
        HomePolicy(home_id=home.id, mode=mode, device_rules=device_rules)
        for mode, device_rules in SEED_POLICIES.items()
        if mode not in existing
    ]
    session.add_all(created)
    return len(created)


async def _ensure_initial_mode(session, home: Home) -> int:
    """家庭没有任何模式历史时补一条 home 模式，作为当前模式的种子值。"""
    result = await session.execute(
        select(func.count())
        .select_from(HomeModeRecord)
        .where(HomeModeRecord.home_id == home.id)
    )
    if result.scalar_one() > 0:
        return 0
    session.add(
        HomeModeRecord(
            home_id=home.id,
            mode="home",
            state={"trigger_source": "seed"},
            reason="种子初始化：默认在家模式",
        )
    )
    return 1


def _energy_events(device_id: int, now: datetime) -> list[DeviceEvent]:
    """生成单台空调过去 7 天 × 4 采样点的能耗事件（全部严格在过去）。"""
    events: list[DeviceEvent] = []
    for days_ago in range(ENERGY_DAYS, 0, -1):
        day = (now - timedelta(days=days_ago)).replace(
            minute=0, second=0, microsecond=0
        )
        for hour in ENERGY_SAMPLE_HOURS:
            events.append(
                DeviceEvent(
                    device_id=device_id,
                    event_type="energy_report",
                    payload={"kwh": ENERGY_KWH_BY_HOUR[hour]},
                    occurred_at=day.replace(hour=hour),
                )
            )
    return events


async def _ensure_energy_events(session, devices: dict[str, Device]) -> int:
    """为没有能耗事件的空调设备补 7 天能耗数据（按设备整体跳过）。"""
    ac_ids = [devices[name].id for name, _, _ in SEED_AC_DEVICES]
    result = await session.execute(
        select(DeviceEvent.device_id).where(
            DeviceEvent.device_id.in_(ac_ids),
            DeviceEvent.event_type == "energy_report",
        )
    )
    seeded = set(result.scalars())
    now = datetime.now(UTC)
    pending = [
        event
        for name, _, _ in SEED_AC_DEVICES
        if (device_id := devices[name].id) not in seeded
        for event in _energy_events(device_id, now)
    ]
    session.add_all(pending)
    return len(pending)


async def _seed() -> None:
    async with session_factory() as session:
        home = await _ensure_home(session)
        rooms = await _ensure_rooms(session, home)
        devices = await _ensure_ac_devices(session, home, rooms)
        policies_created = await _ensure_policies(session, home)
        modes_created = await _ensure_initial_mode(session, home)
        events_created = await _ensure_energy_events(session, devices)

        await session.commit()
        if not policies_created and not modes_created and not events_created:
            print("种子数据已齐全，跳过写入。")
        else:
            print(
                f"本次增量写入：模式策略 {policies_created} 条，"
                f"模式历史 {modes_created} 条，能耗事件 {events_created} 条。"
            )
        await _print_summary(session, home.id)


async def _print_summary(session, home_id: int) -> None:
    """输出种子结果统计，便于人工确认写入成功。"""

    async def count(model, *conditions) -> int:
        result = await session.execute(
            select(func.count()).select_from(model).where(*conditions)
        )
        return result.scalar_one()

    policies = await count(HomePolicy, HomePolicy.home_id == home_id)
    modes = await count(HomeModeRecord, HomeModeRecord.home_id == home_id)
    energy = await count(
        DeviceEvent,
        DeviceEvent.device_id.in_(
            select(Device.id).where(
                Device.home_id == home_id,
                Device.device_type == "ac",
            )
        ),
        DeviceEvent.event_type == "energy_report",
    )
    print(
        f"当前累计：模式策略 {policies} 条，模式历史 {modes} 条，能耗事件 {energy} 条。"
    )


if __name__ == "__main__":
    asyncio.run(_seed())
