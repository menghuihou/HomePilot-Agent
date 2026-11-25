"""家庭模式迁移确定性核心测试，对应第 6 周第 3 步检查点。

纯函数测试，不依赖数据库与 LLM；覆盖总计划断言 #18 / #19 / #20 口径。
所有断言针对结构化输出（动作字典字段），不针对文案。
"""

import pytest

from app.agent.home_mode import (
    REASON_MEDIA_MUTED,
    REASON_NIGHT_LIGHT_BLOCKED,
    REASON_NIGHT_LIGHT_TOO_BRIGHT,
    REASON_OFFLINE,
    REASON_READBACK_MISSING,
    REASON_READBACK_STALE,
    REASON_WINDOW_AC,
    apply_sleep_mute_policy,
    detect_conflicts,
    plan_mode_transition,
    requires_approval,
    summarize_readback,
    unmatched_exclusions,
)

# 授权清单覆盖 away / sleep 两套用例的全部设备 id：越界行为由
# test_plan_blocks_out_of_scope_device 单独用收窄后的 scope 验证。
SCOPE = {"authorized_device_ids": [1, 2, 3, 6, 7]}

# 与 db/seeds/load_home_policies.py 的种子策略保持同构（测试自足，
# 不 import db 模块，避免测试进程触碰数据库引擎）。
AWAY_POLICY = {
    "by_device_type": {
        "light": {"power": "off", "risk": "low"},
        "ac": {"power": "off", "risk": "medium"},
    },
    "lock": {"target": "locked", "risk": "high"},
    "exclusions": ["家庭网关"],
    "conflict_checks": ["window_open_blocks_ac_off"],
}
SLEEP_POLICY = {
    "by_device_type": {
        "light": {"power": "off", "risk": "low"},
        "ac": {"power": "on", "temperature": 27, "risk": "medium"},
    },
    "mute_media": True,
    "allow_night_light": True,
    "exclusions": ["家庭网关"],
    "conflict_checks": [],
}

ACTION_FIELDS = {
    "device_id",
    "device_name",
    "device_type",
    "action",
    "target",
    "policy_risk",
    "risk_level",
    "require_approval",
}


def _state(device_id, name, device_type, online=True, **extra) -> dict:
    return {
        "device_id": str(device_id),
        "name": name,
        "device_type": device_type,
        "online": online,
        **extra,
    }


def _away_states(**overrides) -> dict[str, dict]:
    states = {
        "1": _state(1, "客厅灯", "light", power=True),
        "2": _state(2, "客厅空调", "ac", power=True, temperature=26),
    }
    states.update(overrides)
    return states


def _action(device_id, device_type="light", target=None, name="测试设备") -> dict:
    return {
        "device_id": str(device_id),
        "device_name": name,
        "device_type": device_type,
        "action": "turn_light_off",
        "target": target if target is not None else {"power": "off"},
        "policy_risk": "low",
        "risk_level": "low",
        "require_approval": False,
    }


# ---------- plan_mode_transition ----------


def test_plan_away_generates_structured_actions() -> None:
    """离家模式：每设备一条动作 + 家庭级落锁；风险分级来自规则表。"""
    planned, blocked = plan_mode_transition(AWAY_POLICY, _away_states(), SCOPE)

    assert blocked == []
    by_action = {action["action"]: action for action in planned}
    assert set(by_action) == {"turn_light_off", "turn_ac_off", "lock_door"}
    # 输出是结构化 JSON，不含文案字段。
    for action in planned:
        assert set(action) == ACTION_FIELDS
    # 风险分级以规则表为准（policy 的 risk 只是展示标注 policy_risk）。
    assert by_action["turn_light_off"]["risk_level"] == "low"
    assert by_action["turn_ac_off"]["risk_level"] == "medium"
    assert by_action["turn_ac_off"]["policy_risk"] == "medium"
    # lock_door 刻意不入表：走未知动作 fail-safe 兜底 high + 强制审批。
    lock = by_action["lock_door"]
    assert lock["risk_level"] == "high"
    assert lock["require_approval"] is True
    assert lock["target"] == {"locked": True}
    assert lock["device_id"] is None


def test_plan_is_idempotent_for_devices_at_target() -> None:
    """已处于目标状态的设备跳过（幂等），只剩家庭级动作。"""
    states = _away_states(
        **{
            "1": _state(1, "客厅灯", "light", power=False),
            "2": _state(2, "客厅空调", "ac", power=False, temperature=26),
        }
    )
    planned, blocked = plan_mode_transition(AWAY_POLICY, states, SCOPE)

    assert blocked == []
    assert [action["action"] for action in planned] == ["lock_door"]


