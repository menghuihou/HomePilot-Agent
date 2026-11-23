"""能耗分析子图测试：聚合纯函数与子图链路（第 7 步）。"""

import asyncio
import uuid

import psycopg
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.energy as energy_module
from app.agent.energy import aggregate_energy, build_energy_graph
from app.config import get_settings

DIM = 1024


def _conninfo() -> str:
    return get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def _db_execute(query: str, params: tuple = (), fetch: bool = False):
    with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows


@pytest.fixture()
def energy_home():
    """临时家庭 + 2 台空调 + 8 条能耗事件；结束按外键顺序清理。"""
    home_id = _db_execute(
        "INSERT INTO homes (name, synthetic, source_type) "
        "VALUES (%s, TRUE, 'test') RETURNING id",
        (f"energy-{uuid.uuid4().hex[:6]}",),
        fetch=True,
    )[0][0]
    device_ids = []
    for name in ("客厅空调", "书房空调"):
        device_ids.append(
            _db_execute(
                "INSERT INTO devices (home_id, name, device_type, synthetic, source_type) "
                "VALUES (%s, %s, 'ac', TRUE, 'test') RETURNING id",
                (home_id, name),
                fetch=True,
            )[0][0]
        )
    kwh_by_days_ago = {3: (1.0, 0.5), 2: (2.0, 1.0), 1: (3.0, 1.5), 0: (2.0, 1.0)}
    for days_ago, (kwh_l, kwh_s) in kwh_by_days_ago.items():
        for device_id, kwh in ((device_ids[0], kwh_l), (device_ids[1], kwh_s)):
            _db_execute(
                "INSERT INTO device_events (device_id, event_type, payload, occurred_at, synthetic, source_type) "
                "VALUES (%s, 'energy_report', jsonb_build_object('kwh', %s::float8), now() - (%s * interval '1 day'), TRUE, 'test')",
                (device_id, kwh, days_ago),
            )
    yield home_id, tuple(device_ids)
    _db_execute(
        "DELETE FROM device_events WHERE device_id IN (SELECT id FROM devices WHERE home_id = %s)",
        (home_id,),
    )
    _db_execute("DELETE FROM devices WHERE home_id = %s", (home_id,))
    _db_execute("DELETE FROM homes WHERE id = %s", (home_id,))


def _conninfo_unused():
    pass


class _StubChatModel:
    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, messages, config=None):

        return AIMessage(content=self._content)


class _BoomChatModel:
    async def ainvoke(self, messages, config=None):
        raise RuntimeError("LLM 不可用")


def _run_energy_graph(home_id: int, chat_model):
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    graph = build_energy_graph(chat_model=chat_model)
    patch = pytest.MonkeyPatch()
    patch.setattr(energy_module, "session_factory", session_maker)

    async def _run():
        try:
            return await graph.ainvoke(
                {"home_id": home_id},
                config={"configurable": {"thread_id": f"en-{uuid.uuid4().hex[:8]}"}},
            )
        finally:
            await engine.dispose()

    result = asyncio.run(_run())
    patch.undo()
    return result


def test_aggregate_energy_empty():
    """空输入：全零聚合，无 top consumer。"""
    summary = aggregate_energy([], None)

    assert summary["total_kwh"] == 0
    assert summary["by_device"] == {}
    assert summary["by_day"] == {}
    assert summary["top_consumer"] is None
    assert summary["device_count"] == 0


def test_aggregate_energy_single_device():
    """单设备单条：总量即该条，top consumer 指向它。"""
    events = [
        {"device_id": "1", "device_name": "客厅空调", "kwh": 1.5, "date": "2026-11-20"}
    ]
    summary = aggregate_energy(events, None)

    assert summary["total_kwh"] == 1.5
    assert summary["by_device"]["客厅空调"] == 1.5
    assert summary["by_day"]["2026-11-20"] == 1.5
    assert summary["top_consumer"]["device_name"] == "客厅空调"
    assert summary["device_count"] == 1


def test_aggregate_energy_sums_by_device_and_day():
    """多设备多天：按设备 / 按天分别求和，top consumer 取总量最高。"""
    events = [
        {"device_id": "1", "device_name": "客厅空调", "kwh": 1.0, "date": "2026-11-17"},
        {"device_id": "1", "device_name": "客厅空调", "kwh": 2.0, "date": "2026-11-18"},
        {"device_id": "2", "device_name": "书房空调", "kwh": 0.5, "date": "2026-11-17"},
        {"device_id": "2", "device_name": "书房空调", "kwh": 1.5, "date": "2026-11-18"},
    ]
    summary = aggregate_energy(events, None)

    assert summary["total_kwh"] == 5.0
    assert summary["by_device"]["客厅空调"] == 3.0
    assert summary["by_device"]["书房空调"] == 2.0
    assert summary["by_day"]["2026-11-17"] == 1.5
    assert summary["by_day"]["2026-11-18"] == 3.5
    assert summary["top_consumer"]["device_name"] == "客厅空调"
    assert summary["top_consumer"]["kwh"] == 3.0


def test_aggregate_energy_scope_keeps_latest_days():
    """scope.days=2：仅保留数据中最新的 2 个自然日。"""
    events = [
        {"device_id": "1", "device_name": "客厅空调", "kwh": 1.0, "date": "2026-11-17"},
        {"device_id": "1", "device_name": "客厅空调", "kwh": 1.0, "date": "2026-11-18"},
        {"device_id": "1", "device_name": "客厅空调", "kwh": 1.0, "date": "2026-11-19"},
        {"device_id": "1", "device_name": "客厅空调", "kwh": 1.0, "date": "2026-11-20"},
    ]
    summary = aggregate_energy(events, {"days": 2})

    assert sorted(summary["by_day"]) == ["2026-11-19", "2026-11-20"]
    assert summary["total_kwh"] == 2.0


def test_energy_subgraph_replies_with_stub_summary(energy_home):
    """子图链路：取数 → 聚合 → 桩摘要；状态携带聚合结果与 AI 消息。"""
    home_id, _ = energy_home
    chat_model = _StubChatModel("空调本周用电 12 kWh。")
    result = _run_energy_graph(home_id, chat_model)

    assert result["answer"] == "空调本周用电 12 kWh。"
    summary = result["energy_summary"]
    assert summary["device_count"] == 2
    assert summary["total_kwh"] == 12.0
    assert result["messages"][-1].content == "空调本周用电 12 kWh。"


def test_energy_subgraph_empty_home_uses_template():
    """无 ac 设备与事件：确定性模板兜底（非空回复）。"""
    home_id = _db_execute(
        "INSERT INTO homes (name, synthetic, source_type) "
        "VALUES (%s, TRUE, 'test') RETURNING id",
        (f"energy-empty-{uuid.uuid4().hex[:6]}",),
        fetch=True,
    )[0][0]
    result = _run_energy_graph(home_id, _BoomChatModel())

    assert "暂无空调用电数据" in result["answer"]


def test_energy_subgraph_llm_failure_falls_back_to_template(energy_home):
    """LLM 故障：降级确定性模板（迁移摘要不因展示层故障丢失）。"""
    home_id, _ = energy_home
    result = _run_energy_graph(home_id, _BoomChatModel())

    assert "12.0 kWh" in result["answer"]
