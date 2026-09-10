from datetime import timezone

import pytest

from app.timeparse import format_utc_second, parse_utc_second

VALID = [
    "2026-09-10T08:00:00Z",
    "2026-09-10T08:00:00+00:00",
    "2026-09-10T08:00:00-00:00",
]


@pytest.mark.parametrize("text", VALID)
def test_valid_utc_whole_seconds(text):
    dt = parse_utc_second(text)
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026 and dt.second == 0
    assert format_utc_second(dt) == "2026-09-10T08:00:00Z"


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-10T08:00:00.000Z",      # 小数秒
        "2026-09-10T08:00:00.5Z",
        "2026-09-10 08:00:00Z",          # 空格分隔
        "2026-09-10T08:00:00",           # 无时区
        "2026-09-10T08:00:00+08:00",     # 非零偏移
        "2026-09-10T08:00:00-05:30",
        "2026-09-10T08:00:00z",          # 小写 z
        "2026-09-10T8:00:00Z",           # 非两位小时
        "2026-09-10T08:00Z",             # 缺秒
        "2026-13-10T08:00:00Z",          # 非法月份
        "2026-02-30T08:00:00Z",          # 非法日期
        "2026-09-10T08:00:00Z ",         # 多余字符
        "",
        None,
    ],
)
def test_rejected(text):
    with pytest.raises(ValueError):
        parse_utc_second(text)