def test_plan_skips_exclusions_and_ungoverned_types() -> None:
    """网关在 exclusions 不产生动作；策略未覆盖的设备类型同样跳过。"""
    states = _away_states(
        **{
            "3": _state(3, "家庭网关", "gateway", power=True),
            "4": _state(4, "客厅温湿度传感器", "sensor", power=True),
        }
    )
    planned, blocked = plan_mode_transition(AWAY_POLICY, states, SCOPE)

    assert blocked == []
    target_ids = {action["device_id"] for action in planned if action["device_id"]}
    assert target_ids == {"1", "2"}


def test_plan_blocks_out_of_scope_device() -> None:
    """目标设备越界：动作直接进 blocked（阻断性质），不进入 planned。"""
    planned, blocked = plan_mode_transition(
        AWAY_POLICY, _away_states(), {"authorized_device_ids": [1]}
    )

    planned_ids = {action["device_id"] for action in planned if action["device_id"]}
    assert planned_ids == {"1"}
    assert len(blocked) == 1
    assert blocked[0]["device_id"] == "2"
    assert "不在授权范围内" in blocked[0]["reason"]
    assert "reason" in blocked[0]


def test_plan_temperature_only_change_maps_set_ac_temperature() -> None:
    """电源不变仅温差：动作标识映射为 set_ac_temperature（low）。"""
    states = {"2": _state(2, "客厅空调", "ac", power=False, temperature=26)}
    sleep_like_ac_off = {
        "by_device_type": {"ac": {"power": "off", "temperature": 27, "risk": "medium"}},
        "exclusions": [],
        "conflict_checks": [],
    }
    planned, _ = plan_mode_transition(sleep_like_ac_off, states, SCOPE)

    assert [action["action"] for action in planned] == ["set_ac_temperature"]
    assert planned[0]["risk_level"] == "low"
    assert planned[0]["target"] == {"power": "off", "temperature": 27}


def test_plan_unknown_action_falls_back_to_high() -> None:
    """未入表的组合：动作标识 set_* → judge_risk 兜底 high + 强制审批。"""
    policy = {
        "by_device_type": {"media": {"power": "on", "risk": "low"}},
        "exclusions": [],
        "conflict_checks": [],
    }
    # 设备 id 必须在授权范围内，单独验证未知动作的等级兜底路径。
    states = {"1": _state(1, "客厅音箱", "media", power=False)}
    planned, _ = plan_mode_transition(policy, states, SCOPE)

    assert planned[0]["action"] == "set_media_power"
    assert planned[0]["risk_level"] == "high"
    assert planned[0]["require_approval"] is True


def test_plan_raises_on_missing_device_id() -> None:
    """缺 device_id 是调用方编程错误：fail-fast 抛错，不产出 "None" 假动作。"""
    states = {"1": {"name": "客厅灯", "device_type": "light", "power": True}}

    with pytest.raises(ValueError, match="device_id"):
        plan_mode_transition(AWAY_POLICY, states, SCOPE)


def test_plan_raises_on_blank_device_id() -> None:
    """空白 device_id 同样违约：不产出空 id 动作。"""
    states = {"1": _state(1, "客厅灯", "light", power=True)}
    states["1"]["device_id"] = "  "

    with pytest.raises(ValueError, match="device_id"):
        plan_mode_transition(AWAY_POLICY, states, SCOPE)


def test_plan_raises_on_missing_device_type() -> None:
    """缺 device_type：fail-fast，而不是被静默当作不受治理类型跳过。"""
    states = {"1": {"device_id": "1", "name": "客厅灯", "power": True}}

    with pytest.raises(ValueError, match="device_type"):
        plan_mode_transition(AWAY_POLICY, states, SCOPE)


def test_plan_accepts_falsy_but_valid_int_zero_id() -> None:
    """边界：id=0 是合法值（falsy），契约校验不得误伤。"""
    states = {"0": _state(0, "玄关灯", "light", power=True)}
    scope = {"authorized_device_ids": [0]}

    planned, blocked = plan_mode_transition(AWAY_POLICY, states, scope)

    assert blocked == []
    assert planned[0]["device_id"] == "0"
    assert planned[0]["action"] == "turn_light_off"


