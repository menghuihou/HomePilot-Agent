"""评估结果统一 schema：确定性 / 语义评估共用同一份结果结构。

字段口径与总计划十一.2 一致：case_id / subgraph / model / input_digest /
expected / actual / score / fallback / trace_id；在此基础上增加 passed
布尔字段——语义用例的阈值判断（score ≥ threshold）归执行器声明，
harness 汇总只看 passed，不在 schema 里重复阈值知识。
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EvalResult(BaseModel):
    """单条评估用例的结果，所有子图、所有评估类型共用。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, description="用例唯一标识，与 golden 行一一对应")
    subgraph: str = Field(
        min_length=1, description="所属子图：diagnose / chat / inspect / router"
    )
    model: str = Field(
        default="deterministic",
        description="产出 actual 的模型；确定性用例固定 deterministic",
    )
    input_digest: str = Field(
        min_length=1,
        description="输入内容 sha256 摘要，用于追溯用例是否被改过",
    )
    expected: Any = Field(
        default=None, description="期望结果：结构化期望或 judge criteria 描述"
    )
    actual: Any = Field(default=None, description="实际结果")
    score: float = Field(ge=0.0, le=1.0, description="确定性用例 1/0；语义用例 0-1")
    passed: bool = Field(
        description="执行器声明的通过与否（语义用例按 threshold 判定）"
    )
    fallback: bool = Field(
        default=False, description="是否走了降级路径（第 8 周接入降级评估）"
    )
    trace_id: str = Field(default="", description="Langfuse trace id；确定性用例可为空")


def validate_result(data: Mapping[str, Any]) -> EvalResult:
    """校验执行器产出的原始结果 dict；失败时抛出带 case_id 的 ValueError。"""
    try:
        return EvalResult.model_validate(dict(data))
    except ValidationError as exc:
        case_id = data.get("case_id", "<未知用例>")
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValueError(f"EvalResult 校验失败 case_id={case_id}: {details}") from exc


def input_digest_for(value: Any) -> str:
    """规范化 JSON 后取 sha256 前 16 位；同一输入摘要稳定，输入被改则摘要变化。"""
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class EvalSummary(BaseModel):
    """一次评估运行的汇总：结果列表 + 通过 / 失败统计 + 红线失败清单。"""

    results: list[EvalResult]
    red_line_failed: list[str] = Field(
        default_factory=list,
        description="带 red_line 标签且未通过的 case_id；CI 红线兜底依据",
    )

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def failed_case_ids(self) -> list[str]:
        return [result.case_id for result in self.results if not result.passed]

    @property
    def all_passed(self) -> bool:
        return self.failed_count == 0

    def as_dict(self) -> dict[str, Any]:
        """输出 / 落盘用的普通 dict 结构（JSON 可序列化）。"""
        return {
            "total": self.total,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "all_passed": self.all_passed,
            "red_line_failed": self.red_line_failed,
            "results": [result.model_dump() for result in self.results],
        }
