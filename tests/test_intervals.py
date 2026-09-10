from datetime import datetime, timedelta, timezone

import pytest

from app.intervals import (
    IntervalError,
    billable_hours,
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
