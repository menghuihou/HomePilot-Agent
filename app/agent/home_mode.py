"""家庭模式迁移确定性核心（第 6 周第 3 步）：计划 / 冲突 / 静音 / 回读。

全部零 LLM、零 DB：策略规则与设备当前状态都从参数传入，输入输出全是
结构化 JSON——第 4 步子图与第 5 步确定性评估执行器共用同一实现。
本模块是总计划确定性断言 #18 / #19 / #20 的唯一实现点。

输入契约（第 4 步子图负责把 DB / MCP 状态归一化成下面的结构）：

- policy: home_policies.device_rules（见 db/seeds/load_home_policies.py），
  键：by_device_type / lock / exclusions / conflict_checks；睡眠模式另有
  mute_media / allow_night_light；
  exclusions 按设备名匹配，设备改名 / 移除即失效（unmatched_exclusions
  提供漂移告警），改名后须同步更新 Policy；
- current_states: {device_id(str): state}，state 含 name / device_type /
  online / power(bool) 等归一化字段；窗户类传感器可携带 window_open。
  device_id / device_type 缺失或为空视为调用方编程错误，plan 直接抛
  ValueError（fail-fast，不静默降级——同 anomaly_rules 的 naive 时间口径）。
  power 归一化口径：light 用 brightness > 0 推导，ac 按电源事件；
- scope: {"authorized_device_ids": [...]}，供 judge_risk 做目标范围校验。

安全口径：风险分级直接调用 risk_rules.judge_risk（规则表唯一依据，
fail-safe：未知动作按 high + 强制审批兜底——lock_door 刻意不入表，
正是让离家落锁走该兜底）；policy 规则里的 "risk" 字段只是展示标注
（输出里的 policy_risk），不参与门控判定。
"""

from typing import Any

from app.agent.risk_rules import judge_risk

# 冲突检查标识，与种子 conflict_checks 一一对应（detect_conflicts 消费）。
CONFLICT_WINDOW_AC = "window_open_blocks_ac_off"

# 固定提示口径：冲突 / 暂停文案是确定性输出，子图与评估都按此断言。
REASON_WINDOW_AC = "窗户开着，暂停关空调，标记待确认"
REASON_OFFLINE = "设备离线，暂停执行，标记待确认"
REASON_MEDIA_MUTED = "睡眠模式不自动播放媒体设备，转为待确认"
REASON_NIGHT_LIGHT_BLOCKED = "睡眠模式未允许夜灯，转为待确认"
REASON_NIGHT_LIGHT_TOO_BRIGHT = "夜灯亮度超过睡眠模式允许上限"
REASON_READBACK_STALE = "回读显示状态未生效，转入未完成清单"
REASON_READBACK_MISSING = "回读无该设备状态，转入未完成清单"

# 断言 #19 覆盖的设备域与夜灯识别口径：媒体设备不自动播放；
# 夜灯按设备名识别（种子与演示设备命名约定），允许低亮度开启。
MEDIA_DEVICE_TYPE = "media"
NIGHT_LIGHT_MARKER = "夜灯"
NIGHT_LIGHT_MAX_BRIGHTNESS = 30

# 主变化字段优先级：决定动作标识（power 优先，其次温度 / 亮度）。
_PRIMARY_FIELD_ORDER: tuple[str, ...] = ("power", "temperature", "brightness")


def _device_id_for_risk(device_id: Any) -> int | None:
    """把字符串设备 id 解析为 judge_risk 需要的 int；非数字视为家庭级动作。"""
    if device_id is None:
        return None
    try:
        return int(device_id)
    except ValueError:
        # 非数字字符串。拆成两个子句：不依赖 3.14 专属语法（PEP 758），
        # 与 diagnose.py 同一约定。
        return None
    except TypeError:
        # device_id 非字符串 / 数字类型（防御性兜底），同义返回 None。
        return None


def _target_reached(field: str, target_value: Any, state: dict) -> bool:
    """目标字段是否已等于当前状态——power 归一语义的唯一事实源。

    power 按 on/off 语义比较（target "on" ↔ state power True），其余
    字段等值比较；当前状态缺字段视为未达到。plan 的幂等跳过与回读的
    未生效判定共用本函数，改归一语义只需改这一处。
    """
    if field == "power":
        return state.get("power") == (target_value == "on")
    return state.get(field) == target_value


def _target_differs(field: str, target_value: Any, state: dict) -> bool:
    """目标字段与当前状态是否不同（plan 幂等跳过的依据）。

    定义为 _target_reached 的取反（单一事实源）；当前状态缺字段视同
    不同——宁可多发一条动作，由 MCP 层幂等消化。
    """
    return not _target_reached(field, target_value, state)


def _action_id(device_type: str, field: str, target_value: Any) -> str:
    """按设备域与主变化字段映射规则表动作标识。

    未入表的组合返回 set_{域}_{字段} 形式，交给 judge_risk 的未知动作
    兜底判 high（fail-safe），不在本模块私设等级。
    """
    if field == "power" and device_type in ("light", "ac"):
        domain = "light" if device_type == "light" else "ac"
        on_off = "on" if target_value == "on" else "off"
        return f"turn_{domain}_{on_off}"
    if device_type == "ac" and field == "temperature":
        return "set_ac_temperature"
    return f"set_{device_type}_{field}"


