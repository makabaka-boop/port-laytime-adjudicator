"""作业/暂停区间的裁剪、合并与计费计算（纯函数，无 IO）。

所有区间均为左闭右开 ``[start, end)``，端点必须是带 UTC 时区的 ``datetime``，
且 ``end > start``。暂停区间先裁剪到作业区间，再把重叠或首尾相接的部分合并；
端点相等（首尾相接）本身不产生重复扣减，也不额外增加时长。

租约可约定免计滞期的允许作业时长：先裁剪、合并暂停得到净作业秒数，再从净作业
秒数中扣除不超过它的允许秒数，剩余部分作为可计费秒数向上取整计费。扣除顺序
不可颠倒——允许时长针对的是净作业时长，而非含暂停的毛时长。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


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
    allowed_seconds: int
    allowed_seconds_used: int
    billable_seconds: int
    billable_hours: int
    rate_cents_per_hour: int
    total_cents: int


def calculate(
    work_start: datetime,
    work_end: datetime,
    raw_pauses: list[tuple[datetime, datetime]],
    rate_cents_per_hour: int,
    allowed_seconds: int = 0,
) -> Calculation:
    """执行完整结算计算。任何不合法输入都会抛出异常，调用方不得写库。

    先裁剪、合并暂停并得到净作业秒数（work − paused），再扣除允许秒数；
    允许扣减以净作业秒数为上限（``allowed_seconds_used`` 记录实际扣减值），
    余额向上取整计费。
    """
    validate_interval(work_start, work_end, "作业")
    if not isinstance(rate_cents_per_hour, int) or rate_cents_per_hour < 0:
        raise IntervalError("费率必须是非负整数（分/小时）")
    if (
        not isinstance(allowed_seconds, int)
        or isinstance(allowed_seconds, bool)
        or allowed_seconds < 0
    ):
        raise IntervalError("允许秒数必须是非负整数")

    clipped: list[tuple[datetime, datetime]] = []
    for pause_start, pause_end in raw_pauses:
        intersection = clip_to_work(pause_start, pause_end, work_start, work_end)
        if intersection is not None:
            clipped.append(intersection)

    merged = merge_intervals(clipped)
    work_seconds = int((work_end - work_start).total_seconds())
    paused_seconds = total_seconds(merged)
    net_seconds = work_seconds - paused_seconds
    if net_seconds < 0:
        # 理论上裁剪/合并后不可能发生，保留作为防御性不变量。
        raise IntervalError("净作业秒数不能为负")
    # 先合并暂停、再扣允许时长：允许扣减以净作业秒数为上限，余额不可为负。
    allowed_used = min(allowed_seconds, net_seconds)
    billable_seconds = net_seconds - allowed_used
    hours = billable_hours(billable_seconds)
    total = hours * rate_cents_per_hour

    return Calculation(
        work_start=work_start,
        work_end=work_end,
        pauses_merged=merged,
        work_seconds=work_seconds,
        paused_seconds=paused_seconds,
        allowed_seconds=allowed_seconds,
        allowed_seconds_used=allowed_used,
        billable_seconds=billable_seconds,
        billable_hours=hours,
        rate_cents_per_hour=rate_cents_per_hour,
        total_cents=total,
    )


# 时间线分段类别：暂停 / 允许抵扣 / 计费。
CATEGORY_PAUSE = "pause"
CATEGORY_ALLOWED = "allowed"
CATEGORY_BILLABLE = "billable"


@dataclass(frozen=True)
class Segment:
    """计费时间线分段：左闭右开区间加一个类别，时长恒为正。"""

    start: datetime
    end: datetime
    category: str

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


def build_timeline(
    work_start: datetime,
    work_end: datetime,
    pauses_merged: list[tuple[datetime, datetime]],
    allowed_seconds_used: int,
) -> list[Segment]:
    """由作业边界与已合并暂停扫描出覆盖整个作业区间的连续分段（纯函数）。

    以作业起止与全部暂停端点为切点做边界扫描，相邻切点成段，天然首尾
    相接且无遗漏：先标记暂停段，再按时间先后把实际允许秒数消耗在非暂停
    段上（不足整段时在段内精确切分），剩余部分标记为计费。

    持久化快照不自洽（暂停倒置/越界/互相重叠、实际抵扣秒数无法被非暂停
    区间耗尽等）时抛出 ``IntervalError``，调用方据此判定数据不一致。
    """
    validate_interval(work_start, work_end, "作业")
    if (
        not isinstance(allowed_seconds_used, int)
        or isinstance(allowed_seconds_used, bool)
        or allowed_seconds_used < 0
    ):
        raise IntervalError("实际抵扣秒数必须是非负整数")

    pauses = sorted(pauses_merged, key=lambda item: (item[0], item[1]))
    previous_end: datetime | None = None
    for pause_start, pause_end in pauses:
        validate_interval(pause_start, pause_end, "合并暂停")
        if pause_start < work_start or pause_end > work_end:
            raise IntervalError("合并暂停超出作业区间")
        if previous_end is not None and pause_start < previous_end:
            raise IntervalError("合并暂停互相重叠，不是已合并快照")
        previous_end = pause_end

    # 边界扫描：切点排序去重后相邻成段，必然首尾相接且覆盖整个作业区间。
    boundaries = {work_start, work_end}
    for pause_start, pause_end in pauses:
        boundaries.add(pause_start)
        boundaries.add(pause_end)
    ordered = sorted(boundaries)

    segments: list[Segment] = []
    remaining = allowed_seconds_used
    for left, right in zip(ordered, ordered[1:]):
        if any(s <= left and right <= e for s, e in pauses):
            segments.append(Segment(left, right, CATEGORY_PAUSE))
            continue
        span = int((right - left).total_seconds())
        used = min(remaining, span)
        remaining -= used
        if used <= 0:
            segments.append(Segment(left, right, CATEGORY_BILLABLE))
            continue
        split = left + timedelta(seconds=used)
        segments.append(Segment(left, split, CATEGORY_ALLOWED))
        if split < right:  # 段内精确切分：余额部分计费，不产生零长度段
            segments.append(Segment(split, right, CATEGORY_BILLABLE))

    if remaining > 0:
        raise IntervalError("实际抵扣秒数超过非暂停时长，无法完整消耗")
    return segments
