"""审批门控测试：interrupt 挂起、Command 恢复两条路径、approvals 落库与决策接口。

LLM 与执行器全部用桩；数据库走真实本地库验证审计落库。
测试结束后按 thread_id 清理审批审计行。
"""

import asyncio
import uuid

import httpx
import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.diagnose as diagnose_module
import app.routers.approvals as approvals_router
from app.agent.diagnose import DiagnosisReport, SymptomSlots, build_diagnose_graph
from app.config import get_settings
from app.main import application
from tests.test_agent_diagnose import (
    StubDiagnosisModel,
    _create_device,
    _scripted_investigator,
)

# 演示主路径证据：3 轮排查后 gateway_offline（restart_gateway=high）成为主结论。
HIGH_RISK_SCRIPT = {
    "device_status": ["light_offline"],
    "device_events": ["light_group_diff_sync"],
    "gateway_status": [],
}


def _conninfo() -> str:
    return get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def _approval_rows(thread_id: str) -> list[tuple]:
    """返回该会话的审批审计行（decision, action, risk_level）。"""
    with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT decision, action, risk_level FROM approvals
             WHERE thread_id = %s ORDER BY id
            """,
            (thread_id,),
        )
        return cur.fetchall()


def _cleanup_approvals(thread_id: str) -> None:
    with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM approvals WHERE thread_id = %s", (thread_id,))
        conn.commit()


def _build_graph(model, investigator, executor=None):
    """构建带内存 checkpointer 的诊断子图，并 patch 掉模块级 session_factory。"""
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    graph = build_diagnose_graph(
        chat_model=model,
        investigator=investigator,
        executor=executor,
        checkpointer=MemorySaver(),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(diagnose_module, "session_factory", session_maker)
    return graph, monkeypatch, engine, session_maker


def _stub_executor(calls: list[str]):
    async def _executor(action: str, diagnosis: dict) -> str:
        calls.append(action)
        return "模拟执行成功"

    return _executor


def test_high_risk_interrupts_until_approved(client, home_id):
    """验收路径一：高风险挂起 → approved → 执行器调用 + 审批落库 approved。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="device_abnormal"),
        report=DiagnosisReport(
            report="可能是网关离线导致，已修复。", primary_cause_key="gateway_offline"
        ),
    )
    investigator = _scripted_investigator(HIGH_RISK_SCRIPT)
    executor_calls: list[str] = []
    graph, monkeypatch, engine, _ = _build_graph(
        model, investigator, _stub_executor(executor_calls)
    )
    thread_id = f"ap-{uuid.uuid4().hex[:8]}"
    try:

        async def _run():
            try:
                config = {"configurable": {"thread_id": thread_id}}
                first = await graph.ainvoke(
                    {
                        "messages": [HumanMessage(content="家里灯不对劲")],
                        "home_id": home_id,
                    },
                    config=config,
                )
                second = await graph.ainvoke(
                    Command(resume={"decision": "approved", "reason": ""}),
                    config=config,
                )
                return first, second
            finally:
                await engine.dispose()

        first, second = asyncio.run(_run())

        # 第一段：挂起，产出 pending 审批与 interrupt 载荷。
        interrupts = first.get("__interrupt__") or ()
        assert len(interrupts) == 1
        payload = interrupts[0].value
        assert payload["action"] == "restart_gateway"
        assert payload["risk_level"] == "high"
        assert payload["thread_id"] == thread_id
        rows = _approval_rows(thread_id)
        # 整段流程跑完后审批行已就地更新；pending 唯一约束保证只有这一行。
        assert len(rows) == 1
        assert rows[-1][1] == "restart_gateway"

        # 第二段：approved → 执行器调用，审计落库 approved。
        assert executor_calls == ["restart_gateway"]
        approval = second["final_diagnosis"]["approval"]
        assert approval["decision"] == "approved"
        assert second["final_diagnosis"]["execution_result"] == "模拟执行成功"
        assert "操作已执行：模拟执行成功" in second["final_diagnosis"]["report"]
        assert _approval_rows(thread_id)[-1][0] == "approved"
    finally:
        monkeypatch.undo()
        _cleanup_approvals(thread_id)


