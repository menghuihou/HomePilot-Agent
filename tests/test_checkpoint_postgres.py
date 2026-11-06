"""Postgres checkpointer 断点续跑测试（第 3 周第 9 步验收）。

两个独立的 asyncio 循环 + 两个 AsyncPostgresSaver 实例模拟进程重启：
审批挂起发生在进程 1，决策恢复发生在进程 2，验证审批可跨进程续跑。
"""

import asyncio
import uuid

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.diagnose as diagnose_module
from app.agent.diagnose import DiagnosisReport, SymptomSlots, build_diagnose_graph
from app.config import get_settings
from tests.test_agent_diagnose import (
    StubDiagnosisModel,
    _create_device,
    _scripted_investigator,
)
from tests.test_approvals_api import (
    HIGH_RISK_SCRIPT,
    _cleanup_approvals,
    _conninfo,
)


def _cleanup_checkpoints(thread_id: str) -> None:
    """清理 LangGraph 内部 checkpoint 表（按 thread），保持开发库干净。"""
    with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
        conn.commit()


def _build_flow_graph(home_id: int, executor_calls: list[str]):
    """构建挂起/恢复两段共用的桩模型与排查器（两段各建独立图实例）。"""
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="device_abnormal"),
        report=DiagnosisReport(
            report="可能是网关离线导致。", primary_cause_key="gateway_offline"
        ),
    )
    investigator = _scripted_investigator(HIGH_RISK_SCRIPT)

    async def _executor(action: str, diagnosis: dict) -> str:
        executor_calls.append(action)
        return "模拟执行成功"

    return model, investigator, _executor


def test_postgres_checkpointer_resumes_across_restart(client, home_id):
    """挂起在进程 1，决策恢复在进程 2：审批与报告跨进程续跑成功。"""
    _create_device(client, home_id, "客厅灯", "light")
    thread_id = f"ck-{uuid.uuid4().hex[:8]}"
    executor_calls: list[str] = []
    model, investigator, executor = _build_flow_graph(home_id, executor_calls)
    conninfo = _conninfo()
    try:

        async def _run_process(thread_id: str, resume: dict | None) -> dict:
            """独立循环内构建全部依赖，等价于一个全新的进程。"""
            pool = AsyncConnectionPool(
                conninfo, open=False, kwargs={"autocommit": True}
            )
            await pool.open()
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
            engine = create_async_engine(get_settings().database_url)
            session_maker = async_sessionmaker(engine, expire_on_commit=False)
            graph = build_diagnose_graph(
                chat_model=model,
                investigator=investigator,
                executor=executor,
                checkpointer=saver,
            )
            monkeypatch = pytest.MonkeyPatch()
            monkeypatch.setattr(diagnose_module, "session_factory", session_maker)
            try:
                try:
                    config = {"configurable": {"thread_id": thread_id}}
                    if resume is None:
                        return await graph.ainvoke(
                            {
                                "messages": [HumanMessage(content="家里灯不对劲")],
                                "home_id": home_id,
                            },
                            config=config,
                        )
                    return await graph.ainvoke(Command(resume=resume), config=config)
                finally:
                    await engine.dispose()
            finally:
                monkeypatch.undo()
                await pool.close()

        # 进程 1：跑到审批挂起。
        first = asyncio.run(_run_process(thread_id, resume=None))
        assert first.get("__interrupt__"), "进程 1 应产出审批挂起"
        # 进程 2：模拟重启——全新 saver/引擎/循环，仅执行决策恢复。
        second = asyncio.run(
            _run_process(
                thread_id,
                resume={"decision": "approved", "reason": ""},
            )
        )

        approval = second["final_diagnosis"]["approval"]
        assert approval["decision"] == "approved"
        assert second["final_diagnosis"]["execution_result"] == "模拟执行成功"
        assert "操作已执行" in second["final_diagnosis"]["report"]
        assert executor_calls == ["restart_gateway"]
        with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT decision FROM approvals WHERE thread_id = %s", (thread_id,)
            )
            rows = cur.fetchall()
        assert rows and rows[-1][0] == "approved"
    finally:
        _cleanup_approvals(thread_id)
        _cleanup_checkpoints(thread_id)
