"""确定性风险规则判定测试，对应第 3 周第 3 步检查点。

纯函数测试，不依赖数据库与 LLM。
"""

import pytest

from app.agent.risk_rules import RISK_RULES, RiskRule, judge_risk

IN_SCOPE = {"home_id": 1, "room": "客厅", "authorized_device_ids": [1, 2, 3]}
OUT_OF_SCOPE_DEVICE = 99
NO_LIST = {"home_id": 1, "room": "客厅"}
EMPTY_LIST = {"home_id": 1, "room": "客厅", "authorized_device_ids": []}


def test_rules_table_shape() -> None:
    """规则表自洽：action 唯一、等级合法、high 必须强制审批。"""
    actions = [rule.action for rule in RISK_RULES]
    assert len(actions) == len(set(actions))
    for rule in RISK_RULES:
        assert isinstance(rule, RiskRule)
        assert rule.level in ("low", "medium", "high")
        if rule.level == "high":
            assert rule.require_approval is True


@pytest.mark.parametrize(
    ("action", "expected_level", "expected_approval"),
    [(rule.action, rule.level, rule.require_approval) for rule in RISK_RULES],
    ids=[rule.action for rule in RISK_RULES],
)
def test_known_action_matches_table(
    action: str, expected_level: str, expected_approval: bool
) -> None:
    """规则表每一条：判定结果与表定义严格一致。"""
    decision = judge_risk(action, None, IN_SCOPE)
    assert decision.risk_level == expected_level
    assert decision.require_approval == expected_approval
    assert decision.allowed is True


def test_restart_gateway_is_high_and_requires_approval() -> None:
    """审批演示主路径：网关重启必须是 high + 强制审批。"""
    decision = judge_risk("restart_gateway", 1, IN_SCOPE)
    assert decision.risk_level == "high"
    assert decision.require_approval is True
    assert decision.allowed is True


@pytest.mark.parametrize(
    "action",
    ["launch_missile", "Power_Off_All", " power_off_all ", ""],
    ids=["totally_unknown", "case_variant", "whitespace_variant", "empty_string"],
)
def test_unknown_action_falls_back_to_high(action: str) -> None:
    """fail-safe：未知动作（含大小写/空白变体）一律 high + 强制审批。"""
    decision = judge_risk(action, None, IN_SCOPE)
    assert decision.risk_level == "high"
    assert decision.require_approval is True
    assert decision.allowed is True


def test_out_of_scope_device_blocked_even_for_low_risk() -> None:
    """越界设备：低风险动作同样阻断，范围校验独立于等级判定。"""
    decision = judge_risk("turn_light_on", OUT_OF_SCOPE_DEVICE, IN_SCOPE)
    assert decision.allowed is False
    assert decision.risk_level == "low"
    assert decision.require_approval is False


def test_unknown_action_and_out_of_scope_blocked_wins() -> None:
    """未知动作 + 越界设备：阻断优先，等级仍走 high 兜底。"""
    decision = judge_risk("mystery_action", OUT_OF_SCOPE_DEVICE, IN_SCOPE)
    assert decision.allowed is False
    assert decision.risk_level == "high"
    assert decision.require_approval is True


@pytest.mark.parametrize(
    "scope", [NO_LIST, EMPTY_LIST], ids=["no_list_key", "empty_list"]
)
def test_missing_or_empty_authorization_blocks_targeted_action(
    scope: dict,
) -> None:
    """授权列表缺失或为空：指定目标设备的动作按 fail-safe 阻断。"""
    decision = judge_risk("turn_light_on", 1, scope)
    assert decision.allowed is False


def test_batch_action_without_target_skips_device_scope_check() -> None:
    """批量动作不指定目标：不适用设备级校验，靠 high + 审批保证安全。"""
    decision = judge_risk("power_off_all", None, NO_LIST)
    assert decision.allowed is True
    assert decision.risk_level == "high"
    assert decision.require_approval is True


def test_decision_reason_carries_context() -> None:
    """reason 携带判定依据，可写入 trace 与审批说明。"""
    decision = judge_risk("turn_light_off", 2, IN_SCOPE)
    assert "2" in decision.reason
    assert "turn_light_off" in decision.reason
