"""/inspections/run 接口测试：一键巡检入口（复用巡检子图 + 落库）。"""

from datetime import UTC, datetime

from app.agent.inspect import build_inspect_graph
from app.main import application
from app.routers import inspections as inspections_api


def _override_inspect_graph(fetcher) -> None:
    graph = build_inspect_graph(status_fetcher=fetcher)
    application.dependency_overrides[inspections_api.get_inspect_graph] = lambda: graph


def _release_inspect_graph() -> None:
    application.dependency_overrides.pop(inspections_api.get_inspect_graph, None)


def test_get_inspect_graph_reuses_singleton(monkeypatch):
    """默认巡检依赖复用子图与 LLM 客户端，与 chat 单例口径一致。"""
    original_singleton = inspections_api._inspect_graph_singleton
    inspections_api._inspect_graph_singleton = None
    model = object()
    graph = object()
    built_models = []
    monkeypatch.setattr(inspections_api, "build_chat_model", lambda: model)

    def build_inspect_graph(chat_model):
        built_models.append(chat_model)
        return graph

    monkeypatch.setattr(inspections_api, "build_inspect_graph", build_inspect_graph)
    try:
        assert inspections_api.get_inspect_graph() is graph
        assert inspections_api.get_inspect_graph() is graph
    finally:
        inspections_api._inspect_graph_singleton = original_singleton

    assert built_models == [model]


def test_run_inspection_returns_structured_result_and_persists(client, home_id):
    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅温湿度传感器", "device_type": "sensor"},
    ).json()["id"]
    recent = datetime.now(UTC).isoformat()
    statuses = {
        device_id: {"online": True, "last_seen_at": recent, "battery": 8},
    }

    async def fetcher(device: dict) -> dict:
        return dict(statuses[device["id"]])

    _override_inspect_graph(fetcher)
    try:
        resp = client.post("/inspections/run", json={"home_id": home_id})
    finally:
        _release_inspect_graph()

    assert resp.status_code == 200
    data = resp.json()
    assert data["report_type"] == "inspection"
    assert data["total_devices"] == 1
    assert data["normal_count"] == 0
    assert data["anomalies"][0]["rule"] == "low_battery"
    assert data["report_id"] > 0
    assert "电量 8%" in data["summary_text"]


def test_run_inspection_daily_report_mode(client, home_id):
    client.post(
        f"/homes/{home_id}/devices",
        json={"name": "客厅灯", "device_type": "light"},
    ).json()["id"]
    recent = datetime.now(UTC).isoformat()

    async def fetcher(device: dict) -> dict:
        return {"online": True, "last_seen_at": recent, "device_id": device["id"]}

    _override_inspect_graph(fetcher)
    try:
        resp = client.post(
            "/inspections/run",
            json={"home_id": home_id, "report_type": "daily_report"},
        )
    finally:
        _release_inspect_graph()

    assert resp.status_code == 200
    data = resp.json()
    assert data["report_type"] == "daily_report"
    assert data["summary_text"].startswith("家庭日报：")


def test_run_inspection_unknown_home_returns_404(client):
    async def fetcher(device: dict) -> dict:
        return {"online": True, "last_seen_at": None}

    _override_inspect_graph(fetcher)
    try:
        resp = client.post(
            "/inspections/run",
            json={"home_id": 999999, "report_type": "inspection"},
        )
    finally:
        _release_inspect_graph()

    assert resp.status_code == 404
    assert resp.json()["detail"] == "home not found"
