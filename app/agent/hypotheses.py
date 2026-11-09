"""候选故障假设：确定性计算、证据收缩与排查动作选择（第 3 周第 6 步）。

全部纯函数：不碰 LLM、不访问数据库，数据由调用方从知识库预加载。
口径与总计划 5.2 节一致，其中收敛判定含一处显式细化（见 should_conclude）：

- 并集：命中任一症状的原因即入选，保证候选集不为空；
- 排序：matched_evidence_count 降序 → prevalence 降序 →
  investigation_weight 降序 → cause_key（并列稳定）；
- 收敛：并列最高命中数的「领先组」≤ 2 且最高命中数 ≥ 2 即出结论。
  总计划原文「matched_evidence_count >= 2 的候选数收缩到 ≤ 2」细化为
  领先组口径：证据只会增加命中数，若按绝对阈值统计，高置信集合会随
  轮次单调膨胀、永不收缩；领先组口径与「收缩到 ≤ 2」意图一致且单调；
- 硬上限：排查最多 3 轮，之后强制出结论（状态机边条件，不依赖 LLM）。
"""

INVESTIGATION_LAYERS: tuple[str, ...] = (
    "device_status",
    "device_events",
    "gateway_status",
)
# 不得超过 select_actions 生成的动作数（当前 3 层），否则钳制在最后一层
# 空转；详见 select_actions docstring。
MAX_INVESTIGATION_ROUNDS = 3
HIGH_CONFIDENCE_THRESHOLD = 2
CONVERGENCE_CANDIDATE_LIMIT = 2


def _rank_key(hypothesis: dict) -> tuple:
    """统一排序键：命中数 → 先验 → 排查价值 → key（保证并列确定性）。"""
    return (
        -hypothesis["matched_evidence_count"],
        -hypothesis["prevalence"],
        -hypothesis["investigation_weight"],
        hypothesis["cause_key"],
    )


def compute_hypotheses(
    matched_symptom_keys: list[str],
    causes: list,
    symptom_cause_pairs: list[tuple[str, str]],
) -> list[dict]:
    """计算初始候选假设集。

    - matched_symptom_keys：初始症状 + 已确认证据（本域症状 key）；
    - causes：本域 FaultCause 行（鸭子类型，测试可用 SimpleNamespace）；
    - symptom_cause_pairs：本域 (symptom_key, cause_key) 映射。
    未命中任何症状的原因不入选（并集口径：命中任一即入选）。
    """
    matched = set(matched_symptom_keys)
    hits: dict[str, set[str]] = {}
    for symptom_key, cause_key in symptom_cause_pairs:
        if symptom_key in matched:
            hits.setdefault(cause_key, set()).add(symptom_key)

    hypotheses = []
    for cause in causes:
        matched_keys = hits.get(cause.cause_key)
        if not matched_keys:
            continue
        hypotheses.append(
            {
                "cause_key": cause.cause_key,
                "cause_name": cause.cause_name,
                "description": cause.description,
                "prevalence": cause.prevalence,
                "investigation_weight": cause.investigation_weight,
                "fix_action": cause.fix_action,
                "fix_suggestion": cause.fix_suggestion,
                "matched_evidence_count": len(matched_keys),
                "matched_symptom_keys": sorted(matched_keys),
            }
        )
    hypotheses.sort(key=_rank_key)
    return hypotheses


def shrink(
    hypotheses: list[dict],
    new_evidence: list[str],
    symptom_cause_pairs: list[tuple[str, str]],
) -> list[dict]:
    """用本轮新证据更新命中并重排；返回新列表，不修改入参。"""
    new_set = set(new_evidence)
    updated = []
    for hypothesis in hypotheses:
        new_hits = {
            symptom_key
            for symptom_key, cause_key in symptom_cause_pairs
            if cause_key == hypothesis["cause_key"] and symptom_key in new_set
        } - set(hypothesis["matched_symptom_keys"])
        merged = dict(hypothesis)
        if new_hits:
            merged["matched_symptom_keys"] = sorted(
                set(hypothesis["matched_symptom_keys"]) | new_hits
            )
            merged["matched_evidence_count"] = len(merged["matched_symptom_keys"])
        updated.append(merged)
    updated.sort(key=_rank_key)
    return updated


def high_confidence_count(hypotheses: list[dict]) -> int:
    """领先组大小：与最高命中数并列的候选数。

    最高命中数未达高置信阈值时返回 0（全员并列 1 不算收敛信号）。
    """
    if not hypotheses:
        return 0
    max_count = max(h["matched_evidence_count"] for h in hypotheses)
    if max_count < HIGH_CONFIDENCE_THRESHOLD:
        return 0
    return sum(1 for h in hypotheses if h["matched_evidence_count"] == max_count)


