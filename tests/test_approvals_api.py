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

import app.agent.chat as agent_chat_module
import app.agent.diagnose as diagnose_module
import app.agent.risk_rules as risk_rules_module
import app.routers.approvals as approvals_router
from app.agent.chat import build_agent_graph
from app.agent.diagnose import DiagnosisReport, SymptomSlots, build_diagnose_graph
from app.agent.risk_rules import RiskRule
from app.config import get_settings
from app.main import application
from tests.test_agent_chat import StubRouterModel
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


def test_advice_action_without_interrupt_or_record(client, home_id):
    """人工建议类动作（无执行器）：不走审批门控，不产生审批记录。"""
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

        assert "__interrupt__" not in result
        approval = result["final_diagnosis"]["approval"]
        assert approval["decision"] == "advice"
        assert result["final_diagnosis"]["risk_level"] == "low"
        assert result["final_diagnosis"]["report"] == "可能是停电了。"
        rows = _approval_rows(thread_id)
        assert not rows, "人工建议类动作不应产生审批记录"
    finally:
        monkeypatch.undo()
        _cleanup_approvals(thread_id)


def test_executable_low_action_auto_records_without_interrupt(client, home_id):
    """可执行低风险动作（规则表登记）：decision=auto 落库留痕，不挂起。"""
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
    # 注入一条可执行低风险规则，覆盖 auto 放行分支。
    monkeypatch.setattr(
        risk_rules_module,
        "_RULES_BY_ACTION",
        {
            **risk_rules_module._RULES_BY_ACTION,
            "check_power": RiskRule(
                action="check_power", level="low", require_approval=False
            ),
        },
    )
    # check_power 同时注册为可执行动作：进入 judge_risk 的 auto 分支。
    monkeypatch.setattr(
        diagnose_module,
        "EXECUTABLE_ACTIONS",
        {*diagnose_module.EXECUTABLE_ACTIONS, "check_power"},
    )
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

        assert "__interrupt__" not in result
        approval = result["final_diagnosis"]["approval"]
        assert approval["decision"] == "auto"
        assert result["final_diagnosis"]["risk_level"] == "low"
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
    monkeypatch.setattr(approvals_router, "session_factory", session_maker)

    async def _fake_get_graph():
        return graph

    # approvals 路由在自己的命名空间直接导入 get_agent_graph：
    # patch 必须打在 approvals_router 上才能真正生效。
    monkeypatch.setattr(approvals_router, "get_agent_graph", _fake_get_graph)
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
                    first = await ac.post(
                        f"/approvals/{thread_id}/decision",
                        json={"decision": "approved", "reason": "同意"},
                    )
                    # 幂等回归：重复提交（双击/刷新）返回已处理而非 404。
                    repeat = await ac.post(
                        f"/approvals/{thread_id}/decision",
                        json={"decision": "approved", "reason": "同意"},
                    )
                    return first, repeat
            finally:
                await engine.dispose()

        resp, repeat = asyncio.run(_flow())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"] == thread_id
        assert body["decision"] == "approved"
        assert body["status"] == "resumed"
        assert "操作已执行：模拟执行成功" in body["report"]
        assert executor_calls == ["restart_gateway"]
        assert _approval_rows(thread_id)[-1][0] == "approved"
        # 幂等：重复提交不报错，返回已处理状态。
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "already_processed"
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


def test_agent_graph_routes_diagnose_and_resumes(client, home_id):
    """第 10 步验收：主图挂载诊断子图，interrupt 传播到主图并可恢复。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="device_abnormal"),
        report=DiagnosisReport(
            report="可能是网关离线导致。", primary_cause_key="gateway_offline"
        ),
    )
    investigator = _scripted_investigator(HIGH_RISK_SCRIPT)
    executor_calls: list[str] = []
    diagnose_graph = build_diagnose_graph(
        chat_model=model,
        investigator=investigator,
        executor=_stub_executor(executor_calls),
    )
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    agent = build_agent_graph(
        router_model=StubRouterModel("diagnose"),
        chat_model=model,
        diagnose_graph=diagnose_graph,
        checkpointer=MemorySaver(),
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(diagnose_module, "session_factory", session_maker)
    # 主图 load_context / chat 子图用的是 agent_chat.session_factory
    # （真实 app engine 绑定 TestClient 门户循环），必须一并替换，
    # 否则跨循环 RuntimeError 会毒化共享连接池并污染后续测试。
    monkeypatch.setattr(agent_chat_module, "session_factory", session_maker)
    thread_id = f"ap-{uuid.uuid4().hex[:8]}"
    try:

        async def _run():
            try:
                config = {"configurable": {"thread_id": thread_id}}
                first = await agent.ainvoke(
                    {
                        "messages": [HumanMessage(content="家里灯不对劲")],
                        "home_id": home_id,
                    },
                    config=config,
                )
                second = await agent.ainvoke(
                    Command(resume={"decision": "approved", "reason": ""}),
                    config=config,
                )
                return first, second
            finally:
                await engine.dispose()

        first, second = asyncio.run(_run())

        # 子图的 interrupt 传播到主图层级。
        assert first.get("__interrupt__")
        # 恢复后走完报告节点，to_answer 把报告作为最终回答返回。
        assert second["intent"] == "diagnose"
        assert "操作已执行：模拟执行成功" in second["answer"]
        approval = second["final_diagnosis"]["approval"]
        assert approval["decision"] == "approved"
        assert executor_calls == ["restart_gateway"]
        assert _approval_rows(thread_id)[-1][0] == "approved"
    finally:
        monkeypatch.undo()
        _cleanup_approvals(thread_id)
