"""语义评估执行器：DeepEval G-Eval（对话侧）+ RAGAS faithfulness（证据侧）。

分工口径（总计划十一.2）：RAGAS 只做 faithfulness，context 用 golden 里
冻结的证据快照；其余对话侧指标走 DeepEval G-Eval 自定义 criteria。
judge 全部 temperature=0（GLM 兼容端点，与候选生成同一客户端构造）；
judge 失败（超时 / 格式异常）该例记 score=0 + 错误上下文——评估工具
自身的失败不能伪装成通过。

依赖隔离：deepeval / ragas 在 eval 依赖组（pyproject [dependency-groups]
eval），执行器函数内延迟导入——确定性评估与 pytest 主套件不加载这批
重依赖。trace_id 尽力取 Langfuse 当前 trace（未配置观测时为空串）。
"""

import asyncio
import json
import os
from typing import Any

from eval.harness import Executor

_JUDGE_MODEL = os.environ.get("LLM_MODEL", "glm-4.6")
JUDGE_NAME = f"glm-judge:{_JUDGE_MODEL}"


def _build_judge_client():
    """judge 客户端：GLM 兼容端点；temperature=0 是 judge 的硬性要求。"""
    from langchain_anthropic import ChatAnthropic

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise RuntimeError("语义 judge 需要 ANTHROPIC_AUTH_TOKEN（与候选生成同端点）")
    return ChatAnthropic(
        model=_JUDGE_MODEL,
        base_url=os.environ.get(
            "ANTHROPIC_BASE_URL",
            "https://open.bigmodel.cn/api/anthropic",
        ),
        api_key=api_key,
        temperature=0,
        # GLM 思考块先消耗预算：RAGAS 语句抽取与 G-Eval JSON 输出都曾因
        # thinking 吃满预算被截断（LLMDidNotFinish / invalid JSON），放大到 4096。
        max_tokens=4096,
        timeout=120,
    )


def _current_trace_id() -> str:
    """尽力取 Langfuse 当前 trace id；观测未配置时返回空串（不阻塞评估）。"""
    try:
        from eval.deterministic import _quiet_logs

        with _quiet_logs():
            from langfuse import get_client

            return get_client().get_current_trace_id() or ""
    except Exception:  # noqa: BLE001 —— 观测未配置时静默降级为空 trace id
        return ""


def _input_text(row: dict[str, Any]) -> str:
    """G-Eval 的 INPUT 参数：用户消息原文；结构化输入压成 JSON。"""
    data = row["input"]
    if "message" in data:
        return str(data["message"])
    return json.dumps(data, ensure_ascii=False)


def _judge_error(exc: Exception) -> dict[str, Any]:
    """judge 自身失败 → score=0 + 错误上下文（不允许静默跳过）。"""
    return {
        "actual": {"error": f"{type(exc).__name__}: {exc}"},
        "score": 0.0,
        "passed": False,
        "model": JUDGE_NAME,
    }


def _deepeval_judge():
    """DeepEval 自定义模型适配：GLM 端点，关遥测，输出收敛为纯文本。"""
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
    from deepeval.models import DeepEvalBaseLLM

    from app.llm_client import extract_text_content

    client = _build_judge_client()

    class _GLMJudge(DeepEvalBaseLLM):
        def __init__(self) -> None:
            super().__init__(model_name=JUDGE_NAME)

        def load_model(self):
            return client

        def generate(self, prompt: str, **kwargs: Any) -> str:
            return extract_text_content(client.invoke(prompt).content).strip()

        async def a_generate(self, prompt: str, **kwargs: Any) -> str:
            response = await client.ainvoke(prompt)
            return extract_text_content(response.content).strip()

        def get_model_name(self) -> str:
            return JUDGE_NAME

    return _GLMJudge()


def _g_eval(row: dict[str, Any]) -> dict[str, Any]:
    """G-Eval：criteria 来自 golden（行为要求），score 0-1 对比 threshold。"""
    try:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        metric = GEval(
            name=row["metric"],
            criteria=row["expected"]["criteria"],
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            model=_deepeval_judge(),
            threshold=row["threshold"],
        )
        test_case = LLMTestCase(
            input=_input_text(row),
            actual_output=row["candidate"],
        )
        # GLM 偶发输出不合 JSON（invalid JSON）：与管线重试口径一致，重试一次。
        for attempt in (1, 2):
            try:
                metric.measure(test_case)
                break
            except ValueError as exc:
                if attempt == 2:
                    raise
                print(f"G-Eval 输出格式异常（第 {attempt} 次），重试：{exc}")
        score = round(float(metric.score or 0.0), 4)
        return {
            "actual": {"score": score, "reason": metric.reason},
            "score": score,
            "passed": score >= row["threshold"],
            "model": JUDGE_NAME,
            "trace_id": _current_trace_id(),
        }
    except Exception as exc:  # noqa: BLE001 —— judge 失败降级为该例不通过
        return _judge_error(exc)


def _faithfulness(row: dict[str, Any]) -> dict[str, Any]:
    """RAGAS faithfulness：诊断结论是否忠于证据快照（不编造原因）。"""
    try:
        from ragas.dataset_schema import SingleTurnSample
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import Faithfulness

        metric = Faithfulness(llm=LangchainLLMWrapper(_build_judge_client()))
        sample = SingleTurnSample(
            user_input=_input_text(row),
            response=row["candidate"],
            retrieved_contexts=list(row["input"].get("context", [])),
        )
        raw_score = asyncio.run(metric.single_turn_ascore(sample))
        score = round(float(raw_score), 4)
        return {
            "actual": {"score": score, "reason": "ragas faithfulness 0-1"},
            "score": score,
            "passed": score >= row["threshold"],
            "model": JUDGE_NAME,
            "trace_id": _current_trace_id(),
        }
    except Exception as exc:  # noqa: BLE001 —— judge 失败降级为该例不通过
        return _judge_error(exc)


def _semantic_executor(row: dict[str, Any]) -> dict[str, Any]:
    """按 metric 分派：faithfulness 走 RAGAS，其余走 G-Eval。"""
    if row["metric"] == "diagnosis_faithfulness":
        return _faithfulness(row)
    return _g_eval(row)


EXECUTORS: dict[str, Executor] = {"semantic": _semantic_executor}