def should_conclude(hypotheses: list[dict], round_index: int) -> bool:
    """状态机边条件：收敛或硬上限即出结论，不依赖 LLM 自觉停下。

    - 轮数达硬上限：强制出结论（可以是「未能定位，建议人工介入」）；
    - 候选为空（未知域）：无从排查，直接出结论；
    - 领先组 1-2 个：高置信子集收敛，出结论。
    """
    if round_index >= MAX_INVESTIGATION_ROUNDS:
        return True
    if not hypotheses:
        return True
    leader_count = high_confidence_count(hypotheses)
    return 0 < leader_count <= CONVERGENCE_CANDIDATE_LIMIT


def select_actions(hypotheses: list[dict], top_n: int = 3) -> list[dict]:
    """按排查价值权重标注每轮排查动作。

    MVP 动作集与分层排查三层一一对应（设备状态 → 事件 → 关联设备/网关，
    按检查成本从低到高执行）；每层标注当轮依据候选（investigation_weight
    最高者），供 trace 与报告转写引用。

    动作数量恒等于 len(INVESTIGATION_LAYERS[:top_n])，与候选数无关
    （候选不足时 leading_cause_key 置 None）。约定 MAX_INVESTIGATION_ROUNDS
    不超过本函数返回的动作数：排查循环按 round_index 钳制取层，超出的
    轮次会重复执行最后一层——确定性检查产出相同证据、不再收缩候选，
    只是空转增加延迟。要加轮数先扩充动作层，而不是调大常量。
    """
    by_weight = sorted(
        hypotheses,
        key=lambda h: (-h["investigation_weight"], h["cause_key"]),
    )
    actions = []
    for index, layer in enumerate(INVESTIGATION_LAYERS[:top_n]):
        leader = by_weight[index] if index < len(by_weight) else None
        actions.append(
            {
                "round": index + 1,
                "layer": layer,
                "leading_cause_key": leader["cause_key"] if leader else None,
            }
        )
    return actions


def build_final_diagnosis(
    hypotheses: list[dict],
    observed_evidence: list[str],
    investigation_round: int,
) -> dict:
    """确定性生成最终诊断结论；报告文本由后续转写节点填充。

    - top-1 命中数达高置信阈值 → status=concluded，附证据与修复建议；
    - 否则（空候选或无区分性证据）→ status=unresolved，建议人工介入。
    结论字段全部为 JSON 原生类型，可安全进入 checkpointer 状态。
    """
    alternatives = [
        {
            "cause_key": h["cause_key"],
            "cause_name": h["cause_name"],
            "fix_suggestion": h["fix_suggestion"],
        }
        for h in hypotheses[1:3]
    ]
    if (
        hypotheses
        and hypotheses[0]["matched_evidence_count"] >= HIGH_CONFIDENCE_THRESHOLD
    ):
        primary = hypotheses[0]
        return {
            "status": "concluded",
            "primary_cause_key": primary["cause_key"],
            "primary_cause_name": primary["cause_name"],
            "primary_description": primary["description"],
            "evidence": list(observed_evidence),
            "matched_symptom_keys": list(primary["matched_symptom_keys"]),
            "fix_action": primary["fix_action"],
            "fix_suggestion": primary["fix_suggestion"],
            "alternatives": alternatives,
            "investigation_rounds": investigation_round,
            "report": "",
        }
    top = hypotheses[0] if hypotheses else None
    return {
        "status": "unresolved",
        # unresolved 也携带 top 假设与修复建议（弱证据低置信建议）：
        # 交由审批门控复核，高风险动作照常被规则表拦下走审批——
        # 这正是「网关重启」演示主路径的入口。
        "primary_cause_key": top["cause_key"] if top else None,
        "primary_cause_name": top["cause_name"] if top else None,
        "primary_description": (
            "未能定位具体原因，建议人工介入进一步检查。"
            if hypotheses
            else "未能识别设备或症状，建议人工介入进一步检查。"
        ),
        "evidence": list(observed_evidence),
        "matched_symptom_keys": [],
        "fix_action": top["fix_action"] if top else None,
        # fix_suggestion 会被报告模板直接插值：无 top 时必须给人工兜底话术，
        # 否则报告渲染出字面 None（已复现的空候选路径）。
        "fix_suggestion": (top["fix_suggestion"] if top else "建议联系人工介入排查。"),
        "alternatives": alternatives,
        "investigation_rounds": investigation_round,
        "report": "",
    }