def test_high_risk_rejected_skips_execution(client, home_id):
    """验收路径二：高风险挂起 → rejected → 不执行，报告改写固定话术。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="device_abnormal"),
        report=DiagnosisReport(
            report="可能是网关离线导致，已修复。", primary_cause_key="gateway_offline"
        ),
    )
    investigator = _scripted_investigator(HIGH_RISK_SCRIPT)
    executor_calls: list[str] = []
    graph, monkeypatch, engine, _ = _build_graph(
        model, investigator, _stub_executor(executor_calls)
    )
    thread_id = f"ap-{uuid.uuid4().hex[:8]}"
    try:

        async def _run():
            try:
                config = {"configurable": {"thread_id": thread_id}}
                first = await graph.ainvoke(
                    {
                        "messages": [HumanMessage(content="家里灯不对劲")],
                        "home_id": home_id,
                    },
                    config=config,
                )
                second = await graph.ainvoke(
                    Command(resume={"decision": "rejected", "reason": ""}),
                    config=config,
                )
                return first, second
            finally:
                await engine.dispose()

        first, second = asyncio.run(_run())

        assert first.get("__interrupt__")
        # 拒绝后不调用执行器，报告改写固定话术。
        assert executor_calls == []
        assert second["final_diagnosis"]["approval"]["decision"] == "rejected"
        assert second["final_diagnosis"]["execution_result"] == "未执行"
        assert second["final_diagnosis"]["report"] == "已记录，未执行，建议人工处理。"
        assert _approval_rows(thread_id)[-1][0] == "rejected"
    finally:
        monkeypatch.undo()
        _cleanup_approvals(thread_id)


def test_low_risk_auto_without_interrupt(client, home_id):
    """低风险动作不挂起：decision=auto 直接落库并正常出报告。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="light_not_on"),
        report=DiagnosisReport(
            report="可能是停电了。", primary_cause_key="power_outage"
        ),
    )
    investigator = _scripted_investigator(
        {"device_status": ["light_offline"], "device_events": [], "gateway_status": []}
    )
    graph, monkeypatch, engine, _ = _build_graph(model, investigator)
    thread_id = f"ap-{uuid.uuid4().hex[:8]}"
    try:

        async def _run():
            try:
                return await graph.ainvoke(
                    {
                        "messages": [HumanMessage(content="客厅灯不亮了")],
                        "home_id": home_id,
                    },
                    config={"configurable": {"thread_id": thread_id}},
                )
            finally:
                await engine.dispose()

        result = asyncio.run(_run())

        # check_power 在规则表为 low → 自动放行，不挂起审批。
        assert "__interrupt__" not in result
        approval = result["final_diagnosis"]["approval"]
        assert approval["decision"] == "auto"
        assert result["final_diagnosis"]["risk_level"] == "low"
        assert result["final_diagnosis"]["report"] == "可能是停电了。"
        rows = _approval_rows(thread_id)
        assert rows[-1][0] == "auto"
        assert rows[-1][1] == "check_power"
        assert rows[-1][2] == "low"
    finally:
        monkeypatch.undo()
        _cleanup_approvals(thread_id)


def test_decision_endpoint_resumes_approved(client, home_id):
    """决策接口：按 thread_id 恢复挂起审批，返回含执行结果的最终报告。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="device_abnormal"),
        report=DiagnosisReport(
            report="可能是网关离线导致。", primary_cause_key="gateway_offline"
        ),
    )
    investigator = _scripted_investigator(HIGH_RISK_SCRIPT)
    executor_calls: list[str] = []
    graph, monkeypatch, engine, session_maker = _build_graph(
        model, investigator, _stub_executor(executor_calls)
    )
    monkeypatch.setattr(approvals_router, "get_diagnose_graph", lambda: graph)
    monkeypatch.setattr(approvals_router, "session_factory", session_maker)
    thread_id = f"ap-{uuid.uuid4().hex[:8]}"
    try:

        async def _flow():
            try:
                transport = httpx.ASGITransport(app=application)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as ac:
                    config = {"configurable": {"thread_id": thread_id}}
                    await graph.ainvoke(
                        {
                            "messages": [HumanMessage(content="家里灯不对劲")],
                            "home_id": home_id,
                        },
                        config=config,
                    )
                    return await ac.post(
                        f"/approvals/{thread_id}/decision",
                        json={"decision": "approved", "reason": "同意"},
                    )
            finally:
                await engine.dispose()

        resp = asyncio.run(_flow())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"] == thread_id
        assert body["decision"] == "approved"
        assert body["status"] == "resumed"
        assert "操作已执行：模拟执行成功" in body["report"]
        assert executor_calls == ["restart_gateway"]
        assert _approval_rows(thread_id)[-1][0] == "approved"
    finally:
        monkeypatch.undo()
        _cleanup_approvals(thread_id)


def test_decision_endpoint_404_without_pending(client):
    """无挂起审批：404，防止误恢复其他会话的审批。"""
    resp = client.post(
        "/approvals/unknown-thread/decision", json={"decision": "approved"}
    )
    assert resp.status_code == 404


def test_decision_endpoint_rejects_invalid_decision(client):
    """decision 取值受限：非法值 422。"""
    resp = client.post("/approvals/unknown-thread/decision", json={"decision": "maybe"})
    assert resp.status_code == 422