# ---------- detect_conflicts ----------


def test_window_open_blocks_ac_off_only() -> None:
    """断言 #18：离家模式下窗户开着 → 暂停关空调并标记待确认。"""
    states = _away_states(**{"5": _state(5, "客厅窗磁", "sensor", window_open=True)})
    planned, _ = plan_mode_transition(AWAY_POLICY, states, SCOPE)
    planned_kept, blocked = detect_conflicts(planned, states, AWAY_POLICY)

    kept_actions = [action["action"] for action in planned_kept]
    assert "turn_ac_off" not in kept_actions
    assert "turn_light_off" in kept_actions
    assert len(blocked) == 1
    assert blocked[0]["action"] == "turn_ac_off"
    assert blocked[0]["reason"] == REASON_WINDOW_AC


def test_window_closed_keeps_ac_off() -> None:
    """窗户关闭（无 window_open 状态）→ 不产生冲突阻断。"""
    planned, _ = plan_mode_transition(AWAY_POLICY, _away_states(), SCOPE)
    planned_kept, blocked = detect_conflicts(planned, _away_states(), AWAY_POLICY)

    assert blocked == []
    assert len(planned_kept) == len(planned)


def test_offline_device_moves_to_blocked() -> None:
    """离线设备的动作整体进 blocked，恒生效（不依赖策略声明）。"""
    states = _away_states(
        **{"1": _state(1, "客厅灯", "light", online=False, power=True)}
    )
    planned, _ = plan_mode_transition(AWAY_POLICY, states, SCOPE)
    planned_kept, blocked = detect_conflicts(planned, states, AWAY_POLICY)

    assert [action["reason"] for action in blocked] == [REASON_OFFLINE]
    assert blocked[0]["device_id"] == "1"
    assert all(action["device_id"] != "1" for action in planned_kept)


def test_conflict_check_is_opt_in() -> None:
    """策略未声明 conflict_checks 时，窗户状态不阻断（规则 opt-in）。"""
    policy = {**AWAY_POLICY, "conflict_checks": []}
    states = _away_states(**{"5": _state(5, "客厅窗磁", "sensor", window_open=True)})
    planned, _ = plan_mode_transition(policy, states, SCOPE)
    planned_kept, blocked = detect_conflicts(planned, states, policy)

    assert blocked == []
    assert len(planned_kept) == len(planned)


# ---------- apply_sleep_mute_policy ----------


def _sleep_states() -> dict[str, dict]:
    return {
        "1": _state(1, "卧室灯", "light", power=True),
        "6": _state(6, "客厅音箱", "media", power=False),
        "7": _state(7, "走廊夜灯", "light", power=False),
    }


def test_sleep_mutes_media_autoplay_only() -> None:
    """断言 #19：睡眠模式媒体设备不自动播放 → 移出 planned 转待确认。"""
    policy = {
        **SLEEP_POLICY,
        "by_device_type": {
            **SLEEP_POLICY["by_device_type"],
            "media": {"power": "on", "risk": "low"},
        },
    }
    planned, _ = plan_mode_transition(policy, _sleep_states(), SCOPE)
    planned_kept, muted = apply_sleep_mute_policy(planned, policy)

    assert [action["reason"] for action in muted] == [REASON_MEDIA_MUTED]
    assert muted[0]["device_id"] == "6"
    assert muted[0]["action"] == "set_media_power"
    # 夜灯本就处于关闭态（无动作），灯光关闭动作保留。
    assert len(planned_kept) == 1
    assert planned_kept[0]["device_id"] == "1"


def test_sleep_allows_night_light_low_brightness() -> None:
    """断言 #19：夜灯允许低亮度开启 → 低亮度动作保留在 planned。"""
    night_light_on = _action(
        7,
        target={"power": "on", "brightness": 20},
        name="走廊夜灯",
    )
    kept, muted = apply_sleep_mute_policy([night_light_on], SLEEP_POLICY)

    assert muted == []
    assert kept == [night_light_on]


