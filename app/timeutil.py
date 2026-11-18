"""时区工具（ADR-013：全系统时间基准 Asia/Shanghai）。

Windows 默认不带 IANA 时区库（需要 tzdata 包）。这里做**降级**：
拿不到 ZoneInfo 时退回固定 UTC+8 —— 中国全境不使用夏令时，UTC+8 与
Asia/Shanghai 在"自然日"语义上完全等价，不会影响额度重置与趋势分日。
"""
from __future__ import annotations

from datetime import timedelta, timezone, tzinfo

_TZ_NAME = "Asia/Shanghai"
_FALLBACK = timezone(timedelta(hours=8), _TZ_NAME)


def shanghai() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(_TZ_NAME)
    except Exception:                     # pragma: no cover - 取决于运行环境
        return _FALLBACK


def now_local():
    from datetime import datetime

    return datetime.now(shanghai())


def natural_day_string():
    """自然日（YYYY-MM-DD，Asia/Shanghai）—— 额度 quota_date / geo_daily.date 用。"""
    return now_local().strftime("%Y-%m-%d")


def hm_string(dt) -> str:
    """数据库时间 → 「YYYY-MM-DD HH:MM」（页面列表展示格式），空值 → 空串。

    SQLite 的 server_default=CURRENT_TIMESTAMP 落库是 **UTC 裸值**（无 tzinfo），
    统一按 UTC 补齐后再转上海时区；PG timestamptz 自带时区，直接转换。
    """
    if not dt:
        return ""
    if dt.tzinfo is None:
        from datetime import datetime, timezone

        dt = dt.replace(tzinfo=timezone.utc) if isinstance(dt, datetime) else dt
    return dt.astimezone(shanghai()).strftime("%Y-%m-%d %H:%M")
