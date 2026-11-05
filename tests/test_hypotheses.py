"""候选假设纯函数测试：并集口径、排序、收缩、收敛与硬上限（第 3 周第 6 步）。

不依赖数据库与 LLM，原因行用 SimpleNamespace 鸭子类型构造。
"""

from types import SimpleNamespace

from app.agent.hypotheses import (
    compute_hypotheses,
    high_confidence_count,
    select_actions,
    should_conclude,
    shrink,
)


def _cause(key: str, prevalence: float = 0.1, weight: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        cause_key=key,
        cause_name=key,
        description="",
        prevalence=prevalence,
        investigation_weight=weight,
        fix_action=None,
        fix_suggestion="",
    )


# 初始症状 s_init 命中 c0/c1/c2；s_other 只命中 c3（验证未命中原因被排除）。
PAIRS = [
    ("s_init", "c0"),
    ("s_init", "c1"),
    ("s_init", "c2"),
    ("s_other", "c3"),
]


def test_compute_union_includes_any_hit_and_excludes_misses():
    causes = [
        _cause("c0", 0.1),
        _cause("c1", 0.3),
        _cause("c2", 0.2),
        _cause("c3", 0.4),
    ]

    result = compute_hypotheses(["s_init"], causes, PAIRS)

    # 并集口径：c3 未命中初始症状，即便 prevalence 最高也不入选。
    assert [h["cause_key"] for h in result] == ["c1", "c2", "c0"]
    assert all(h["matched_evidence_count"] == 1 for h in result)


def test_evidence_count_beats_prevalence():
    causes = [_cause("c0", 0.1, 1), _cause("c1", 0.3, 5)]
    pairs = PAIRS + [("s_evidence", "c0")]

    result = compute_hypotheses(["s_init", "s_evidence"], causes, pairs)

    assert [h["cause_key"] for h in result] == ["c0", "c1"]
    assert result[0]["matched_evidence_count"] == 2
    assert result[0]["matched_symptom_keys"] == ["s_evidence", "s_init"]


def test_rank_tie_breaks_by_weight_then_key():
    causes = [_cause("cb", 0.2, 1), _cause("ca", 0.2, 1), _cause("cc", 0.2, 5)]
    pairs = [("s", "cb"), ("s", "ca"), ("s", "cc")]

    result = compute_hypotheses(["s"], causes, pairs)

    assert [h["cause_key"] for h in result] == ["cc", "ca", "cb"]


def test_shrink_updates_counts_dedups_and_keeps_input_intact():
    causes = [_cause("c0", 0.5, 5), _cause("c1", 0.1, 1)]
    pairs = [("s_init", "c0"), ("s_init", "c1"), ("s_new", "c0")]
    hypotheses = compute_hypotheses(["s_init"], causes, pairs)
    snapshot = [dict(h) for h in hypotheses]

    # 同轮重复 token 去重，只计一次命中。
    updated = shrink(hypotheses, ["s_new", "s_new"], pairs)

    assert [h["cause_key"] for h in updated] == ["c0", "c1"]
    assert updated[0]["matched_evidence_count"] == 2
    assert updated[1]["matched_evidence_count"] == 1
    assert hypotheses == snapshot  # 入参不被修改


def test_should_conclude_matrix():
    def h(count: int) -> dict:
        return {
            "cause_key": "c",
            "matched_evidence_count": count,
            "prevalence": 0.1,
            "investigation_weight": 3,
        }

    assert should_conclude([], 0) is True  # 未知域：无从排查
    assert should_conclude([h(1)], 3) is True  # 硬上限强制出结论
    assert should_conclude([h(1), h(1)], 1) is False  # 全员并列 1：无收敛信号
    assert should_conclude([h(2), h(2)], 1) is True  # 领先组 = 2
    assert should_conclude([h(2)], 1) is True  # 领先组 = 1
    assert should_conclude([h(2), h(2), h(2)], 1) is False  # 领先组 = 3：继续排查
    assert should_conclude([h(3), h(1)], 2) is True  # 领先组 = 1


def test_two_round_scenario_from_plan():
    """总计划场景：首轮候选全集 → 2 轮区分性证据后领先组收缩到 ≤ 2。"""
    causes = [_cause(f"c{i}", prevalence=0.1 + i * 0.01, weight=3) for i in range(6)]
    pairs = [("s_init", f"c{i}") for i in range(6)]
    # 第 1 轮证据命中 4 个候选（领先组 4 > 2，继续排查）；第 2 轮只加冕 c0/c1。
    pairs += [("s1", "c0"), ("s1", "c1"), ("s1", "c2"), ("s1", "c3")]
    pairs += [("s2", "c0"), ("s2", "c1")]

    hypotheses = compute_hypotheses(["s_init"], causes, pairs)
    assert len(hypotheses) == 6

    # 第 1 轮：s1 命中 c0-c3，领先组 = 4（> 2，继续排查）。
    hypotheses = shrink(hypotheses, ["s1"], pairs)
    assert high_confidence_count(hypotheses) == 4
    assert should_conclude(hypotheses, 1) is False

    # 第 2 轮：s2 只加冕 c0/c1，领先组收缩到 2。
    hypotheses = shrink(hypotheses, ["s2"], pairs)
    counts = {h["cause_key"]: h["matched_evidence_count"] for h in hypotheses}
    assert counts["c0"] == 3 and counts["c1"] == 3
    assert high_confidence_count(hypotheses) == 2
    assert should_conclude(hypotheses, 2) is True


def test_select_actions_layers_and_weight_leaders():
    hypotheses = [
        {
            "cause_key": "gateway_offline",
            "investigation_weight": 5,
            "matched_evidence_count": 2,
            "prevalence": 0.1,
        },
        {
            "cause_key": "bulb_burned",
            "investigation_weight": 5,
            "matched_evidence_count": 1,
            "prevalence": 0.1,
        },
        {
            "cause_key": "socket_loose",
            "investigation_weight": 4,
            "matched_evidence_count": 1,
            "prevalence": 0.1,
        },
    ]

    actions = select_actions(hypotheses)

    assert [a["layer"] for a in actions] == [
        "device_status",
        "device_events",
        "gateway_status",
    ]
    assert [a["round"] for a in actions] == [1, 2, 3]
    # 排查价值并列 5 时按 cause_key 稳定排序。
    assert actions[0]["leading_cause_key"] == "bulb_burned"
    assert actions[1]["leading_cause_key"] == "gateway_offline"
    assert actions[2]["leading_cause_key"] == "socket_loose"
    assert len(select_actions(hypotheses, top_n=2)) == 2
