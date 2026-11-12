"""pytest 参数化 + JSONL 用例集最小 demo：用例数据与测试逻辑分离。

对应第 5 周实施计划「第 1 步：学习 spike」的第 1 件事。

运行方式（必须显式指定文件，见下方说明）：
    uv run pytest examples/eval_parametrize_demo.py -q

为什么文件名不带 test_ 前缀：默认 `uv run pytest -q` 只收集 tests/ 下的
test_*.py；本文件是学习 demo，显式指定路径 pytest 照样能收集运行，
但不会混进正式测试套件。

跑完后能回答三个问题：
1. 往 JSONL 加一行新 action 的用例，parametrize 测试逻辑为什么永远不用改？
   什么时候被测代码（规则表）必须跟着改？
2. parametrize 的 ids 参数起了什么作用？
3. 用例失败时，怎么让失败信息直接定位到 case_id？
"""

import json
from pathlib import Path

import pytest

# 1) 迷你风险规则表：模拟 app/agent/risk_rules.py 的确定性映射。
#    真实评估集里，这里换成调用被测纯函数，demo 用字典代替。
RISK_RULES = {
    "power_off_all": "high",
    "toggle_light": "low",
    "restart_gateway": "high",
    "restart_all": "high",
    # 在规则表里、但还没进 golden 的动作：留给「练习 A」加用例用。
    "read_sensor": "low",
}


def judge_risk(action: str) -> str:
    """确定性判断：未知动作兜底 high，与 app/agent/risk_rules.py 的
    fail-safe 口径一致（宁可误拦截，不可漏拦截）。"""
    return RISK_RULES.get(action, "high")


def load_golden_cases(path: Path) -> list[dict]:
    """读 JSONL：每行一个独立用例，这就是 golden 集的最小形态。"""
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


GOLDEN_CASES = load_golden_cases(Path(__file__).parent / "eval_parametrize_cases.jsonl")

# 2) 关键一步：模块加载时读一次数据，parametrize 把每行数据变成一个独立测试。
#    ids 让测试名带 case_id，失败时一眼看到是哪条用例红了。
@pytest.mark.parametrize(
    "case",
    GOLDEN_CASES,
    ids=[case["case_id"] for case in GOLDEN_CASES],
)
def test_risk_level_matches_golden(case: dict) -> None:
    actual = judge_risk(case["action"])
    assert actual == case["expected_level"], (
        f"{case['case_id']}: action={case['action']} "
        f"expected={case['expected_level']} actual={actual}"
    )


# 3) 动手验证（核心结论：改的从来不是 parametrize 测试逻辑，而是数据、
#    或必要时被测实现）：
#
#    练习 A（纯数据变更，任何代码都不用动）：
#    往 eval_parametrize_cases.jsonl 加一行
#      {"case_id": "risk-005", "action": "read_sensor", "expected_level": "low"}
#    跑本文件——测试自动多出一条且直接绿。read_sensor 已在规则表里，
#    所以只需要改数据。
#
#    练习 B（数据 + 被测代码一起改，测试逻辑依然一行不动）：
#    加一行规则表里没有的动作，如
#      {"case_id": "risk-006", "action": "unlock_door", "expected_level": "medium"}
#    跑本文件 → risk-006 红（actual=high：未知动作被 fail-safe 兜底拦截）。
#    这不是测试写错了，而是评估集在如实暴露「实现还不认识这个动作」。
#    修法是在 RISK_RULES（真实项目里是 app/agent/risk_rules.py 的规则表）
#    补一条显式规则，parametrize 和断言逻辑不动。
#
#    进阶提醒：真实实现的未知动作也兜底 high，所以「期望 high 的新用例」
#    即使规则表没有该规则也可能碰巧通过（错的原因，对的结果）。真实
#    RiskDecision 带 reason 字段（「命中规则表」vs「未知动作兜底」），
#    严谨的 golden 用例应连 reason 一起断言，确保绿得有理。
