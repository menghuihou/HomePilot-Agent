"""第 4 步：确定性 golden 集接入 pytest —— 用例失败即测试红。

用例数据与断言逻辑分离：golden JSONL 是唯一用例源，本文件只定义
执行方式（经 harness 参数化逐例断言），不写任何业务断言。
任何人改动 risk_rules 等被测实现打破 golden 期望，pytest 直接红。

红线子集单独点名：uv run pytest tests/test_eval_cases.py -m red_line -q
"""

from pathlib import Path

import pytest

from eval.deterministic import EXECUTORS
from eval.harness import load_cases, run_cases
from eval.schema import EvalResult, EvalSummary

GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent
    / "eval"
    / "golden"
    / "deterministic_cases.jsonl"
)

# 模块级执行一次：数据 bug（坏行 / 缺字段 / case_id 重复）在收集期就报错，
# 不吞不拖；执行器异常已被 harness 降级为该用例失败结果。
SUMMARY: EvalSummary = run_cases(GOLDEN_PATH, EXECUTORS)
CASES = load_cases(GOLDEN_PATH)
TAGS = {row["case_id"]: row.get("tags", []) for row in CASES}

CASE_PARAMS = [
    pytest.param(
        result,
        id=result.case_id,
        marks=pytest.mark.red_line if "red_line" in TAGS[result.case_id] else (),
    )
    for result in SUMMARY.results
]


@pytest.mark.parametrize("result", CASE_PARAMS)
def test_golden_case_passed(result: EvalResult) -> None:
    """逐例断言：失败信息带 case_id + expected / actual 对照。"""
    assert result.passed, (
        f"{result.case_id}: score={result.score} "
        f"expected={result.expected} actual={result.actual}"
    )
