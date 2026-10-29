"""家庭与成员接口测试。"""

from uuid import uuid4


def test_create_and_list_homes(client) -> None:
    """创建家庭并出现在列表中，演示标记默认生效。"""
    name = f"test-home-{uuid4().hex[:8]}"
    resp = client.post("/homes", json={"name": name, "address": "测试路 1 号"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == name
    assert body["synthetic"] is True
    assert body["source_type"] == "synthetic"

    homes = client.get("/homes").json()
    assert any(h["id"] == body["id"] for h in homes)


def test_create_and_list_members(client, home_id) -> None:
    """成员能挂到指定家庭下并查询。"""
    resp = client.post(
        f"/homes/{home_id}/members", json={"name": "测试成员", "role": "admin"}
    )
    assert resp.status_code == 201
    assert resp.json()["home_id"] == home_id

    members = client.get(f"/homes/{home_id}/members").json()
    assert len(members) == 1
    assert members[0]["name"] == "测试成员"
