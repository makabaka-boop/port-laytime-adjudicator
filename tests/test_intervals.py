from datetime import datetime, timedelta, timezone

import pytest

from app.intervals import (
    IntervalError,
    billable_hours,
    build_timeline,
    calculate,
    clip_to_work,
    merge_intervals,
    total_seconds,
)

UTC = timezone.utc


def t(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 10, hour, minute, tzinfo=UTC)


def test_merge_overlap_and_touch_only_counted_once():
    intervals = [
        (t(1), t(3)),
        (t(2), t(4)),
        (t(2, 30), t(3, 30)),   # 完全包含
        (t(4), t(5)),           # 首尾相接
    ]
    merged = merge_intervals(intervals)
    assert merged == [(t(1), t(5))]
    assert total_seconds(merged) == 4 * 3600


def test_merge_keeps_gap_separate():
    merged = merge_intervals([(t(1), t(2)), (t(3), t(4))])
    assert merged == [(t(1), t(2)), (t(3), t(4))]
    assert total_seconds(merged) == 2 * 3600


def test_clip_cross_boundary():
    work = (t(8), t(9))
    assert clip_to_work(t(7), t(8, 30), *work) == (t(8), t(8, 30))
    assert clip_to_work(t(8, 45), t(10), *work) == (t(8, 45), t(9))
    # 完全在外 / 仅端点相接 -> None
    assert clip_to_work(t(10), t(11), *work) is None
    assert clip_to_work(t(7), t(8), *work) is None
    assert clip_to_work(t(9), t(10), *work) is None


def test_billable_hours_rounding():
    assert billable_hours(0) == 0
    assert billable_hours(1) == 1
    assert billable_hours(3599) == 1
    assert billable_hours(3600) == 1
    assert billable_hours(3601) == 2
    assert billable_hours(7200) == 2


def test_calculate_overlap_and_boundary():
    result = calculate(
        work_start=t(0), work_end=t(8),
        raw_pauses=[
            (t(1), t(3)),
            (t(2), t(4)),
            (t(2, 30), t(3, 30)),
            (t(4), t(5)),
        ],
        rate_cents_per_hour=100,
    )
    assert result.pauses_merged == [(t(1), t(5))]
    assert result.work_seconds == 8 * 3600
    assert result.paused_seconds == 4 * 3600
    assert result.billable_seconds == 4 * 3600
    assert result.billable_hours == 4
    assert result.total_cents == 400


def test_calculate_zero_seconds_is_zero_fee():
    result = calculate(
        work_start=t(0), work_end=t(1),
        raw_pauses=[(t(0), t(1))],
        rate_cents_per_hour=999,
    )
    assert result.billable_seconds == 0
    assert result.billable_hours == 0
    assert result.total_cents == 0


def test_calculate_no_pauses_rounds_up_one_hour():
    result = calculate(
        work_start=t(0), work_end=t(0) + timedelta(seconds=1),
        raw_pauses=[],
        rate_cents_per_hour=100,
    )
    assert result.billable_seconds == 1
    assert result.billable_hours == 1
    assert result.total_cents == 100


def test_invalid_work_interval_rejected():
    with pytest.raises(IntervalError):
        calculate(t(1), t(1), [], 100)
    with pytest.raises(IntervalError):
        calculate(t(2), t(1), [], 100)


def test_invalid_pause_or_rate_rejected():
    with pytest.raises(IntervalError):
        calculate(t(0), t(2), [(t(1, 30), t(1))], 100)
    with pytest.raises(IntervalError):
        calculate(t(0), t(2), [], -1)


def test_allowed_seconds_less_than_net_only_balance_billed():
    # 作业 4 小时，暂停合并后 1 小时 -> 净作业 3 小时（10800 秒）；
    # 允许 1 小时 -> 仅对余额 2 小时计费。
    result = calculate(
        work_start=t(0), work_end=t(4),
        raw_pauses=[(t(1), t(2))],
        rate_cents_per_hour=100,
        allowed_seconds=3600,
    )
    assert result.work_seconds == 4 * 3600
    assert result.paused_seconds == 3600
    assert result.allowed_seconds == 3600
    assert result.allowed_seconds_used == 3600
    assert result.billable_seconds == 2 * 3600
    assert result.billable_hours == 2
    assert result.total_cents == 200


def test_allowed_seconds_equals_net_is_zero_fee():
    result = calculate(
        work_start=t(0), work_end=t(4),
        raw_pauses=[(t(1), t(2))],
        rate_cents_per_hour=100,
        allowed_seconds=3 * 3600,
    )
    assert result.allowed_seconds_used == 3 * 3600
    assert result.billable_seconds == 0
    assert result.billable_hours == 0
    assert result.total_cents == 0


def test_allowed_seconds_exceeds_net_is_capped_and_zero_fee():
    # 约定值超过净作业时长时，实际扣减以净作业为上限，费用为零。
    result = calculate(
        work_start=t(0), work_end=t(4),
        raw_pauses=[(t(1), t(2))],
        rate_cents_per_hour=100,
        allowed_seconds=99 * 3600,
    )
    assert result.allowed_seconds == 99 * 3600
    assert result.allowed_seconds_used == 3 * 3600
    assert result.billable_seconds == 0
    assert result.total_cents == 0


