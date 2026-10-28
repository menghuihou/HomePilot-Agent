"""核心 CRUD 接口的功能测试，对应第 1 周计划第 7 步检查点。"""

from uuid import uuid4

from fastapi.testclient import TestClient


def test_create_home_room_member(client: TestClient, home_id: int) -> None:
    """家庭、房间、成员均能创建。"""
    room = client.post(
        f"/homes/{home_id}/rooms", json={"name": "living-room", "room_type": "living_room"}
    )
    assert room.status_code == 201
    assert room.json()["home_id"] == home_id

    member = client.post(f"/homes/{home_id}/members", json={"name": "hmh", "role": "admin"})
    assert member.status_code == 201

    rooms = client.get(f"/homes/{home_id}/rooms").json()
    members = client.get(f"/homes/{home_id}/members").json()
    assert len(rooms) == 1
    assert len(members) == 1


def test_create_device_with_room(client: TestClient, home_id: int) -> None:
    """设备能绑定家庭内的房间。"""
    room_id = client.post(
        f"/homes/{home_id}/rooms", json={"name": "bedroom"}
    ).json()["id"]

    resp = client.post(
        f"/homes/{home_id}/devices",
        json={"name": f"light-{uuid4().hex[:8]}", "device_type": "light", "room_id": room_id},
    )
    assert resp.status_code == 201
    assert resp.json()["room_id"] == room_id
    assert resp.json()["synthetic"] is True


def test_reject_device_for_missing_home(client: TestClient) -> None:
    """不存在的家庭不能绑定设备。"""
    resp = client.post("/homes/999999999/devices", json={"name": "ghost", "device_type": "light"})
    assert resp.status_code == 404


def test_reject_device_for_missing_room(client: TestClient, home_id: int) -> None:
    """不存在的房间不能绑定设备。"""
    resp = client.post(
        f"/homes/{home_id}/devices",
        json={"name": f"light-{uuid4().hex[:8]}", "device_type": "light", "room_id": 987654321},
    )
    assert resp.status_code == 404


def test_reject_duplicate_device_name_in_same_home(client: TestClient, home_id: int) -> None:
    """同一家庭下设备名必须唯一；不同名称可正常创建。"""
    name = f"light-{uuid4().hex[:8]}"
    assert client.post(
        f"/homes/{home_id}/devices", json={"name": name, "device_type": "light"}
    ).status_code == 201

    resp = client.post(
        f"/homes/{home_id}/devices", json={"name": name, "device_type": "light"}
    )
    assert resp.status_code == 409


def test_device_event_roundtrip(client: TestClient, home_id: int) -> None:
    """设备事件能写入并按设备查询。"""
    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": f"sensor-{uuid4().hex[:8]}", "device_type": "sensor"},
    ).json()["id"]

    resp = client.post(
        f"/devices/{device_id}/events",
        json={"event_type": "power_on", "payload": {"brightness": 80}},
    )
    assert resp.status_code == 201
    assert resp.json()["payload"] == {"brightness": 80}

    events = client.get(f"/devices/{device_id}/events").json()
    assert len(events) == 1


def test_update_device(client: TestClient, home_id: int) -> None:
    """更新设备信息；改名仍需满足家庭内唯一。"""
    name_a = f"light-{uuid4().hex[:8]}"
    name_b = f"light-{uuid4().hex[:8]}"
    first = client.post(
        f"/homes/{home_id}/devices", json={"name": name_a, "device_type": "light"}
    ).json()["id"]
    client.post(f"/homes/{home_id}/devices", json={"name": name_b, "device_type": "light"})

    resp = client.patch(f"/devices/{first}", json={"name": name_b})
    assert resp.status_code == 409

    renamed = f"light-{uuid4().hex[:8]}"
    resp = client.patch(f"/devices/{first}", json={"name": renamed, "vendor": "acme"})
    assert resp.status_code == 200
    assert resp.json()["name"] == renamed
    assert resp.json()["vendor"] == "acme"


def test_delete_device(client: TestClient, home_id: int) -> None:
    """删除设备后详情 404，关联事件一并删除。"""
    device_id = client.post(
        f"/homes/{home_id}/devices",
        json={"name": f"plug-{uuid4().hex[:8]}", "device_type": "plug"},
    ).json()["id"]
    client.post(f"/devices/{device_id}/events", json={"event_type": "heartbeat"})

    assert client.delete(f"/devices/{device_id}").status_code == 204
    assert client.get(f"/devices/{device_id}").status_code == 404
    assert client.get(f"/devices/{device_id}/events").status_code == 404
