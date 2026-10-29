"""房间接口测试。"""


def test_create_and_list_rooms(client, home_id) -> None:
    """房间能创建并按家庭查询。"""
    resp = client.post(
        f"/homes/{home_id}/rooms", json={"name": "书房", "room_type": "study"}
    )
    assert resp.status_code == 201
    assert resp.json()["home_id"] == home_id

    rooms = client.get(f"/homes/{home_id}/rooms").json()
    assert len(rooms) == 1
    assert rooms[0]["name"] == "书房"


def test_reject_room_for_missing_home(client) -> None:
    """不存在的家庭不能创建房间。"""
    resp = client.post("/homes/999999999/rooms", json={"name": "ghost"})
    assert resp.status_code == 404
