"""统一评估入口：加载 golden 集 → 按 case_type 分派执行器 → 汇总输出。

对应第 5 周实施计划「第 2 步」。所有子图的评估都走本入口，
禁止各子图另写评估脚本（总计划十一.2 口径）。

执行器契约：接收一条 golden 行（dict），返回至少含 score / actual /
passed 的结果 dict；可选 model / fallback / trace_id。执行器运行时异常
会被转为该用例的失败结果（score=0 + 错误上下文），不中断整体——
评估工具自身的失败不能伪装成通过；golden 数据结构错误则快速失败。
"""

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from eval.schema import EvalSummary, input_digest_for, validate_result

# 执行器契约：golden 行 → 结果 dict（至少含 score / actual / passed）。
Executor = Callable[[dict[str, Any]], dict[str, Any]]

# 执行器集合注册表：harness 只认集合名，具体实现延迟导入。
# deterministic 集合在第 3 步落地，semantic 集合在第 6 步落地。
EXECUTOR_SETS: dict[str, tuple[str, str]] = {
    "deterministic": ("eval.deterministic", "EXECUTORS"),
    "semantic": ("eval.semantic", "EXECUTORS"),
}

# golden 行必填字段；tags / metric / threshold 为可选字段。
_REQUIRED_FIELDS = ("case_id", "subgraph", "case_type", "input", "expected")


class EvalHarnessError(Exception):
    """golden 数据或执行器契约错误：数据 bug 必须快速失败、清晰报错。"""


def load_cases(jsonl_path: Path) -> list[dict[str, Any]]:
    """读 JSONL golden 集：每行一个用例；空行跳过，坏行带行号报错。"""
    text = jsonl_path.read_text(encoding="utf-8")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise EvalHarnessError(
                f"{jsonl_path.name} 第 {line_no} 行不是合法 JSON：{exc}"
            ) from exc
        for field in _REQUIRED_FIELDS:
            if field not in row:
                raise EvalHarnessError(
                    f"{jsonl_path.name} 第 {line_no} 行缺少必填字段 {field}"
                )
        case_id = row["case_id"]
        if case_id in seen_ids:
            raise EvalHarnessError(
                f"{jsonl_path.name} 第 {line_no} 行 case_id 重复：{case_id}"
            )
        seen_ids.add(case_id)
        cases.append(row)
    if not cases:
        raise EvalHarnessError(f"{jsonl_path.name} 没有读到任何用例")
    return cases


def run_cases(
    jsonl_path: Path | str,
    executors: Mapping[str, Executor],
    output_path: Path | str | None = None,
) -> EvalSummary:
    """统一评估流程：加载 → 分派 → 产出 EvalResult 列表 → 汇总 → 可选落盘。"""
    results = []
    red_line_failed: list[str] = []
    for row in load_cases(Path(jsonl_path)):
        case_type = row["case_type"]
        executor = executors.get(case_type)
        if executor is None:
            raise EvalHarnessError(
                f"用例 {row['case_id']} 的 case_type={case_type} 没有对应执行器"
            )
        try:
            payload = executor(row)
        except Exception as exc:  # noqa: BLE001 —— 执行器异常降级为该用例失败，不中断整体
            payload = {
                "score": 0.0,
                "actual": {"error": f"{type(exc).__name__}: {exc}"},
                "passed": False,
            }
        missing = [
            field for field in ("score", "actual", "passed") if field not in payload
        ]
        if missing:
            raise EvalHarnessError(
                f"执行器返回缺少 {missing} 字段：用例 {row['case_id']}"
            )
        result = validate_result(
            {
                "case_id": row["case_id"],
                "subgraph": row["subgraph"],
                "model": payload.get("model", "deterministic"),
                "input_digest": input_digest_for(row["input"]),
                "expected": row["expected"],
                "actual": payload["actual"],
                "score": payload["score"],
                "passed": payload["passed"],
                "fallback": payload.get("fallback", False),
                "trace_id": payload.get("trace_id", ""),
            }
        )
        results.append(result)
        if "red_line" in row.get("tags", []) and not result.passed:
            red_line_failed.append(result.case_id)
    summary = EvalSummary(results=results, red_line_failed=red_line_failed)
    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(summary.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return summary


def load_executor_set(name: str) -> dict[str, Executor]:
    """按名称延迟导入执行器集合；对应步骤未完成时给出明确提示。"""
    entry = EXECUTOR_SETS.get(name)
    if entry is None:
        raise EvalHarnessError(f"未知执行器集合：{name}，可选：{sorted(EXECUTOR_SETS)}")
    module_name, attr_name = entry
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise EvalHarnessError(
            f"执行器模块 {module_name} 尚未实现（对应第 3 / 6 步），"
            "harness 骨架已就绪，等执行器落地后即可直接使用"
        ) from exc
    executors = getattr(module, attr_name, None)
    if not isinstance(executors, Mapping):
        raise EvalHarnessError(
            f"{module_name}.{attr_name} 必须是 Mapping[str, Executor]"
        )
    return dict(executors)


def main(argv: list[str] | None = None) -> int:
    """CLI：uv run python -m eval.harness <golden.jsonl> --executor deterministic"""
    parser = argparse.ArgumentParser(description="HomePilot 统一评估入口")
    parser.add_argument("jsonl", type=Path, help="golden 用例集 JSONL 路径")
    parser.add_argument(
        "--executor",
        required=True,
        choices=sorted(EXECUTOR_SETS),
        help="执行器集合名",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="汇总 JSON 落盘路径（可选，默认只打印不落盘）",
    )
    args = parser.parse_args(argv)
    try:
        summary = run_cases(args.jsonl, load_executor_set(args.executor), args.output)
    except EvalHarnessError as exc:
        print(f"评估失败：{exc}", file=sys.stderr)
        return 2
    print(
        f"评估完成：total={summary.total} passed={summary.passed_count} "
        f"failed={summary.failed_count}"
    )
    if summary.failed_case_ids:
        print(f"失败用例：{'、'.join(summary.failed_case_ids)}")
    if summary.red_line_failed:
        print(f"红线用例失败（CI 必须阻断）：{'、'.join(summary.red_line_failed)}")
    return 0 if summary.all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
