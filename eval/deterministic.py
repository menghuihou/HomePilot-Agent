"""确定性评估执行器：case_type → 被测纯函数的分派映射（第 5 周第 3 步）。

全部零 LLM、零数据库连接：被测对象是 app/agent 下的确定性纯函数、
模型静态结构断言、MCP 工具标注枚举。golden 行字段见实施计划第 6 节；
期望值是「当前正确行为的快照」，逐条与实现核对后录入，score 只取 1/0。

与计划 21 例表格的诚实口径修正（第 12 节原则：不凑不存在的行为）：
- 第 6 例「不重复排查」：当前排查动作按固定三层顺序执行，尚无
  「已确认证据 → 跳过对应排查项」的自适应逻辑（随第 6 周网关域扩展
  再补）；该例改为日报话术细分 detect_report_type 的 fail-safe 断言。
- 第 15 例「Router 低置信度」：RouterDecision 当前无置信度字段，
  实现的兜底边界是「LLM 无结构化输出/异常 → 降级 chat 不进诊断」，
  按实际行为断言（与 tests/test_agent_router.py 同款桩模型）。
- 第 16 / 18 例为数据库写入路径，改为审计模型字段清单 / 偏好唯一
  约束名的静态结构断言（字段或约束被删 → 红）。
"""

import asyncio
import contextlib
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import HumanMessage

from app.agent.anomaly_rules import detect_anomalies
from app.agent.context import HomeContext
from app.agent.diagnose import approval_allows_execution
from app.agent.hypotheses import compute_hypotheses, should_conclude, shrink
from app.agent.inspect import (
    detect_report_type,
    format_daily_report,
    query_all_devices,
    sort_anomalies,
)
from app.agent.risk_rules import judge_risk
from app.agent.router import classify_intent
from app.agent.wording import filter_assertions
from app.models import Approval, Preference
from app.schemas import DeviceRead, HomeRead, MemberRead, RoomRead
from eval.harness import Executor


def _judge(payload: Any, expected: Any) -> dict[str, Any]:
    """深比较 actual 与 expected；相等 score=1 通过，否则 0 失败。"""
    passed = payload == expected
    return {"actual": payload, "score": 1.0 if passed else 0.0, "passed": passed}


def _risk_rule(row: dict[str, Any]) -> dict[str, Any]:
    """judge_risk 查表判定：投影结构化判定字段，reason 不参与比较。"""
    decision = judge_risk(row["input"]["action"], None, {})
    actual = {
        "action": decision.action,
        "risk_level": decision.risk_level,
        "require_approval": decision.require_approval,
        "allowed": decision.allowed,
    }
    return _judge(actual, row["expected"])


def _risk_scope_boundary(row: dict[str, Any]) -> dict[str, Any]:
    """范围越界阻断：目标设备不在授权清单 → allowed=False。"""
    decision = judge_risk(
        row["input"]["action"],
        row["input"]["target_device_id"],
        row["input"]["scope"],
    )
    return _judge({"allowed": decision.allowed}, row["expected"])


def _round_cap(row: dict[str, Any]) -> dict[str, Any]:
    """排查硬上限：轮数达 MAX_INVESTIGATION_ROUNDS 强制出结论。"""
    hypotheses = [
        {"cause_key": f"c{i}", "matched_evidence_count": count}
        for i, count in enumerate(row["input"]["matched_counts"])
    ]
    actual = {
        "should_conclude": should_conclude(hypotheses, row["input"]["round_index"])
    }
    return _judge(actual, row["expected"])


def _hypotheses_shrink(row: dict[str, Any]) -> dict[str, Any]:
    """候选假设收缩：首轮并集候选数 + 证据收缩后领先组与 top-1。"""
    spec = row["input"]
    causes = [
        SimpleNamespace(
            cause_key=c["cause_key"],
            cause_name=c["cause_key"],
            description="",
            prevalence=c["prevalence"],
            investigation_weight=c["investigation_weight"],
            fix_action="restart_gateway",
            fix_suggestion="",
        )
        for c in spec["causes"]
    ]
    pairs = [tuple(pair) for pair in spec["symptom_cause_pairs"]]
    initial = compute_hypotheses(spec["initial_symptoms"], causes, pairs)
    current = initial
    for evidence in spec["evidence_rounds"]:
        current = shrink(current, evidence, pairs)
    max_count = max(h["matched_evidence_count"] for h in current)
    leaders = sum(1 for h in current if h["matched_evidence_count"] == max_count)
    actual = {
        "initial_count": len(initial),
        "final_leaders": leaders,
        "final_top_cause": current[0]["cause_key"],
    }
    return _judge(actual, row["expected"])


