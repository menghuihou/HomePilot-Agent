"""第 2 步验收测试：EvalResult schema 校验 + harness 分派 / 汇总。

用假执行器和临时 JSONL 验证，不碰任何真实业务函数；
golden 行字段与实施计划第 6 节口径一致。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from eval.harness import EvalHarnessError, run_cases
from eval.schema import EvalResult, input_digest_for, validate_result


def _write_golden(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "golden.jsonl"
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


PASS_ROW = {
    "case_id": "risk-001",
    "subgraph": "diagnose",
    "case_type": "risk_rule",
    "input": {"action": "power_off_all"},
    "expected": {"risk_level": "high"},
    "tags": ["red_line"],
}
FAIL_ROW = {
    "case_id": "risk-002",
    "subgraph": "diagnose",
    "case_type": "risk_rule",
    "input": {"action": "toggle_light"},
    "expected": {"risk_level": "low"},
    "tags": [],
}
OTHER_ROW = {
    "case_id": "word-001",
    "subgraph": "diagnose",
    "case_type": "wording_filter",
    "input": {"text": "一定是网关坏了"},
    "expected": {"blocked": True},
    "tags": ["red_line"],
}


def _fake_risk_executor(row: dict[str, Any]) -> dict[str, Any]:
    """假实现：一律判 high（fail-safe），再与期望比对定通过。"""
    matched = row["expected"]["risk_level"] == "high"
    return {
        "actual": {"risk_level": "high"},
        "score": 1.0 if matched else 0.0,
        "passed": matched,
    }


def _fake_wording_executor(row: dict[str, Any]) -> dict[str, Any]:
    """假实现：一律返回拦截成功。"""
    return {"actual": {"blocked": True}, "score": 1.0, "passed": True}


def test_validate_result_fills_defaults() -> None:
    result = validate_result(
        {
            "case_id": "risk-001",
            "subgraph": "diagnose",
            "input_digest": "abc123",
            "score": 1.0,
            "passed": True,
        }
    )
    assert result.model == "deterministic"
    assert result.fallback is False
    assert result.trace_id == ""


def test_validate_result_rejects_bad_score() -> None:
    with pytest.raises(ValueError, match="risk-001"):
        validate_result(
            {
                "case_id": "risk-001",
                "subgraph": "diagnose",
                "input_digest": "abc123",
                "score": 1.5,
                "passed": True,
            }
        )


def test_eval_result_forbids_extra_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        EvalResult.model_validate(
            {
                "case_id": "risk-001",
                "subgraph": "diagnose",
                "input_digest": "abc123",
                "score": 1.0,
                "passed": True,
                "unknown_field": "x",
            }
        )


def test_input_digest_stable_and_sensitive() -> None:
    assert input_digest_for({"action": "a", "b": 1}) == input_digest_for(
        {"b": 1, "action": "a"}
    )
    assert input_digest_for({"action": "a"}) != input_digest_for({"action": "b"})


def test_run_cases_dispatch_and_summary(tmp_path: Path) -> None:
    golden = _write_golden(tmp_path, [PASS_ROW, FAIL_ROW, OTHER_ROW])
    summary = run_cases(
        golden,
        {"risk_rule": _fake_risk_executor, "wording_filter": _fake_wording_executor},
    )
    assert summary.total == 3
    assert summary.passed_count == 2
    assert summary.failed_case_ids == ["risk-002"]
    assert summary.all_passed is False
    # 红线用例（risk-001 / word-001）全部通过，红线失败清单应为空。
    assert summary.red_line_failed == []


def test_run_cases_marks_red_line_failure(tmp_path: Path) -> None:
    golden = _write_golden(tmp_path, [PASS_ROW, OTHER_ROW])

    def failing_wording(row: dict[str, Any]) -> dict[str, Any]:
        return {"actual": {"blocked": False}, "score": 0.0, "passed": False}

    summary = run_cases(
        golden,
        {"risk_rule": _fake_risk_executor, "wording_filter": failing_wording},
    )
    assert summary.failed_case_ids == ["word-001"]
    assert summary.red_line_failed == ["word-001"]


def test_run_cases_executor_exception_becomes_failed_result(tmp_path: Path) -> None:
    golden = _write_golden(tmp_path, [PASS_ROW])

    def broken_executor(row: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("被测函数炸了")

    summary = run_cases(golden, {"risk_rule": broken_executor})
    assert summary.failed_case_ids == ["risk-001"]
    assert summary.results[0].score == 0.0
    assert "RuntimeError" in str(summary.results[0].actual)


def test_run_cases_unknown_case_type_fails_fast(tmp_path: Path) -> None:
    mystery = {**PASS_ROW, "case_type": "mystery"}
    golden = _write_golden(tmp_path, [mystery])
    with pytest.raises(EvalHarnessError, match="mystery"):
        run_cases(golden, {"risk_rule": _fake_risk_executor})


def test_load_cases_bad_line_and_blank_lines(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(EvalHarnessError, match="第 1 行"):
        run_cases(bad, {})

    good = _write_golden(tmp_path, [PASS_ROW])
    good.write_text(good.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    summary = run_cases(good, {"risk_rule": _fake_risk_executor})
    assert summary.total == 1


def test_run_cases_writes_output_summary(tmp_path: Path) -> None:
    golden = _write_golden(tmp_path, [PASS_ROW, FAIL_ROW])
    output = tmp_path / "out" / "summary.json"
    summary = run_cases(
        golden,
        {"risk_rule": _fake_risk_executor},
        output_path=output,
    )
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["total"] == 2
    assert data["failed"] == 1
    assert data["all_passed"] is False
    assert data["results"][0]["case_id"] == "risk-001"
    assert summary.as_dict() == data
