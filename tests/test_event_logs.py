"""交接班事件簿接口：确定性状态机编译、派生暂停区间与费用、原子回滚与回放。

验收点：
  1. 无暂停日志（开工→完工）：派生区间为空，整段作业按原规则计费；
  2. 多次暂停日志：每对「暂停→复工」派生左闭右开区间，调用原计费规则，
     允许秒数在净作业时长上扣减；创建响应返回完整事件簿；
  3. 缺开工/缺完工、连续暂停、未暂停即复工、暂停后直接完工、时间倒退/相等
     均返回 422 且定位到事件下标，字段级问题定位到 events/<下标>/at；
  4. 编译或计费失败原子回滚：事件簿与结算记录都不留；
  5. 第二个入口按事件簿标识回放，输入与生成结果稳定，关联结果可单独回查。
"""

import pytest

from app.db import SessionLocal
from app.eventbook import (
    EVENT_FINISH_WORK,
    EVENT_PAUSE,
    EVENT_RESUME,
    EVENT_START_WORK,
    EventCompileError,
    compile_event_book,
)
from app.models import DemurrageRecord, EventLog
from app.timeparse import parse_utc_second

PATH = "/api/v1/demurrage/event-logs"
CALC_PATH = "/api/v1/demurrage/calculations"
MISSING_ID = "00000000-0000-0000-0000-000000000000"


def counts() -> tuple[int, int]:
    with SessionLocal() as db:
        return (
            db.query(EventLog).count(),
            db.query(DemurrageRecord).count(),
        )


def events(*pairs: tuple[str, str]) -> list[dict]:
    return [{"type": kind, "at": at} for kind, at in pairs]


def _locs(payload) -> list[str]:
    return ["/".join(str(p) for p in e["loc"]) for e in payload["detail"]]


def test_compiler_deterministic_state_machine_unit():
    """编译器纯函数：合法序列产出作业边界、派生暂停与完整迁移摘要。"""
    compiled = compile_event_book(
        [
            (EVENT_START_WORK, parse_utc_second("2026-09-10T00:00:00Z")),
            (EVENT_PAUSE, parse_utc_second("2026-09-10T01:00:00Z")),
            (EVENT_RESUME, parse_utc_second("2026-09-10T02:00:00Z")),
            (EVENT_FINISH_WORK, parse_utc_second("2026-09-10T08:00:00Z")),
        ]
    )
    assert compiled.work_start == parse_utc_second("2026-09-10T00:00:00Z")
    assert compiled.work_end == parse_utc_second("2026-09-10T08:00:00Z")
    assert compiled.pauses == [
        (
            parse_utc_second("2026-09-10T01:00:00Z"),
            parse_utc_second("2026-09-10T02:00:00Z"),
        )
    ]
    assert compiled.valid_pause_count == 1
    assert compiled.summary() == {
        "event_count": 4,
        "valid_pause_count": 1,
        "transitions": [
            EVENT_START_WORK,
            EVENT_PAUSE,
            EVENT_RESUME,
            EVENT_FINISH_WORK,
        ],
        "work_start_event": EVENT_START_WORK,
        "work_end_event": EVENT_FINISH_WORK,
    }


