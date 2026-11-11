"""异常检测规则表测试：每条规则、边界值与 fail-safe 口径（第 4 步检查点）。

纯函数测试，不依赖数据库与 LLM。
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.agent import anomaly_rules as rules

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


def _status(**overrides) -> dict:
    """构造「在线且 5 分钟前刚上报」的正常设备状态基线，按需覆盖字段。"""
    status: dict = {
        "online": True,
        "last_seen_at": (NOW - timedelta(minutes=5)).isoformat(),
    }
    status.update(overrides)
    return status


def test_healthy_online_device_reports_no_anomaly():
    assert rules.detect_anomalies(1, "客厅灯", "light", _status(), NOW) == []


@pytest.mark.parametrize(
    ("battery", "expected_rules"),
    [
        (20, []),  # 阈值边界：battery=20 不报（规则是 < 20）
        (19, [rules.RULE_LOW_BATTERY]),  # 19 报
        (0, [rules.RULE_LOW_BATTERY]),
        (100, []),
    ],
)
def test_low_battery_boundary(battery, expected_rules):
    status = _status(battery=battery)
    anomalies = rules.detect_anomalies(2134, "客厅温湿度传感器", "sensor", status, NOW)
    assert [a["rule"] for a in anomalies] == expected_rules


def test_low_battery_applies_to_any_domain_reporting_battery():
    """低电量判定以 battery 字段为准，不限定设备域。"""
    status = _status(battery=10)
    anomalies = rules.detect_anomalies(1, "带电量上报的灯", "light", status, NOW)
    assert [a["rule"] for a in anomalies] == [rules.RULE_LOW_BATTERY]


def test_sensor_without_battery_skips_low_battery_rule():
    """电量未上报的传感器不得误报低电量（传感器可能不支持电量上报）。"""
    status = _status()  # 不带 battery 字段
    assert rules.detect_anomalies(2135, "卧室人体传感器", "sensor", status, NOW) == []


@pytest.mark.parametrize(
    ("offline_age", "expected_rule", "expected_severity"),
    [
        # 阈值边界：恰好 2 小时不算超时（规则是 > 2 小时）
        (timedelta(hours=2), rules.RULE_OFFLINE_BRIEF, rules.SEVERITY_LOW),
        # 超一点就算超时
        (
            timedelta(hours=2, seconds=1),
            rules.RULE_OFFLINE_TIMEOUT,
            rules.SEVERITY_HIGH,
        ),
        (timedelta(minutes=30), rules.RULE_OFFLINE_BRIEF, rules.SEVERITY_LOW),
        (timedelta(hours=3), rules.RULE_OFFLINE_TIMEOUT, rules.SEVERITY_HIGH),
    ],
)
def test_offline_boundary(offline_age, expected_rule, expected_severity):
    status = {"online": False, "last_seen_at": (NOW - offline_age).isoformat()}
    anomalies = rules.detect_anomalies(19, "家庭网关", "gateway", status, NOW)
    assert len(anomalies) == 1
    assert anomalies[0]["rule"] == expected_rule
    assert anomalies[0]["severity"] == expected_severity


def test_never_reported_offline_device_flags_medium():
    """从未上报过事件的离线设备无法计算离线时长，单独规则兜底。"""
    status = {"online": False, "last_seen_at": None}
    anomalies = rules.detect_anomalies(21, "阳台摄像头", "sensor", status, NOW)
    assert len(anomalies) == 1
    assert anomalies[0]["rule"] == rules.RULE_NEVER_REPORTED
    assert anomalies[0]["severity"] == rules.SEVERITY_MEDIUM


@pytest.mark.parametrize(
    ("stale_age", "expected_rules"),
    [
        (timedelta(hours=6), []),  # 阈值边界：恰好 6 小时不报
        (timedelta(hours=6, seconds=1), [rules.RULE_SENSOR_STALE]),
    ],
)
def test_sensor_stale_boundary(stale_age, expected_rules):
    status = _status(last_seen_at=(NOW - stale_age).isoformat())
    anomalies = rules.detect_anomalies(2134, "客厅温湿度传感器", "sensor", status, NOW)
    assert [a["rule"] for a in anomalies] == expected_rules


def test_non_sensor_online_stale_not_flagged():
    """数据停滞规则本周仅覆盖 sensor 域，其他在线域不误报。"""
    status = _status(last_seen_at=(NOW - timedelta(hours=7)).isoformat())
    assert rules.detect_anomalies(18, "卧室空调", "thermostat", status, NOW) == []


def test_sensor_online_old_battery_event_reports_both_in_fixed_order():
    """多条异常可并存，输出顺序与规则表顺序一致（先低电量后停滞）。"""
    status = _status(last_seen_at=(NOW - timedelta(hours=7)).isoformat(), battery=8)
    anomalies = rules.detect_anomalies(2134, "客厅温湿度传感器", "sensor", status, NOW)
    assert [a["rule"] for a in anomalies] == [
        rules.RULE_LOW_BATTERY,
        rules.RULE_SENSOR_STALE,
    ]


def test_error_status_falls_back_to_query_failed():
    status = {"error": "设备不存在：999999"}
    anomalies = rules.detect_anomalies(999999, "幽灵设备", "light", status, NOW)
    assert [a["rule"] for a in anomalies] == [rules.RULE_QUERY_FAILED]
    assert "设备不存在" in anomalies[0]["detail"]


def test_missing_online_field_falls_back_to_query_failed():
    """未知状态宁可标记不可漏报：缺 online 字段按查询失败兜底。"""
    status = {"last_seen_at": NOW.isoformat()}
    anomalies = rules.detect_anomalies(1, "客厅灯", "light", status, NOW)
    assert [a["rule"] for a in anomalies] == [rules.RULE_QUERY_FAILED]


def test_online_without_last_seen_falls_back_to_query_failed():
    """在线却无最近上报时间属于契约违反，按查询失败兜底。"""
    status = {"online": True, "last_seen_at": None}
    anomalies = rules.detect_anomalies(1, "客厅灯", "light", status, NOW)
    assert [a["rule"] for a in anomalies] == [rules.RULE_QUERY_FAILED]


def test_mark_query_failed_returns_anomaly():
    anomaly = rules.mark_query_failed(19, "家庭网关", "timeout after 5s")
    assert anomaly == {
        "device_id": 19,
        "device_name": "家庭网关",
        "rule": rules.RULE_QUERY_FAILED,
        "severity": rules.SEVERITY_MEDIUM,
        "detail": "家庭网关状态查询失败：timeout after 5s，可能需要检查设备连接",
    }


def test_anomaly_structure_matches_report_schema():
    """异常项五字段口径与 inspection_reports.anomalies 约定一致。"""
    (anomaly,) = rules.detect_anomalies(
        2134, "客厅温湿度传感器", "sensor", _status(battery=8), NOW
    )
    assert set(anomaly) == {"device_id", "device_name", "rule", "severity", "detail"}
    assert anomaly["device_id"] == 2134
    assert anomaly["device_name"] == "客厅温湿度传感器"


def test_naive_datetime_rejected():
    """naive 时间是编程错误，直接抛出不静默错判。"""
    naive_now = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="aware datetime"):
        rules.detect_anomalies(1, "客厅灯", "light", _status(), naive_now)

    status = _status(last_seen_at=NOW.replace(tzinfo=None).isoformat())
    with pytest.raises(ValueError, match="带时区的 ISO 8601"):
        rules.detect_anomalies(1, "客厅灯", "light", status, NOW)


def test_every_rule_has_chinese_label():
    """规则中文名与规则键一一对应（分布行与 LLM 提示词共用）。"""
    assert set(rules.RULE_LABELS) == {
        rules.RULE_LOW_BATTERY,
        rules.RULE_OFFLINE_TIMEOUT,
        rules.RULE_OFFLINE_BRIEF,
        rules.RULE_NEVER_REPORTED,
        rules.RULE_SENSOR_STALE,
        rules.RULE_QUERY_FAILED,
    }