def _approval_decision_gate(row: dict[str, Any]) -> dict[str, Any]:
    """审批拒绝不执行（接缝语义断言，非端到端）。

    本用例钉住的是 fail-close 口径本身：仅 approved 放行，其余
    （rejected / blocked / 缺失决策）一律不执行；tool_calls 由接缝
    布尔值推导，方便 golden 表达「调用数为 0」的计划口径。
    真实的门控接线（interrupt → resume → 执行器不被调用）由
    tests/test_approvals_api.py::test_high_risk_rejected_skips_execution
    走真实图覆盖，两层职责不同、互不替代。
    """
    allowed = approval_allows_execution(row["input"]["decision"])
    actual = {"allowed_execution": allowed, "tool_calls": 1 if allowed else 0}
    return _judge(actual, row["expected"])


def _wording_filter(row: dict[str, Any]) -> dict[str, Any]:
    """措辞过滤：断言词替换结果 + 替换记录（tuple → list 对齐 JSON）。"""
    filtered, replacements = filter_assertions(row["input"]["text"])
    actual = {
        "filtered": filtered,
        "replacements": [list(pair) for pair in replacements],
    }
    return _judge(actual, row["expected"])


def _anomaly_rule(row: dict[str, Any]) -> dict[str, Any]:
    """异常检测规则：投影 (rule, severity) 列表，话术明细不参与比较。"""
    anomalies = detect_anomalies(
        row["input"].get("device_id", 1),
        row["input"].get("name", "测试设备"),
        row["input"]["device_type"],
        row["input"]["status"],
        datetime.fromisoformat(row["input"]["now"]),
    )
    actual = [
        {"rule": item["rule"], "severity": item["severity"]} for item in anomalies
    ]
    return _judge(actual, row["expected"])


def _anomaly_sort(row: dict[str, Any]) -> dict[str, Any]:
    """异常排序：severity 高→低，同 severity 按规则表固定顺序。"""
    sorted_items = sort_anomalies(row["input"]["anomalies"])
    return _judge(
        {"order": [item["device_id"] for item in sorted_items]}, row["expected"]
    )


def _report_type_route(row: dict[str, Any]) -> dict[str, Any]:
    """日报话术细分：未命中关键词默认纯巡检（宁可少不误升）。"""
    return _judge(
        {"report_type": detect_report_type(row["input"]["text"])}, row["expected"]
    )


def _make_home_context() -> HomeContext:
    """固定最小家庭上下文（与 tests/test_agent_router.py 同构）。"""
    now = datetime.now()
    base = {
        "synthetic": False,
        "source_type": "user",
        "created_at": now,
        "updated_at": now,
    }
    return HomeContext(
        home=HomeRead(id=1, name="测试之家", address=None, **base),
        rooms=[RoomRead(id=1, home_id=1, name="客厅", room_type=None, **base)],
        members=[MemberRead(id=1, home_id=1, name="小明", role="admin", **base)],
        devices=[
            DeviceRead(
                id=10,
                home_id=1,
                name="客厅灯",
                device_type="light",
                vendor=None,
                room_id=1,
                **base,
            )
        ],
        preferences=[],
        current_mode="normal",
    )


class _BrokenChatModel:
    """模拟 LLM 无结构化输出 / 网关异常的桩模型（复用单测口径）。"""

    def with_structured_output(self, schema):
        class _Structured:
            async def ainvoke(self, messages):
                raise ValueError("模拟结构化输出失败")

        return _Structured()


@contextlib.contextmanager
def _quiet_logs():
    """路由桩用例临时降噪：降级行为由返回值断言，告警日志与断言无关。"""
    saved: dict[str, int] = {}
    for name in ("app", "langfuse"):
        logger_obj = logging.getLogger(name)
        saved[name] = logger_obj.level
        logger_obj.setLevel(logging.ERROR)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


