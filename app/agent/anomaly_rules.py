"""巡检异常检测规则表：纯确定性函数，零 LLM。

规则表版本化在 repo（本文件 + git 历史）——异常判定是巡检链路的安全关键
口径，不读 MCP 远端 metadata；LLM 只负责把本模块输出的异常列表转写成
可读摘要，无权新增、隐瞒或改判异常项。

规则口径（与第 4 周实施计划第 6 节一致）：

| 规则 | 触发条件 | severity |
| --- | --- | --- |
| low_battery | battery < LOW_BATTERY_THRESHOLD（battery 缺失时跳过） | medium |
| offline_timeout | 离线且距 last_seen_at 超过 OFFLINE_TIMEOUT | high |
| offline_brief | 离线且离线时长不超过 OFFLINE_TIMEOUT | low |
| sensor_stale | sensor 在线但超过 SENSOR_STALE 未上报数据 | medium |
| never_reported | 离线且从未上报过事件（last_seen_at 缺失） | medium |
| query_failed | 状态查询失败 / 结构不完整（fail-safe 兜底） | medium |

时间口径：now 与 last_seen_at 一律 UTC aware datetime（MCP 层输出
ISO 8601 字符串，由 _parse_last_seen 解析）；naive 输入视为编程错误
直接抛出，避免静默错判。数据停滞规则仅覆盖 sensor 域，灯光 / 温控的
停滞判定留待按域扩展（当前刻意不做，避免误报）。
"""

from datetime import datetime, timedelta

LOW_BATTERY_THRESHOLD = 20
OFFLINE_TIMEOUT = timedelta(hours=2)
SENSOR_STALE = timedelta(hours=6)

OFFLINE_TIMEOUT_HOURS = int(OFFLINE_TIMEOUT.total_seconds() // 3600)
SENSOR_STALE_HOURS = int(SENSOR_STALE.total_seconds() // 3600)

RULE_LOW_BATTERY = "low_battery"
RULE_OFFLINE_TIMEOUT = "offline_timeout"
RULE_OFFLINE_BRIEF = "offline_brief"
RULE_SENSOR_STALE = "sensor_stale"
RULE_NEVER_REPORTED = "never_reported"
RULE_QUERY_FAILED = "query_failed"

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# 规则中文名：汇总排序的分布行与 LLM 转写提示词共用，与规则键一一对应。
RULE_LABELS = {
    RULE_LOW_BATTERY: "低电量",
    RULE_OFFLINE_TIMEOUT: "离线超时",
    RULE_OFFLINE_BRIEF: "离线短暂",
    RULE_SENSOR_STALE: "数据停滞",
    RULE_NEVER_REPORTED: "从未上报",
    RULE_QUERY_FAILED: "查询失败",
}


def _anomaly(device_id: int, name: str, rule: str, severity: str, detail: str) -> dict:
    """构造统一结构的异常项，字段口径与 inspection_reports.anomalies 一致。"""
    return {
        "device_id": device_id,
        "device_name": name,
        "rule": rule,
        "severity": severity,
        "detail": detail,
    }


def mark_query_failed(device_id: int, name: str, reason: str) -> dict:
    """查询失败的设备统一走本入口产出异常项（宁可标记不可漏报）。"""
    return _anomaly(
        device_id,
        name,
        RULE_QUERY_FAILED,
        SEVERITY_MEDIUM,
        f"{name}状态查询失败：{reason}，可能需要检查设备连接",
    )


def _parse_last_seen(status: dict) -> datetime | None:
    """解析 MCP 返回的 last_seen_at（ISO 8601 字符串）；缺失返回 None。"""
    raw = status.get("last_seen_at")
    if raw is None:
        return None
    parsed = datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None:
        raise ValueError(f"last_seen_at 必须是带时区的 ISO 8601 字符串：{raw!r}")
    return parsed


def detect_anomalies(
    device_id: int,
    name: str,
    device_type: str,
    status: dict,
    now: datetime,
) -> list[dict]:
    """确定性异常检测：返回异常项列表（可多条），正常返回空列表。

    输入 status 为 MCP get_device_status 的返回结构；含 error 键、缺少
    online 字段或在线却无上报时间等不完整状态，一律按 query_failed 兜底
    （未知状态宁可标记不可漏报，绝不静默放行）。
    """
    if now.tzinfo is None:
        raise ValueError("now 必须是 aware datetime（UTC）")

    if "error" in status:
        return [mark_query_failed(device_id, name, str(status["error"]))]

    online = status.get("online")
    if not isinstance(online, bool):
        return [mark_query_failed(device_id, name, "状态结构不完整：缺少 online 字段")]

    last_seen = _parse_last_seen(status)

    if not online:
        if last_seen is None:
            return [
                _anomaly(
                    device_id,
                    name,
                    RULE_NEVER_REPORTED,
                    SEVERITY_MEDIUM,
                    f"{name}从未上报过状态数据，建议检查设备接入配置",
                )
            ]
        age = now - last_seen
        if age > OFFLINE_TIMEOUT:
            hours = round(age.total_seconds() / 3600, 1)
            return [
                _anomaly(
                    device_id,
                    name,
                    RULE_OFFLINE_TIMEOUT,
                    SEVERITY_HIGH,
                    f"{name}已离线 {hours} 小时（超过阈值 "
                    f"{OFFLINE_TIMEOUT_HOURS} 小时），最后在线 {last_seen.isoformat()}，"
                    "建议检查网络或网关",
                )
            ]
        return [
            _anomaly(
                device_id,
                name,
                RULE_OFFLINE_BRIEF,
                SEVERITY_LOW,
                f"{name}当前离线，可能网络波动",
            )
        ]

    if last_seen is None:
        return [
            mark_query_failed(device_id, name, "状态结构不完整：在线但缺少最近上报时间")
        ]

    # 在线设备只查低电量与传感器数据停滞两类规则，判定顺序固定
    # （与规则表顺序一致），保证输出列表确定性、测试可精确断言。
    anomalies: list[dict] = []
    battery = status.get("battery")
    if battery is not None and battery < LOW_BATTERY_THRESHOLD:
        anomalies.append(
            _anomaly(
                device_id,
                name,
                RULE_LOW_BATTERY,
                SEVERITY_MEDIUM,
                f"{name}电量 {battery}%（低于阈值 {LOW_BATTERY_THRESHOLD}%），"
                "建议尽快更换电池",
            )
        )
    if device_type == "sensor" and now - last_seen > SENSOR_STALE:
        anomalies.append(
            _anomaly(
                device_id,
                name,
                RULE_SENSOR_STALE,
                SEVERITY_MEDIUM,
                f"{name}超过 {SENSOR_STALE_HOURS} 小时未上报数据，建议检查设备",
            )
        )
    return anomalies