COMPILER_INVALID_CASES = [
    (
        "缺开工",
        [
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_RESUME, "2026-09-10T02:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ],
        0,
    ),
    (
        "缺完工",
        [
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_RESUME, "2026-09-10T02:00:00Z"),
        ],
        2,
    ),
    (
        "连续暂停",
        [
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T02:00:00Z"),
            (EVENT_RESUME, "2026-09-10T03:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ],
        2,
    ),
    (
        "未暂停即复工",
        [
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_RESUME, "2026-09-10T01:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ],
        1,
    ),
    (
        "暂停后直接完工",
        [
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ],
        2,
    ),
    (
        "时间倒退",
        [
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T02:00:00Z"),
            (EVENT_RESUME, "2026-09-10T01:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ],
        2,
    ),
    (
        "时间相等（非严格递增）",
        [
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_RESUME, "2026-09-10T01:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ],
        2,
    ),
    (
        "开工出现在第二位",
        [
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_START_WORK, "2026-09-10T01:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ],
        1,
    ),
]


def test_compiler_rejects_invalid_transitions_with_index():
    for label, raw, expected_index in COMPILER_INVALID_CASES:
        parsed = [(kind, parse_utc_second(at)) for kind, at in raw]
        with pytest.raises(EventCompileError) as exc_info:
            compile_event_book(parsed)
        assert exc_info.value.index == expected_index, label


def test_compiler_rejects_empty_book():
    with pytest.raises(EventCompileError) as exc_info:
        compile_event_book([])
    assert exc_info.value.index == 0


def test_log_without_pauses_derives_empty_intervals_and_bills_full_shift(client):
    """日志一（无暂停）：开工→完工，派生区间为空，整段 8 小时计费。"""
    body = {
        "events": events(
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ),
        "rate_cents_per_hour": 100,
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()

    # 创建响应返回完整事件簿（含提交下标）
    assert data["events"] == [
        {"index": 0, "type": EVENT_START_WORK, "at": "2026-09-10T00:00:00Z"},
        {"index": 1, "type": EVENT_FINISH_WORK, "at": "2026-09-10T08:00:00Z"},
    ]
    # 无有效暂停：派生区间为空，编译摘要如实记录
    assert data["pauses_derived"] == []
    assert data["compilation_summary"] == {
        "event_count": 2,
        "valid_pause_count": 0,
        "transitions": [EVENT_START_WORK, EVENT_FINISH_WORK],
        "work_start_event": EVENT_START_WORK,
        "work_end_event": EVENT_FINISH_WORK,
    }
    # 原计费规则：8 小时 × 100 分 = 800
    result = data["result"]
    assert result["id"] == data["result_id"]
    assert result["pauses"] == []
    assert result["pauses_merged"] == []
    assert result["work_seconds"] == 8 * 3600
    assert result["paused_seconds"] == 0
    assert result["billable_seconds"] == 8 * 3600
    assert result["billable_hours"] == 8
    assert result["total_cents"] == 800
    assert counts() == (1, 1)


def test_log_with_multiple_pauses_derives_intervals_and_fees(client):
    """日志二（多次暂停）：两对暂停/复工派生两个左闭右开区间并按原规则计费。"""
    body = {
        "events": events(
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_RESUME, "2026-09-10T02:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T04:00:00Z"),
            (EVENT_RESUME, "2026-09-10T05:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ),
        "rate_cents_per_hour": 100,
        "allowed_seconds": 3600,
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["pauses_derived"] == [
        {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"},
        {"start": "2026-09-10T04:00:00Z", "end": "2026-09-10T05:00:00Z"},
    ]
    assert data["compilation_summary"]["valid_pause_count"] == 2
    assert data["compilation_summary"]["event_count"] == 6
    result = data["result"]
    # 作业 8h − 暂停 2h = 净作业 6h；再扣允许 1h -> 5h × 100 = 500
    assert result["pauses"] == data["pauses_derived"]
    assert result["pauses_merged"] == data["pauses_derived"]
    assert result["paused_seconds"] == 2 * 3600
    assert result["allowed_seconds"] == 3600
    assert result["allowed_seconds_used"] == 3600
    assert result["billable_seconds"] == 5 * 3600
    assert result["billable_hours"] == 5
    assert result["total_cents"] == 500
    assert counts() == (1, 1)

    # 第二个入口：按事件簿标识回放，输入与生成结果稳定
    replay = client.get(f"{PATH}/{data['id']}")
    assert replay.status_code == 200
    assert replay.json() == data

    # 关联结果标识可在原创建接口按 id 回查
    linked = client.get(f"{CALC_PATH}/{data['result_id']}")
    assert linked.status_code == 200
    assert linked.json()["total_cents"] == 500
    assert linked.json()["pauses"] == data["pauses_derived"]


def test_repeated_replay_is_stable(client):
    body = {
        "events": events(
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_RESUME, "2026-09-10T02:30:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T05:00:00Z"),
        ),
        "rate_cents_per_hour": 240,
    }
    created = client.post(PATH, json=body).json()
    first = client.get(f"{PATH}/{created['id']}").json()
    second = client.get(f"{PATH}/{created['id']}").json()
    assert first == second == created


def test_get_missing_event_log_returns_404(client):
    resp = client.get(f"{PATH}/{MISSING_ID}")
    assert resp.status_code == 404
    assert MISSING_ID in resp.json()["detail"]


@pytest.mark.parametrize("label,raw,expected_index", COMPILER_INVALID_CASES)
def test_invalid_transitions_return_422_located_at_index_and_atomic_rollback(
    client, label, raw, expected_index
):
    before = counts()
    resp = client.post(
        PATH,
        json={"events": events(*raw), "rate_cents_per_hour": 100},
    )
    assert resp.status_code == 422, (label, resp.text)
    payload = resp.json()
    assert "id" not in payload, label
    assert f"body/events/{expected_index}" in _locs(payload), (label, _locs(payload))
    # 原子回滚：非法转换不留下事件簿或结算记录
    assert counts() == before, label


def test_empty_events_is_422_at_events_field(client):
    resp = client.post(PATH, json={"events": [], "rate_cents_per_hour": 100})
    assert resp.status_code == 422
    assert "body/events" in _locs(resp.json())
    assert counts() == (0, 0)


def test_bad_timestamp_located_at_event_at_field(client):
    resp = client.post(
        PATH,
        json={
            "events": events(
                (EVENT_START_WORK, "2026-09-10T00:00:00.5Z"),
                (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
            ),
            "rate_cents_per_hour": 100,
        },
    )
    assert resp.status_code == 422
    assert "body/events/0/at" in _locs(resp.json())
    assert counts() == (0, 0)


def test_unknown_event_type_is_422(client):
    resp = client.post(
        PATH,
        json={
            "events": [
                {"type": "nap", "at": "2026-09-10T01:00:00Z"},
                {"type": "finish_work", "at": "2026-09-10T08:00:00Z"},
            ],
            "rate_cents_per_hour": 100,
        },
    )
    assert resp.status_code == 422
    assert "body/events/0/type" in _locs(resp.json())
    assert counts() == (0, 0)


INVALID_RATE_BODIES = [
    ("负费率", -1),
    ("小数费率", 1.5),
    ("字符串费率", "100"),
    ("布尔费率", True),
]


@pytest.mark.parametrize("label,rate", INVALID_RATE_BODIES)
def test_invalid_rate_returns_located_422_and_nothing_persisted(client, label, rate):
    body = {
        "events": events(
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ),
        "rate_cents_per_hour": rate,
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 422, (label, resp.text)
    assert "body/rate_cents_per_hour" in _locs(resp.json())
    assert counts() == (0, 0)


def test_extra_field_forbidden(client):
    body = {
        "events": events(
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ),
        "rate_cents_per_hour": 100,
        "unexpected": 1,
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 422
    assert any("unexpected" in loc for loc in _locs(resp.json()))
    assert counts() == (0, 0)


def test_billing_failure_rolls_back_both_event_log_and_record(client, monkeypatch):
    """编译通过但计费失败时，同一事务整体回滚：两张表都不留痕。"""
    from app.intervals import IntervalError
    from app import services

    def boom(*args, **kwargs):
        raise IntervalError("注入的计费失败")

    # 直接在 services 模块内替换 run_calculate 的绑定名
    monkeypatch.setattr(services, "run_calculate", boom)

    body = {
        "events": events(
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_PAUSE, "2026-09-10T01:00:00Z"),
            (EVENT_RESUME, "2026-09-10T02:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ),
        "rate_cents_per_hour": 100,
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 422, resp.text
    assert "id" not in resp.json()
    assert counts() == (0, 0)


def test_commit_failure_rolls_back(client, monkeypatch):
    """提交阶段失败同样整体回滚（事件簿与结算记录同生共死）。"""
    from sqlalchemy.orm import Session

    real_commit = Session.commit

    def failing_commit(self):
        # 仅让事件簿创建路径的第一次提交失败
        failing_commit.calls += 1
        if failing_commit.calls == 1:
            self.rollback()
            raise RuntimeError("注入的提交失败")
        return real_commit(self)

    failing_commit.calls = 0
    monkeypatch.setattr(Session, "commit", failing_commit)

    body = {
        "events": events(
            (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
            (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
        ),
        "rate_cents_per_hour": 100,
    }
    # TestClient 默认把服务端异常抛出；无论响应如何，回滚后两张表都为空。
    with pytest.raises(RuntimeError):
        client.post(PATH, json=body)
    assert counts() == (0, 0)


def test_event_log_persisted_rows_are_replayable(client):
    """直接从持久化层验证：原始事件、派生区间、编译摘要与结果标识同事务保存。"""
    resp = client.post(
        PATH,
        json={
            "events": events(
                (EVENT_START_WORK, "2026-09-10T00:00:00Z"),
                (EVENT_FINISH_WORK, "2026-09-10T08:00:00Z"),
            ),
            "rate_cents_per_hour": 100,
        },
    )
    assert resp.status_code == 201
    with SessionLocal() as db:
        log = db.query(EventLog).one()
        assert [e["index"] for e in log.events] == [0, 1]
        assert log.compilation_summary["valid_pause_count"] == 0
        assert log.pauses_derived == []
        record = db.get(DemurrageRecord, log.result_id)
        assert record is not None
        assert record.total_cents == 100 * record.billable_hours