def test_allowed_seconds_applied_after_merge_not_before():
    # 两段暂停重叠，若先扣允许时长再合并会得到错误结果：
    # 正确顺序：合并后暂停 2 小时（01:00-03:00），净作业仅 1 小时；
    # 允许 1 小时恰好冲抵净作业，费用为零。允许扣减以净作业为基准，
    # 不能在含暂停的毛时长上扣减。
    result = calculate(
        work_start=t(0), work_end=t(3),
        raw_pauses=[(t(1), t(2, 30)), (t(2), t(3))],
        rate_cents_per_hour=1000,
        allowed_seconds=3600,
    )
    assert result.pauses_merged == [(t(1), t(3))]
    assert result.paused_seconds == 2 * 3600
    assert result.work_seconds == 3 * 3600
    assert result.allowed_seconds_used == 3600
    assert result.billable_seconds == 0
    assert result.billable_hours == 0
    assert result.total_cents == 0


def test_allowed_seconds_capped_against_net_not_gross():
    # 作业 3 小时、暂停 2 小时 -> 净作业仅 1 小时；允许 2 小时。
    # 实际扣减必须以净作业（1 小时）为上限，而非含暂停的毛时长（3 小时），
    # 更不能在扣减后再减暂停导致暂停被允许时长"补回"。
    result = calculate(
        work_start=t(0), work_end=t(3),
        raw_pauses=[(t(0), t(2))],
        rate_cents_per_hour=100,
        allowed_seconds=2 * 3600,
    )
    assert result.paused_seconds == 2 * 3600
    assert result.allowed_seconds_used == 3600
    assert result.billable_seconds == 0
    assert result.total_cents == 0


def test_allowed_seconds_balance_rounds_up():
    # 净作业 1 小时 1 秒，允许整 1 小时后余额 1 秒 -> 向上取整 1 小时。
    result = calculate(
        work_start=t(0), work_end=t(0) + timedelta(seconds=3601),
        raw_pauses=[],
        rate_cents_per_hour=250,
        allowed_seconds=3600,
    )
    assert result.billable_seconds == 1
    assert result.billable_hours == 1
    assert result.total_cents == 250


def test_default_allowed_seconds_zero_preserves_legacy_result():
    result = calculate(
        work_start=t(0), work_end=t(8),
        raw_pauses=[(t(1), t(5))],
        rate_cents_per_hour=100,
    )
    assert result.allowed_seconds == 0
    assert result.allowed_seconds_used == 0
    assert result.billable_seconds == 4 * 3600
    assert result.total_cents == 400


def test_invalid_allowed_seconds_rejected():
    for bad in (-1, 1.5, "100", True, None):
        with pytest.raises(IntervalError):
            calculate(t(0), t(2), [], 100, allowed_seconds=bad)


def _categories(segments):
    return [(s.start, s.end, s.seconds, s.category) for s in segments]


def test_timeline_boundary_scan_covers_whole_work_interval():
    # 边界扫描：切点相邻成段，首尾相接覆盖整个作业区间；无允许时长时
    # 非暂停段全部计费。
    segments = build_timeline(
        work_start=t(0), work_end=t(4),
        pauses_merged=[(t(1), t(2))],
        allowed_seconds_used=0,
    )
    assert _categories(segments) == [
        (t(0), t(1), 3600, "billable"),
        (t(1), t(2), 3600, "pause"),
        (t(2), t(4), 7200, "billable"),
    ]
    # 首段起点=作业起点，末段终点=作业终点，相邻段首尾相接
    assert segments[0].start == t(0)
    assert segments[-1].end == t(4)
    for previous, current in zip(segments, segments[1:]):
        assert previous.end == current.start


def test_timeline_allowed_spans_segments_and_splits_precisely():
    # 允许 9000 秒：第一段 3600 整段抵扣，第二段（10800）前 5400 抵扣、
    # 余额 5400 计费，耗尽后第三段整段计费；不产生零长度段。
    segments = build_timeline(
        work_start=t(0), work_end=t(8),
        pauses_merged=[(t(1), t(2)), (t(5), t(6))],
        allowed_seconds_used=9000,
    )
    assert _categories(segments) == [
        (t(0), t(1), 3600, "allowed"),
        (t(1), t(2), 3600, "pause"),
        (t(2), t(3, 30), 5400, "allowed"),
        (t(3, 30), t(5), 5400, "billable"),
        (t(5), t(6), 3600, "pause"),
        (t(6), t(8), 7200, "billable"),
    ]
    assert all(s.seconds > 0 for s in segments)


def test_timeline_allowed_exactly_consumes_segment_without_zero_split():
    # 允许时长恰好等于工作段长度：整段抵扣，不产生零长度计费段。
    segments = build_timeline(
        work_start=t(0), work_end=t(2),
        pauses_merged=[(t(1), t(2))],
        allowed_seconds_used=3600,
    )
    assert _categories(segments) == [
        (t(0), t(1), 3600, "allowed"),
        (t(1), t(2), 3600, "pause"),
    ]


def test_timeline_rejects_inconsistent_snapshot():
    work = (t(0), t(4))
    # 合并暂停互相重叠
    with pytest.raises(IntervalError):
        build_timeline(*work, [(t(1), t(3)), (t(2), t(4))], 0)
    # 合并暂停超出作业边界
    with pytest.raises(IntervalError):
        build_timeline(*work, [(t(3), t(5))], 0)
    # 合并暂停倒置
    with pytest.raises(IntervalError):
        build_timeline(*work, [(t(2), t(1))], 0)
    # 实际抵扣秒数超过非暂停时长，无法完整消耗
    with pytest.raises(IntervalError):
        build_timeline(*work, [(t(0), t(2))], 3 * 3600)
    # 作业区间倒置 / 非法抵扣值
    with pytest.raises(IntervalError):
        build_timeline(t(4), t(0), [], 0)
    with pytest.raises(IntervalError):
        build_timeline(*work, [], -1)