def _primary_sort_key(pair: tuple[str, Any]) -> int:
    """主变化字段排序键：不在优先级表中的字段排在已知字段之后。"""
    try:
        return _PRIMARY_FIELD_ORDER.index(pair[0])
    except ValueError:
        return len(_PRIMARY_FIELD_ORDER)


def _validated_state(state: dict) -> tuple[str, str]:
    """提取并校验 plan 依赖的契约字段：device_id / device_type。

    两字段缺失或为空都视为调用方编程错误，直接抛 ValueError：
    缺 device_id 会产出 "None" 假动作（judge_risk 跳过范围校验、冲突
    与回读查不到、MCP 执行必失败），缺 device_type 会让设备被静默
    当作不受治理类型跳过——契约违约不允许降级，宁可响亮失败。
    """
    device_id = state.get("device_id")
    device_type = state.get("device_type")
    if device_id is None or str(device_id).strip() == "":
        raise ValueError(
            f"设备状态缺少 device_id（name={state.get('name')!r}），"
            "违反 plan 输入契约，fail-fast 不静默降级"
        )
    if not device_type or str(device_type).strip() == "":
        raise ValueError(
            f"设备状态缺少 device_type（device_id={device_id!r}），"
            "违反 plan 输入契约，fail-fast 不静默跳过"
        )
    return str(device_id), str(device_type)


def _graded_action(
    device_id: str | None,
    device_name: str,
    device_type: str,
    action_id: str,
    target: dict,
    policy_risk: Any,
    scope: dict,
) -> tuple[dict | None, dict | None]:
    """构造动作并做确定性风险分级；范围越界时返回 blocked 条目。

    返回 (planned_action, blocked_action)，恰好一个为 None：
    allowed=False（目标不在授权范围）的动作直接进 blocked，绝不进入
    planned——范围越界是阻断性质，与冲突的「待确认」性质不同。
    """
    decision = judge_risk(action_id, _device_id_for_risk(device_id), scope)
    action = {
        "device_id": device_id,
        "device_name": device_name,
        "device_type": device_type,
        "action": action_id,
        "target": target,
        "policy_risk": policy_risk,
        "risk_level": decision.risk_level,
        "require_approval": decision.require_approval,
    }
    if decision.allowed:
        return action, None
    return None, {**action, "reason": decision.reason}


def plan_mode_transition(
    policy: dict, current_states: dict[str, dict], scope: dict
) -> tuple[list[dict], list[dict]]:
    """按策略生成迁移动作并逐条做确定性风险分级。

    返回 (planned, blocked)。幂等口径：已处于目标状态的设备跳过；
    exclusions（按设备名匹配，改名即失效，漂移由 unmatched_exclusions
    告警）与策略未覆盖的设备类型不产生动作；lock 是家庭级动作，只要
    策略声明就生成（无设备状态可比，目标固定 locked=True）。
    """
    planned: list[dict] = []
    blocked: list[dict] = []
    rules = policy.get("by_device_type", {})
    exclusions = set(policy.get("exclusions", []))

    for state in current_states.values():
        device_id, device_type = _validated_state(state)
        if state.get("name") in exclusions:
            continue
        rule = rules.get(device_type)
        if rule is None:
            continue
        target = {k: v for k, v in rule.items() if k != "risk"}
        changed = [
            (field, value)
            for field, value in target.items()
            if _target_differs(field, value, state)
        ]
        if not changed:
            continue
        # 主变化字段决定动作标识；动作携带完整目标，供 MCP 一次下发。
        primary = min(changed, key=_primary_sort_key)
        action_id = _action_id(device_type, primary[0], primary[1])
        device_name = str(state.get("name") or "")
        action, out_of_scope = _graded_action(
            device_id,
            device_name,
            device_type,
            action_id,
            target,
            rule.get("risk"),
            scope,
        )
        if action is not None:
            planned.append(action)
        else:
            assert out_of_scope is not None
            blocked.append(out_of_scope)

    lock_rule = policy.get("lock")
    if lock_rule is not None:
        action, out_of_scope = _graded_action(
            None,
            "家庭门锁",
            "lock",
            "lock_door",
            {"locked": True},
            lock_rule.get("risk"),
            scope,
        )
        if action is not None:
            planned.append(action)
        else:
            assert out_of_scope is not None
            blocked.append(out_of_scope)
    return planned, blocked


