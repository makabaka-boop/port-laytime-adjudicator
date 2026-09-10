"""作业/暂停区间的裁剪、合并与计费计算（纯函数，无 IO）。

所有区间均为左闭右开 ``[start, end)``，端点必须是带 UTC 时区的 ``datetime``，
且 ``end > start``。暂停区间先裁剪到作业区间，再把重叠或首尾相接的部分合并；
端点相等（首尾相接）本身不产生重复扣减，也不额外增加时长。
"""

from dataclasses import dataclass
from datetime import datetime


class IntervalError(ValueError):
    """区间不合法（结束不晚于开始等），绝不允许进入持久化。"""


def validate_interval(start: datetime, end: datetime, what: str) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise IntervalError(f"{what}区间必须带时区")
    if end <= start:
        raise IntervalError(f"{what}区间结束必须晚于开始")


def clip_to_work(
    pause_start: datetime,
    pause_end: datetime,
    work_start: datetime,
    work_end: datetime,
) -> tuple[datetime, datetime] | None:
    """把单个暂停区间裁剪到作业区间内，交集为空（含仅端点相接）时返回 ``None``。"""
    validate_interval(pause_start, pause_end, "暂停")
    start = max(pause_start, work_start)
    end = min(pause_end, work_end)
    # 左闭右开：start == end 时交集时长为 0，不应产生暂停。
    if start >= end:
        return None
    return start, end


def merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """合并重叠或首尾相接的区间。端点相等会合并但不增加总时长。"""
    if not intervals:
        return []

    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # 相等即首尾相接，同样合并
            if end > last_end:
                merged[-1] = (last_start, end)
            # 被完全包含时保持不变，重复覆盖不会重复扣减。
        else:
            merged.append((start, end))
    return merged


def total_seconds(intervals: list[tuple[datetime, datetime]]) -> int:
    return int(sum((end - start).total_seconds() for start, end in intervals))


def billable_hours(seconds: int) -> int:
    """不足一小时向上取整；零秒为零小时。"""
    if seconds <= 0:
        return 0
    return (seconds + 3599) // 3600


@dataclass(frozen=True)
class Calculation:
    work_start: datetime
    work_end: datetime
    pauses_merged: list[tuple[datetime, datetime]]
    work_seconds: int
    paused_seconds: int
    billable_seconds: int
    billable_hours: int
    rate_cents_per_hour: int
    total_cents: int


def calculate(
    work_start: datetime,
    work_end: datetime,
    raw_pauses: list[tuple[datetime, datetime]],
    rate_cents_per_hour: int,
) -> Calculation:
    """执行完整结算计算。任何不合法输入都会抛出异常，调用方不得写库。"""
    validate_interval(work_start, work_end, "作业")
    if not isinstance(rate_cents_per_hour, int) or rate_cents_per_hour < 0:
        raise IntervalError("费率必须是非负整数（分/小时）")

    clipped: list[tuple[datetime, datetime]] = []
    for pause_start, pause_end in raw_pauses:
        intersection = clip_to_work(pause_start, pause_end, work_start, work_end)
        if intersection is not None:
            clipped.append(intersection)

    merged = merge_intervals(clipped)
    work_seconds = int((work_end - work_start).total_seconds())
    paused_seconds = total_seconds(merged)
    billable_seconds = work_seconds - paused_seconds
    if billable_seconds < 0:
        # 理论上裁剪/合并后不可能发生，保留作为防御性不变量。
        raise IntervalError("可计费秒数不能为负")
    hours = billable_hours(billable_seconds)
    total = hours * rate_cents_per_hour

    return Calculation(
        work_start=work_start,
        work_end=work_end,
        pauses_merged=merged,
        work_seconds=work_seconds,
        paused_seconds=paused_seconds,
        billable_seconds=billable_seconds,
        billable_hours=hours,
        rate_cents_per_hour=rate_cents_per_hour,
        total_cents=total,
    )
