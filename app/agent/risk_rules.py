"""确定性风险规则表与判定函数（第 3 周第 3 步）。

安全口径：风险等级判定只查本文件规则表，不依赖 LLM 自觉，
也不读 MCP 远端 metadata（远端标注仅做展示参考）。
fail-safe：查不到的动作按 high + 强制审批兜底，宁可误拦不可漏放；
动作名不做大小写或空白归一化，任何变体都落入未知动作兜底。
"""

from dataclasses import dataclass
from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class RiskRule:
    """单条风险规则：动作 → 风险等级 + 是否强制审批。"""

    action: str
    level: RiskLevel
    require_approval: bool


# 规则表版本化在 repo（本文件 + git 历史），是安全关键路径的唯一依据。
RISK_RULES: tuple[RiskRule, ...] = (
    # 网关影响全屋设备连通性，重启列为 high（审批演示主路径）。
    RiskRule(action="restart_gateway", level="high", require_approval=True),
    RiskRule(action="power_off_all", level="high", require_approval=True),
    RiskRule(action="factory_reset", level="high", require_approval=True),
    RiskRule(action="restart_device", level="medium", require_approval=False),
    RiskRule(action="turn_light_on", level="low", require_approval=False),
    RiskRule(action="turn_light_off", level="low", require_approval=False),
    # 灯光域知识库 fix_action（第 8 步对齐）：查询/清洁/人工联系类均为低风险。
    RiskRule(action="check_power", level="low", require_approval=False),
    RiskRule(action="replace_bulb", level="low", require_approval=False),
    RiskRule(action="tighten_socket", level="low", require_approval=False),
    RiskRule(action="toggle_switch", level="low", require_approval=False),
    RiskRule(action="clean_shade", level="low", require_approval=False),
    RiskRule(action="contact_electrician", level="low", require_approval=False),
)

# 由规则表构建的查表索引，O(1) 命中；运行期不再变更。
_RULES_BY_ACTION: dict[str, RiskRule] = {rule.action: rule for rule in RISK_RULES}


@dataclass(frozen=True)
class RiskDecision:
    """judge_risk 的判定结果，供审批门控与审批记录直接使用。

    allowed 与 require_approval 是两回事：allowed=False 表示本回合
    禁止执行（范围越界，无可挽回性质）；require_approval=True 表示
    可以执行，但必须先经用户审批。
    """

    action: str
    risk_level: RiskLevel
    require_approval: bool
    allowed: bool
    # 判定依据说明，写入 trace 与审批事件，便于排查。
    reason: str


def judge_risk(
    action: str, target_device_id: int | None, scope: dict[str, Any]
) -> RiskDecision:
    """确定性风险判定：查规则表 + 目标范围校验，零 LLM 参与。

    - 查表：未知动作按 high + 强制审批兜底（fail-safe）。
    - 范围：指定目标设备的动作，目标必须在 scope["authorized_device_ids"]
      内，否则直接阻断；授权列表缺失或为空同样阻断（fail-safe）。
    - 批量动作（target_device_id=None）不适用设备级范围校验，
      其安全性由规则表等级与审批门控保证。
    """
    rule = _RULES_BY_ACTION.get(action)
    if rule is None:
        risk_level: RiskLevel = "high"
        require_approval = True
        rule_reason = "未知动作，按 high + 强制审批兜底"
    else:
        risk_level = rule.level
        require_approval = rule.require_approval
        rule_reason = f"命中规则表：{action} → {rule.level}"

    authorized_ids = set(scope.get("authorized_device_ids") or [])
    scope_ok = True
    scope_reason = "无指定目标设备，按家庭范围执行"
    if target_device_id is not None:
        if target_device_id in authorized_ids:
            scope_reason = f"目标设备 {target_device_id} 在授权范围内"
        else:
            scope_ok = False
            scope_reason = f"目标设备 {target_device_id} 不在授权范围内，阻断"

    return RiskDecision(
        action=action,
        risk_level=risk_level,
        require_approval=require_approval,
        allowed=scope_ok,
        reason=f"{scope_reason}；{rule_reason}",
    )