def test_sleep_blocks_night_light_by_flag_or_brightness() -> None:
    """夜灯两个边界：策略未允许 → 整体移出；允许但亮度超上限 → 移出。"""
    planned = [
        _action(7, target={"power": "on"}, name="走廊夜灯"),
        _action(8, target={"power": "on", "brightness": 100}, name="卧室夜灯"),
    ]

    disallowed = {**SLEEP_POLICY, "allow_night_light": False}
    kept, muted = apply_sleep_mute_policy(planned, disallowed)
    assert kept == []
    assert [action["reason"] for action in muted] == [
        REASON_NIGHT_LIGHT_BLOCKED,
        REASON_NIGHT_LIGHT_BLOCKED,
    ]

    allowed = {**SLEEP_POLICY, "allow_night_light": True}
    kept, muted = apply_sleep_mute_policy(planned, allowed)
    assert [action["device_id"] for action in kept] == ["7"]
    assert [action["reason"] for action in muted] == [REASON_NIGHT_LIGHT_TOO_BRIGHT]


def test_non_sleep_policy_passthrough() -> None:
    """策略未声明静音字段（如 night）→ 原样透传，零副作用。"""
    policy = {"by_device_type": {"light": {"power": "off", "risk": "low"}}}
    planned = [_action(1), _action(2)]
    kept, muted = apply_sleep_mute_policy(planned, policy)

    assert muted == []
    assert kept == planned


# ---------- summarize_readback ----------


def test_readback_all_applied_with_rollback_skeleton() -> None:
    """全部生效：applied 与回滚骨架（applied 设备清单）一致。"""
    planned = [_action(1), _action(2, target={"power": "off"})]
    readback = {
        "1": {"power": False},
        "2": {"power": False},
    }
    summary = summarize_readback(planned, readback)

    assert summary["total"] == 2
    assert len(summary["applied"]) == 2
    assert summary["unresolved"] == []
    assert summary["rollback_plan"]["applied_device_ids"] == ["1", "2"]
    assert "不实现自动回滚" in summary["rollback_plan"]["note"]


def test_readback_reports_single_unresolved() -> None:
    """断言 #20：计划 8 个动作、7 生效 1 未生效 → 输出 1 条未完成项。"""
    planned = [_action(i) for i in range(1, 9)]
    readback = {str(i): {"power": False} for i in range(1, 9)}
    readback["8"] = {"power": True}  # 下发成功但状态未生效

    summary = summarize_readback(planned, readback)

    assert summary["total"] == 8
    assert len(summary["applied"]) == 7
    assert len(summary["unresolved"]) == 1
    assert summary["unresolved"][0]["device_id"] == "8"
    assert summary["unresolved"][0]["reason"] == REASON_READBACK_STALE
    assert summary["unresolved"][0]["stale_fields"] == ["power"]


def test_readback_missing_state_is_unresolved() -> None:
    """回读清单缺设备：该动作进未完成（不静默当作成功）。"""
    planned = [_action(1)]
    summary = summarize_readback(planned, {})

    assert summary["unresolved"][0]["reason"] == REASON_READBACK_MISSING
    assert summary["applied"] == []


# ---------- requires_approval ----------


def test_requires_approval_true_when_any_action_high() -> None:
    """任一动作 require_approval → 整批挂起（含家庭级落锁）。"""
    planned, _ = plan_mode_transition(AWAY_POLICY, _away_states(), SCOPE)
    assert requires_approval(planned) is True


def test_requires_approval_false_for_all_low() -> None:
    """全低风险（无落锁策略）→ 不挂起，gate 节点直接放行。"""
    policy = {
        "by_device_type": {"light": {"power": "off", "risk": "low"}},
        "exclusions": [],
        "conflict_checks": [],
    }
    planned, _ = plan_mode_transition(policy, _away_states(), SCOPE)
    assert planned
    assert requires_approval(planned) is False


# ---------- unmatched_exclusions ----------


def test_unmatched_exclusions_detects_rename_or_removal() -> None:
    """网关改名 / 移除后，策略声明的排除项匹配不到 → 返回漂移名单。"""
    states = _away_states(**{"3": _state(3, "全屋智能网关", "gateway")})

    drift = unmatched_exclusions(AWAY_POLICY, states)

    assert drift == ["家庭网关"]


def test_unmatched_exclusions_empty_when_all_matched() -> None:
    """排除项都能匹配到当前设备名 → 无漂移。"""
    states = _away_states(**{"3": _state(3, "家庭网关", "gateway")})

    assert unmatched_exclusions(AWAY_POLICY, states) == []


def test_unmatched_exclusions_passthrough_without_exclusions() -> None:
    """策略未声明 exclusions → 恒返回空列表。"""
    policy = {"by_device_type": {"light": {"power": "off", "risk": "low"}}}

    assert unmatched_exclusions(policy, _away_states()) == []
