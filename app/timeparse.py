"""严格的 RFC 3339 时间解析。

只接受 UTC（Z 或零偏移）且精确到整秒的时间戳：

    2026-09-10T08:00:00Z
    2026-09-10T08:00:00+00:00
    2026-09-10T08:00:00-00:00

拒绝：小数秒、非零偏移、空格分隔、无时区、多余字符等。
"""

import re
from datetime import datetime, timedelta, timezone

# 整秒、零偏移。日期/时间的数值合法性交由 datetime 再次校验。
_RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"T"
    r"(\d{2}):(\d{2}):(\d{2})"
    r"(Z|[+-]\d{2}:\d{2})$"
)

# Pydantic 的 field validator 需要把 ValueError 关联到具体字段名。
_TIME_FORMAT_MESSAGE = (
    "时间必须是精确到整秒的 RFC 3339 UTC 时间戳，"
    "例如 2026-09-10T08:00:00Z；不允许小数秒"
)
_UTC_ONLY_MESSAGE = "只接受 UTC 时间（Z 或 ±00:00 偏移），禁止非零时区偏移"


def parse_utc_second(value: object) -> datetime:
    """把字符串解析为 UTC ``datetime``，任何不合法输入都抛出 ``ValueError``。"""
    if not isinstance(value, str):
        raise ValueError(_TIME_FORMAT_MESSAGE)

    match = _RFC3339_RE.match(value)
    if match is None:
        raise ValueError(_TIME_FORMAT_MESSAGE)

    year, month, day, hour, minute, second, zone = match.groups()
    try:
        naive = datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second),
        )
    except ValueError as exc:  # 例如月份 13、日期 32
        raise ValueError(_TIME_FORMAT_MESSAGE) from exc

    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        offset = sign * (int(zone[1:3]) * 60 + int(zone[4:6]))
        if offset != 0:
            raise ValueError(_UTC_ONLY_MESSAGE)

    return naive.replace(tzinfo=timezone.utc)


def format_utc_second(value: datetime) -> str:
    """把 UTC ``datetime`` 规范化为 ``...Z`` 形式（整秒）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