def unmatched_exclusions(policy: dict, current_states: dict[str, dict]) -> list[str]:
    """返回 exclusions 中当前设备名集合已匹配不到的条目（配置漂移告警）。

    exclusions 按设备名匹配：设备改名 / 移除后排除关系会静默失效，
    被排除设备重新落入迁移动作的覆盖范围——本函数把策略声明的排除项
    与当前设备名做差集，把失效从「静默」变成「可断言的告警信号」，
    供第 4 步子图在 dry-run 摘要里向用户展示，也给评估集提供断言点。
    长期方案是排除项改用稳定设备标识（device_id），但当前 Policy 由
    人工按名声明、项目尚无独立于数据库主键的稳定标识层，暂缓（YAGNI）。
    返回顺序与策略声明顺序一致（确定性输出）。
    """
    known_names = {state.get("name") for state in current_states.values()}
    return [name for name in policy.get("exclusions", []) if name not in known_names]


def detect_conflicts(
    planned: list[dict], current_states: dict[str, dict], policy: dict
) -> tuple[list[dict], list[dict]]:
    """冲突检测：离线设备与策略声明的 conflict_checks（当前实现 #18）。

    返回 (planned, blocked)：命中冲突的动作从 planned 移入 blocked，
    blocked 条目 = 原动作字段 + reason，交由子图转待确认；冲突检查
    按标识 opt-in（策略未声明不生效），离线检查恒生效。
    """
    checks = set(policy.get("conflict_checks", []))
    window_open = any(
        bool(state.get("window_open")) for state in current_states.values()
    )
    planned_kept: list[dict] = []
    blocked: list[dict] = []
    for action in planned:
        state = current_states.get(str(action.get("device_id")))
        if state is not None and state.get("online") is False:
            blocked.append({**action, "reason": REASON_OFFLINE})
            continue
        if (
            CONFLICT_WINDOW_AC in checks
            and window_open
            and action.get("device_type") == "ac"
            and action.get("target", {}).get("power") == "off"
        ):
            blocked.append({**action, "reason": REASON_WINDOW_AC})
            continue
        planned_kept.append(action)
    return planned_kept, blocked


def apply_sleep_mute_policy(
    planned: list[dict], policy: dict
) -> tuple[list[dict], list[dict]]:
    """睡眠静音策略（#19：媒体设备不自动播放，夜灯允许低亮度开启）。

    返回 (planned, muted)：muted = 从 planned 移出的动作 + reason。
    策略未声明 mute_media / allow_night_light 时原样透传（night 等
    模式不受影响）；allow_night_light=False 时夜灯动作整体移出，
    为 True 时仅放行亮度不超上限的夜灯动作。
    """
    mute_media = bool(policy.get("mute_media"))
    night_light_declared = "allow_night_light" in policy
    if not mute_media and not night_light_declared:
        return planned, []

    muted: list[dict] = []
    kept: list[dict] = []
    for action in planned:
        name = str(action.get("device_name") or "")
        target = action.get("target", {})
        if (
            mute_media
            and action.get("device_type") == MEDIA_DEVICE_TYPE
            and target.get("power") == "on"
        ):
            muted.append({**action, "reason": REASON_MEDIA_MUTED})
            continue
        if NIGHT_LIGHT_MARKER in name:
            if not night_light_declared or not policy.get("allow_night_light"):
                muted.append({**action, "reason": REASON_NIGHT_LIGHT_BLOCKED})
                continue
            brightness = target.get("brightness")
            if brightness is not None and brightness > NIGHT_LIGHT_MAX_BRIGHTNESS:
                muted.append({**action, "reason": REASON_NIGHT_LIGHT_TOO_BRIGHT})
                continue
        kept.append(action)
    return kept, muted


def summarize_readback(planned: list[dict], readback_states: dict[str, dict]) -> dict:
    """执行后状态回读汇总（#20：区分「已下发」与「已生效」）。

    planned 的每条动作逐字段与回读状态比对；输出结构
    {total, applied, unresolved, rollback_plan}。rollback_plan 本周只
    输出骨架（applied 设备清单 + 说明）——真实回滚需按上一模式
    Policy 反推目标状态，不在本周范围。
    """
    applied: list[dict] = []
    unresolved: list[dict] = []
    for action in planned:
        state = readback_states.get(str(action.get("device_id")))
        if state is None:
            unresolved.append({**action, "reason": REASON_READBACK_MISSING})
            continue
        stale_fields = [
            field
            for field, value in action.get("target", {}).items()
            if not _target_reached(field, value, state)
        ]
        if stale_fields:
            unresolved.append(
                {
                    **action,
                    "reason": REASON_READBACK_STALE,
                    "stale_fields": stale_fields,
                }
            )
        else:
            applied.append(action)
    return {
        "total": len(planned),
        "applied": applied,
        "unresolved": unresolved,
        "rollback_plan": {
            "note": "回滚骨架：按上一模式 Policy 反推目标状态，第 6 周不实现自动回滚",
            "applied_device_ids": [action.get("device_id") for action in applied],
        },
    }


def requires_approval(planned: list[dict]) -> bool:
    """迁移整体是否需要审批：任一动作 require_approval 即整批挂起。

    简化口径：一次迁移一批审批，部分高风险不会放行其余动作并行执行，
    由子图 gate 节点统一挂起 / 恢复（与诊断审批同一模型）。
    """
    return any(action.get("require_approval") for action in planned)