async def _router_fallback_chat_async(row: dict[str, Any]) -> dict[str, Any]:
    with _quiet_logs():
        intent = await classify_intent(
            [HumanMessage(content=row["input"]["message"])],
            _make_home_context(),
            chat_model=_BrokenChatModel(),
        )
    return {"intent": intent}


def _router_fallback_chat(row: dict[str, Any]) -> dict[str, Any]:
    """Router 兜底：LLM 异常 → 降级 chat，不进诊断子图。"""
    return _judge(asyncio.run(_router_fallback_chat_async(row)), row["expected"])


def _approval_record_shape(row: dict[str, Any]) -> dict[str, Any]:
    """审计完整性：Approval 模型必须保留全部审计字段（删字段 → 红）。"""
    columns = {column.name for column in Approval.__table__.columns}
    missing = [f for f in row["expected"]["required_fields"] if f not in columns]
    return _judge({"missing": missing}, {"missing": []})


def _preference_constraint(row: dict[str, Any]) -> dict[str, Any]:
    """偏好覆盖语义的地基：唯一约束存在，upsert 才是单行覆盖。"""
    names = {constraint.name for constraint in Preference.__table__.constraints}
    exists = row["expected"]["constraint_name"] in names
    return _judge({"constraint_exists": exists}, {"constraint_exists": True})


def _daily_report_elements(row: dict[str, Any]) -> dict[str, Any]:
    """日报三要素：汇总 / 提醒 / 待确认清单必须齐备。"""
    spec = row["input"]
    text = format_daily_report(
        spec["summary_text"], spec["reminders"], spec["pending_approvals"]
    )
    actual = {
        "has_summary": spec["summary_text"] in text,
        "has_reminders": "提醒：" in text and all(r in text for r in spec["reminders"]),
        "has_pending": "待确认操作" in text
        and all(item["action"] in text for item in spec["pending_approvals"]),
    }
    return _judge(actual, row["expected"])


async def _mcp_tools_annotation_async(row: dict[str, Any]) -> dict[str, Any]:
    from mcp_servers import light_server

    tools = await light_server.mcp.list_tools()
    unannotated = [
        tool.name for tool in tools if "风险等级" not in (tool.description or "")
    ]
    return {"unannotated": unannotated, "tool_count": len(tools)}


def _mcp_tools_annotation(row: dict[str, Any]) -> dict[str, Any]:
    """MCP 工具标注完整：每个工具描述都必须带「风险等级」（无未标注）。"""
    return _judge(asyncio.run(_mcp_tools_annotation_async(row)), row["expected"])


async def _inspect_full_coverage_async(row: dict[str, Any]) -> dict[str, Any]:
    spec = row["input"]
    devices = [
        {"id": i, "name": f"设备{i}", "device_type": "light"}
        for i in range(1, spec["device_count"] + 1)
    ]
    failing_id = spec["failing_device_id"]

    async def fetcher(device: dict) -> dict:
        if device["id"] == failing_id:
            raise RuntimeError("模拟 MCP 调用失败")
        return {"online": True}

    results = await query_all_devices(devices, fetcher)
    return {
        "count": len(results),
        "order_ok": [r["device_id"] for r in results] == [d["id"] for d in devices],
        "failed_marked": "error" in results[failing_id - 1],
    }


def _inspect_full_coverage(row: dict[str, Any]) -> dict[str, Any]:
    """巡检全覆盖：单设备失败不静默丢设备，结果与传入严格保序。"""
    return _judge(asyncio.run(_inspect_full_coverage_async(row)), row["expected"])


EXECUTORS: dict[str, Executor] = {
    "risk_rule": _risk_rule,
    "risk_scope_boundary": _risk_scope_boundary,
    "round_cap": _round_cap,
    "hypotheses_shrink": _hypotheses_shrink,
    "approval_decision_gate": _approval_decision_gate,
    "wording_filter": _wording_filter,
    "anomaly_rule": _anomaly_rule,
    "anomaly_sort": _anomaly_sort,
    "report_type_route": _report_type_route,
    "router_fallback_chat": _router_fallback_chat,
    "approval_record_shape": _approval_record_shape,
    "preference_constraint": _preference_constraint,
    "daily_report_elements": _daily_report_elements,
    "mcp_tools_annotation": _mcp_tools_annotation,
    "inspect_full_coverage": _inspect_full_coverage,
}
