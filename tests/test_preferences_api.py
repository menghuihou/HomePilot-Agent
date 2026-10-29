"""preferences 接口功能测试：upsert 覆盖语义、外键校验与家庭隔离。"""


def test_upsert_rejects_member_from_other_home(client, home_id, other_home_id):
    other_member_id = client.post(
        f"/homes/{other_home_id}/members", json={"name": "隔壁成员"}
    ).json()["id"]

    resp = client.put(
        f"/homes/{home_id}/preferences",
        json={
            "member_id": other_member_id,
            "pref_key": "home_mode",
            "pref_value": "normal",
        },
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "member not found in this home"


def test_upsert_creates_preference(client, home_id):
    resp = client.put(
        f"/homes/{home_id}/preferences",
        json={"pref_key": "device_alias:1", "pref_value": "主灯"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["home_id"] == home_id
    assert body["member_id"] is None
    assert body["pref_key"] == "device_alias:1"
    assert body["pref_value"] == "主灯"


def test_upsert_overwrites_same_key(client, home_id):
    url = f"/homes/{home_id}/preferences"
    client.put(url, json={"pref_key": "device_alias:1", "pref_value": "主灯"})

    resp = client.put(
        url, json={"pref_key": "device_alias:1", "pref_value": "客厅主灯"}
    )

    assert resp.status_code == 200, resp.text
    listing = client.get(url).json()
    assert len(listing) == 1
    assert listing[0]["pref_value"] == "客厅主灯"


def test_home_level_and_member_level_keys_coexist(client, home_id):
    member_resp = client.post(f"/homes/{home_id}/members", json={"name": "测试成员"})
    member_id = member_resp.json()["id"]
    url = f"/homes/{home_id}/preferences"

    client.put(url, json={"pref_key": "home_mode", "pref_value": "normal"})
    resp = client.put(
        url,
        json={
            "member_id": member_id,
            "pref_key": "home_mode",
            "pref_value": "sleep",
        },
    )

    assert resp.status_code == 200, resp.text
    listing = client.get(url).json()
    assert len(listing) == 2


def test_upsert_rejects_unknown_home(client):
    resp = client.put(
        "/homes/999999/preferences",
        json={"pref_key": "home_mode", "pref_value": "normal"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "home not found"
