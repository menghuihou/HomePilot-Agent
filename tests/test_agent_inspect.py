"""巡检子图测试：并行超时降级、保序对齐、排序确定性、模板摘要与骨架流转。

纯逻辑用例不依赖数据库；子图端到端用例走真实本地库 + 注入桩 fetcher，
LLM 转写未接入前（第 8 步）摘要走确定性模板。
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import anomaly_rules as rules
from app.agent import inspect as inspect_module
from app.agent.inspect import (
    QUERY_TIMEOUT_SECONDS,
    InspectSummary,
    _make_llm_summarizer,
    build_inspect_graph,
    format_template_summary,
    query_all_devices,
    sort_anomalies,
)
from app.config import get_settings


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
def inspect_home():
    """创建临时家庭与 2 台设备；测试后按外键依赖顺序清理。"""
    home_id = _db_execute(
        "INSERT INTO homes (name, synthetic, source_type) "
        "VALUES (%s, TRUE, 'test') RETURNING id",
        (f"inspect-{uuid.uuid4().hex[:6]}",),
        fetch=True,
    )[0][0]
    yield home_id
    _db_execute(
        """
        DELETE FROM device_events
         WHERE device_id IN (SELECT id FROM devices WHERE home_id = %s)
        """,
        (home_id,),
    )
    _db_execute("DELETE FROM devices WHERE home_id = %s", (home_id,))
    _db_execute("DELETE FROM homes WHERE id = %s", (home_id,))


def _create_device(home_id: int, name: str, device_type: str) -> int:
    return _db_execute(
        """
        INSERT INTO devices (home_id, name, device_type, synthetic, source_type)
        VALUES (%s, %s, %s, TRUE, 'test') RETURNING id
        """,
        (home_id, name, device_type),
        fetch=True,
    )[0][0]


def _anomaly(rule: str, severity: str, device_id: int) -> dict:
    """构造测试用异常项（结构同规则表输出）。"""
    return {
        "device_id": device_id,
        "device_name": f"设备{device_id}",
        "rule": rule,
        "severity": severity,
        "detail": f"{rule}-{severity}",
    }


class StubSummaryModel:
    """InspectSummary 结构化输出桩；text=None 模拟解析失败，error 模拟调用故障。"""

    def __init__(self, text: str | None = None, error: Exception | None = None):
        self._text = text
        self._error = error
        self.prompts: list[list] = []

    def with_structured_output(self, schema):
        stub = self

        class _Summary:
            async def ainvoke(self, messages, config=None):
                stub.prompts.append(list(messages))
                if stub._error is not None:
                    raise stub._error
                if stub._text is None:
                    return None
                return InspectSummary(summary_text=stub._text)

        return _Summary()


def _summary_anomalies() -> list[dict]:
    """两条结构完整的异常（离线超时 + 低电量），供转写器测试复用。"""
    return [
        {
            "device_id": 19,
            "device_name": "家庭网关",
            "rule": rules.RULE_OFFLINE_TIMEOUT,
            "severity": rules.SEVERITY_HIGH,
            "detail": "家庭网关已离线 3.5 小时（超过阈值 2 小时），建议检查网络或网关",
        },
        {
            "device_id": 2134,
            "device_name": "客厅温湿度传感器",
            "rule": rules.RULE_LOW_BATTERY,
            "severity": rules.SEVERITY_MEDIUM,
            "detail": "客厅温湿度传感器电量 8%（低于阈值 20%），建议尽快更换电池",
        },
    ]


def test_query_all_devices_isolates_single_slow_device():
    """第 6 步核心口径：一台设备卡死，整体巡检 ≈ 超时阈值返回且其余完整。

    注入 0.3 秒超时 + 2 秒卡死，与生产 5 秒阈值同等隔离语义，但亚秒级完成。
    """
    devices = [{"id": i, "name": f"设备{i}", "device_type": "light"} for i in range(8)]

    async def fetcher(device: dict) -> dict:
        if device["id"] == 3:
            await asyncio.sleep(2)  # 模拟卡死设备：远超注入的 0.3 秒阈值
        await asyncio.sleep(0.05)
        return {"online": True, "last_seen_at": datetime.now(UTC).isoformat()}

    start = time.perf_counter()
    results = asyncio.run(query_all_devices(devices, fetcher, timeout_seconds=0.3))
    elapsed = time.perf_counter() - start

    # 总耗时 ≈ 单设备超时阈值（约 0.3 秒），而不是被卡死设备拖到 2 秒。
    assert 0.25 < elapsed < 1.0, f"巡检总耗时 {elapsed:.2f} 秒，超时隔离未生效"
    assert len(results) == 8
    # 卡死设备降级为 query_failed 结构，不炸整体。
    assert "超时" in results[3]["error"]
    # device_id 统一 int（与 anomalies 同轨），且保序对回设备。
    assert results[3]["device_id"] == 3
    # 其余 7 台完整返回，且严格保序对回设备（gather 保序）。
    for index in (0, 1, 2, 4, 5, 6, 7):
        assert results[index]["online"] is True
        assert results[index]["device_id"] == index


def test_query_all_devices_converts_exceptions_to_error_structure():
    """fetcher 抛异常不炸整体：单设备降级为 error 结构。"""
    devices = [
        {"id": 1, "name": "客厅灯", "device_type": "light"},
        {"id": 2, "name": "卧室灯", "device_type": "light"},
    ]

    async def bad_fetcher(device: dict) -> dict:
        raise RuntimeError("MCP 连接崩溃")

    results = asyncio.run(query_all_devices(devices, bad_fetcher))

    assert len(results) == 2
    assert all("error" in result for result in results)
    assert "MCP 连接崩溃" in results[0]["error"]
    # 保序对齐：错误结构携带对应设备标识（统一 int，与 anomalies 同轨）。
    assert results[1]["device_id"] == 2


def test_query_timeout_constant_is_five_seconds():
    """超时阈值锁定为计划口径 5 秒，改动需显式评估。"""
    assert QUERY_TIMEOUT_SECONDS == 5.0


def test_sort_anomalies_severity_then_fixed_rule_order():
    """severity 高→低；同 severity 按规则表固定顺序；同规则稳定保插入序。"""
    anomalies = [
        _anomaly(rules.RULE_QUERY_FAILED, rules.SEVERITY_MEDIUM, 5),
        _anomaly(rules.RULE_OFFLINE_BRIEF, rules.SEVERITY_LOW, 6),
        _anomaly(rules.RULE_LOW_BATTERY, rules.SEVERITY_MEDIUM, 1),
        _anomaly(rules.RULE_OFFLINE_TIMEOUT, rules.SEVERITY_HIGH, 2),
        _anomaly(rules.RULE_LOW_BATTERY, rules.SEVERITY_MEDIUM, 4),
        _anomaly(rules.RULE_NEVER_REPORTED, rules.SEVERITY_MEDIUM, 3),
    ]

    ordered = sort_anomalies(anomalies)

    assert [(a["rule"], a["device_id"]) for a in ordered] == [
        # high 最先
        (rules.RULE_OFFLINE_TIMEOUT, 2),
        # medium 内按规则表顺序：low_battery → never_reported → query_failed
        (rules.RULE_LOW_BATTERY, 1),
        (rules.RULE_LOW_BATTERY, 4),
        (rules.RULE_NEVER_REPORTED, 3),
        (rules.RULE_QUERY_FAILED, 5),
        # low 最后
        (rules.RULE_OFFLINE_BRIEF, 6),
    ]


def test_sort_plan_mixed_scenario_no_omission():
    """第 7 步验收场景：1 离线超时 + 2 低电量 + 1 查询失败。

    期望顺序：离线超时（high）→ 低电量×2 → 查询失败（medium 按规则固定序），
    且四条全部保留无漏项。
    """
    anomalies = [
        _anomaly(rules.RULE_QUERY_FAILED, rules.SEVERITY_MEDIUM, 4),
        _anomaly(rules.RULE_LOW_BATTERY, rules.SEVERITY_MEDIUM, 2),
        _anomaly(rules.RULE_OFFLINE_TIMEOUT, rules.SEVERITY_HIGH, 1),
        _anomaly(rules.RULE_LOW_BATTERY, rules.SEVERITY_MEDIUM, 3),
    ]

    ordered = sort_anomalies(anomalies)

    assert [a["rule"] for a in ordered] == [
        rules.RULE_OFFLINE_TIMEOUT,
        rules.RULE_LOW_BATTERY,
        rules.RULE_LOW_BATTERY,
        rules.RULE_QUERY_FAILED,
    ]
    # 无漏项：四台设备的异常全部保留，设备 id 可完整追溯。
    assert {a["device_id"] for a in ordered} == {1, 2, 3, 4}


def test_template_summary_with_anomalies():
    anomalies = [
        {
            "device_id": 19,
            "device_name": "家庭网关",
            "rule": rules.RULE_OFFLINE_TIMEOUT,
            "severity": rules.SEVERITY_HIGH,
            "detail": "家庭网关已离线 3.5 小时（超过阈值 2 小时），建议检查网络或网关",
        },
        {
            "device_id": 2134,
            "device_name": "客厅温湿度传感器",
            "rule": rules.RULE_LOW_BATTERY,
            "severity": rules.SEVERITY_MEDIUM,
            "detail": "客厅温湿度传感器电量 8%（低于阈值 20%），建议尽快更换电池",
        },
    ]

    text = format_template_summary(8, 6, anomalies)

    assert text.splitlines()[0] == "检查了 8 台设备：6 台正常，2 台异常。"
    assert "1. 家庭网关已离线 3.5 小时" in text
    assert "2. 客厅温湿度传感器电量 8%" in text


def test_template_summary_merges_same_rule_distribution():
    """同类异常合并展示（分布行）但逐台明细全部保留（第 7 步口径）。"""
    anomalies = [
        {
            "device_id": 19,
            "device_name": "家庭网关",
            "rule": rules.RULE_OFFLINE_TIMEOUT,
            "severity": rules.SEVERITY_HIGH,
            "detail": "家庭网关已离线 3.5 小时（超过阈值 2 小时），建议检查网络或网关",
        },
        {
            "device_id": 2134,
            "device_name": "客厅温湿度传感器",
            "rule": rules.RULE_LOW_BATTERY,
            "severity": rules.SEVERITY_MEDIUM,
            "detail": "客厅温湿度传感器电量 8%（低于阈值 20%），建议尽快更换电池",
        },
        {
            "device_id": 2133,
            "device_name": "走廊灯",
            "rule": rules.RULE_LOW_BATTERY,
            "severity": rules.SEVERITY_MEDIUM,
            "detail": "走廊灯电量 15%（低于阈值 20%），建议尽快更换电池",
        },
    ]

    text = format_template_summary(8, 5, anomalies)
    lines = text.splitlines()

    # 表头 → 同类合并分布行（低电量 2 台合并计数）→ 逐台明细 3 行不缺。
    assert lines[0] == "检查了 8 台设备：5 台正常，3 台异常。"
    assert lines[1] == "异常分布：离线超时 1 台；低电量 2 台"
    detail_lines = [line for line in lines if line[0].isdigit()]
    assert len(detail_lines) == 3
    assert "客厅温湿度传感器电量 8%" in text
    assert "走廊灯电量 15%" in text


def test_template_summary_single_anomaly_has_no_distribution_line():
    """单条异常无需分布行，直接表头 + 明细，摘要保持最短。"""
    anomalies = [_anomaly(rules.RULE_OFFLINE_BRIEF, rules.SEVERITY_LOW, 1)]

    text = format_template_summary(5, 4, anomalies)

    assert "异常分布" not in text
    assert text.splitlines()[0] == "检查了 5 台设备：4 台正常，1 台异常。"


def test_template_summary_all_normal():
    assert format_template_summary(5, 5, []) == "检查了 5 台设备，一切正常。"


def test_llm_summarizer_adopts_valid_transcription():
    """合规转写（覆盖全部异常设备名）直接采用，且 LLM 确实被调用。"""
    model = StubSummaryModel(
        "检查了 8 台设备：6 台正常，2 台异常。\n"
        "家庭网关可能故障，建议检查网络或网关；"
        "客厅温湿度传感器电量 8%，建议尽快更换电池。"
    )
    summarize = _make_llm_summarizer(model)

    text = asyncio.run(summarize(8, 6, _summary_anomalies()))

    assert "家庭网关" in text
    assert "客厅温湿度传感器" in text
    assert len(model.prompts) == 1


def test_llm_summarizer_rejects_concealment():
    """第 8 步验收：输入 2 条异常、LLM 称「一切正常」→ 转写被拒，回落模板。"""
    model = StubSummaryModel("一切正常")
    summarize = _make_llm_summarizer(model)

    text = asyncio.run(summarize(8, 6, _summary_anomalies()))

    assert text == format_template_summary(8, 6, _summary_anomalies())
    assert text.startswith("检查了 8 台设备：6 台正常，2 台异常。")


def test_llm_summarizer_applies_wording_filter():
    """第 8 步验收：LLM 输出过度断言词 → 确定性过滤替换。"""
    model = StubSummaryModel(
        "检查了 2 台设备：1 台正常，1 台异常。\n家庭网关一定是坏了，建议重启。"
    )
    summarize = _make_llm_summarizer(model)

    text = asyncio.run(summarize(2, 1, _summary_anomalies()[:1]))

    assert "一定是" not in text
    assert "坏了" not in text
    assert "家庭网关可能是故障" in text


def test_llm_summarizer_falls_back_on_llm_error():
    """LLM 调用故障 → 模板降级，巡检能力不因 LLM 挂而不可用。"""
    model = StubSummaryModel(error=RuntimeError("LLM API 超时"))
    summarize = _make_llm_summarizer(model)

    text = asyncio.run(summarize(8, 6, _summary_anomalies()))

    assert text == format_template_summary(8, 6, _summary_anomalies())


def test_llm_summarizer_skips_llm_when_no_anomalies():
    """无异常时确定性模板即可，LLM 不被调用（零成本路径）。"""
    model = StubSummaryModel("不该被调用")
    summarize = _make_llm_summarizer(model)

    text = asyncio.run(summarize(5, 5, []))

    assert text == "检查了 5 台设备，一切正常。"
    assert model.prompts == []


def test_inspect_graph_uses_llm_summarizer_when_chat_model_injected(inspect_home):
    """图级验证：chat_model 注入后 summarize 节点走 LLM 转写路径。"""
    light_id = _create_device(inspect_home, "客厅灯", "light")
    sensor_id = _create_device(inspect_home, "客厅温湿度传感器", "sensor")
    recent = datetime.now(UTC).isoformat()
    statuses = {
        light_id: {"online": True, "last_seen_at": recent},
        sensor_id: {"online": True, "last_seen_at": recent, "battery": 8},
    }

    async def fetcher(device: dict) -> dict:
        return dict(statuses[device["id"]])

    model = StubSummaryModel(
        "检查了 2 台设备：1 台正常，1 台异常。\n"
        "客厅灯正常在线；客厅温湿度传感器电量 8%，建议尽快更换电池。"
    )
    engine = create_async_engine(get_settings().database_url)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inspect_module, "session_factory", async_sessionmaker(engine))

    async def _run():
        try:
            graph = build_inspect_graph(status_fetcher=fetcher, chat_model=model)
            return await graph.ainvoke({"home_id": inspect_home})
        finally:
            await engine.dispose()

    result = asyncio.run(_run())

    # summarize 节点确实调用了 LLM（模板路径不会产生 prompts）。
    assert len(model.prompts) == 1
    assert "客厅温湿度传感器" in result["summary_text"]
    assert light_id > 0


def test_inspect_graph_end_to_end_with_stubbed_fetcher(inspect_home):
    """骨架流转：加载 → 并行查询 → 检测排序 → 模板转写，状态字段全部落位。"""
    light_id = _create_device(inspect_home, "客厅灯", "light")
    sensor_id = _create_device(inspect_home, "客厅温湿度传感器", "sensor")
    recent = datetime.now(UTC).isoformat()
    statuses = {
        light_id: {"online": True, "last_seen_at": recent},
        sensor_id: {"online": True, "last_seen_at": recent, "battery": 8},
    }

    async def fetcher(device: dict) -> dict:
        return dict(statuses[device["id"]])

    engine = create_async_engine(get_settings().database_url)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inspect_module, "session_factory", async_sessionmaker(engine))

    async def _run():
        try:
            graph = build_inspect_graph(status_fetcher=fetcher)
            return await graph.ainvoke({"home_id": inspect_home})
        finally:
            await engine.dispose()

    result = asyncio.run(_run())

    assert result["total_count"] == 2
    assert result["normal_count"] == 1
    assert [a["rule"] for a in result["anomalies"]] == [rules.RULE_LOW_BATTERY]
    assert result["anomalies"][0]["device_id"] == sensor_id
    assert len(result["raw_statuses"]) == 2
    assert result["summary_text"].startswith("检查了 2 台设备：1 台正常，1 台异常。")
    assert "更换电池" in result["summary_text"]


def test_inspect_graph_all_normal_summary(inspect_home):
    """全部正常时模板输出简洁版，不产生空异常段落。"""
    light_id = _create_device(inspect_home, "卧室灯", "light")
    recent = datetime.now(UTC).isoformat()

    async def fetcher(device: dict) -> dict:
        return {"online": True, "last_seen_at": recent}

    engine = create_async_engine(get_settings().database_url)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inspect_module, "session_factory", async_sessionmaker(engine))

    async def _run():
        try:
            graph = build_inspect_graph(status_fetcher=fetcher)
            return await graph.ainvoke({"home_id": inspect_home})
        finally:
            await engine.dispose()

    result = asyncio.run(_run())

    assert result["anomalies"] == []
    assert result["normal_count"] == 1
    assert result["summary_text"] == "检查了 1 台设备，一切正常。"
    assert light_id > 0  # 设备确已建出（防 fixture 静默失效）


def test_malformed_last_seen_degrades_per_device(inspect_home):
    """畸形时间戳按设备降级为 query_failed，不炸整场巡检（fail-safe 口径）。"""
    light_id = _create_device(inspect_home, "客厅灯", "light")
    sensor_id = _create_device(inspect_home, "客厅温湿度传感器", "sensor")
    statuses = {
        # naive 时间戳：外部数据格式异常的现实形态
        light_id: {"online": True, "last_seen_at": "2026-01-01T08:00:00"},
        sensor_id: {"online": True, "last_seen_at": datetime.now(UTC).isoformat()},
    }

    async def fetcher(device: dict) -> dict:
        return dict(statuses[device["id"]])

    engine = create_async_engine(get_settings().database_url)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inspect_module, "session_factory", async_sessionmaker(engine))

    async def _run():
        try:
            graph = build_inspect_graph(status_fetcher=fetcher)
            return await graph.ainvoke({"home_id": inspect_home})
        finally:
            await engine.dispose()

    result = asyncio.run(_run())

    assert result["total_count"] == 2
    assert result["normal_count"] == 1
    assert [a["rule"] for a in result["anomalies"]] == [rules.RULE_QUERY_FAILED]
    assert result["anomalies"][0]["device_id"] == light_id
    assert "状态查询失败" in result["summary_text"]


def test_stub_fetcher_latency_well_under_timeout():
    """无卡死设备时整体耗时远小于超时阈值（并发而非串行的回归哨兵）。"""
    devices = [{"id": i, "name": f"设备{i}", "device_type": "light"} for i in range(8)]

    async def fetcher(device: dict) -> dict:
        await asyncio.sleep(0.2)
        return {"online": True, "last_seen_at": datetime.now(UTC).isoformat()}

    start = time.perf_counter()
    asyncio.run(query_all_devices(devices, fetcher))
    elapsed = time.perf_counter() - start

    # 串行会是 1.6 秒；并发应 ≈ 0.2 秒。留出余量断言远小于串行耗时。
    assert elapsed < 1.0, f"8 台设备耗时 {elapsed:.2f} 秒，疑似退化为串行"


def test_now_uses_utc_for_detection():
    """检测时间基准为 UTC aware：离线判定与规则表口径一致（回归哨兵）。"""
    now = datetime.now(UTC)
    status = {"online": False, "last_seen_at": (now - timedelta(hours=3)).isoformat()}
    anomalies = rules.detect_anomalies(1, "家庭网关", "gateway", status, now)
    assert anomalies[0]["rule"] == rules.RULE_OFFLINE_TIMEOUT
