"""健康检查接口测试。"""


def test_health(client) -> None:
    """GET /health 返回 200 与固定状态体。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "env": "local"}
