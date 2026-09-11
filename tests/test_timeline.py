"""计费时间线接口：连续分段、允许时长跨段消耗、汇总一致性与错误语义。

验收点：
  1. 跨界/相接暂停按已合并快照只形成连续分段（首尾相接、覆盖整个作业区间）；
  2. 允许时长跨多个工作段消耗，并在段内精确切分为抵扣/计费两段；
  3. 允许时长耗尽后的工作段计费，汇总字段与原结算结果吻合；
  4. 缺失标识返回带标识的 404，快照不一致返回 409，二者均不写库。
"""

from sqlalchemy import text

from app.db import SessionLocal, engine
from app.models import DemurrageRecord

PATH = "/api/v1/demurrage/calculations"
MISSING_ID = "00000000-0000-0000-0000-000000000000"


def row_count() -> int:
    with SessionLocal() as db:
        return db.query(DemurrageRecord).count()


def create(client, body) -> dict:
    resp = client.post(PATH, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def timeline(client, result_id: str):
    return client.get(f"{PATH}/{result_id}/timeline")


def assert_contiguous(segments: list[dict], work_start: str, work_end: str) -> None:
    """分段首尾相接、覆盖整个作业区间，且无零长度段。"""
    assert segments, "时间线不应为空"
    assert segments[0]["start"] == work_start
    assert segments[-1]["end"] == work_end
    for previous, current in zip(segments, segments[1:]):
        assert previous["end"] == current["start"]
    for segment in segments:
        assert segment["seconds"] > 0
        assert segment["category"] in ("pause", "allowed", "billable")


def category_totals(segments: list[dict]) -> dict:
    totals = {"pause": 0, "allowed": 0, "billable": 0}
    for segment in segments:
        totals[segment["category"]] += segment["seconds"]
    return totals


def test_cross_boundary_and_touching_pauses_form_continuous_segments(client):
    """跨界与首尾相接的暂停按已合并快照分段，时间线连续覆盖作业区间。"""
    record = create(
        client,
        {
            "work_start": "2026-09-10T08:00:00Z",
            "work_end": "2026-09-10T09:00:00Z",
            "rate_cents_per_hour": 600,
            "pauses": [
                # 左跨界：仅 08:00-08:30 计入
                {"start": "2026-09-10T07:00:00Z", "end": "2026-09-10T08:30:00Z"},
                # 与上一段相接（08:30）且右跨界：仅 08:30-09:00 计入，
                # 两段相接合并为 08:00-09:00 一段
                {"start": "2026-09-10T08:30:00Z", "end": "2026-09-10T10:00:00Z"},
                # 完全在作业外，裁剪为空
                {"start": "2026-09-10T10:00:00Z", "end": "2026-09-10T11:00:00Z"},
            ],
        },
    )
    assert record["pauses_merged"] == [
        {"start": "2026-09-10T08:00:00Z", "end": "2026-09-10T09:00:00Z"}
    ]

    resp = timeline(client, record["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == record["id"]
    # 相接暂停已合并为一段，时间线只有一段暂停，不出现重复或缝隙
    assert data["segments"] == [
        {
            "start": "2026-09-10T08:00:00Z",
            "end": "2026-09-10T09:00:00Z",
            "seconds": 3600,
            "category": "pause",
        }
    ]
    assert_contiguous(
        data["segments"], "2026-09-10T08:00:00Z", "2026-09-10T09:00:00Z"
    )
    assert data["paused_seconds"] == 3600
    assert data["billable_seconds"] == 0
    assert data["billable_hours"] == 0
    assert data["total_cents"] == 0


def test_allowed_consumed_across_work_segments_with_in_segment_split(client):
    """允许时长跨多个工作段消耗，并在第二个工作段内精确切分。"""
    # 工作段：00:00-01:00（3600）、02:00-05:00（10800）、06:00-08:00（7200）。
    # 允许 9000 秒 = 第一段 3600 全抵扣 + 第二段前 5400 秒抵扣。
    record = create(
        client,
        {
            "work_start": "2026-09-10T00:00:00Z",
            "work_end": "2026-09-10T08:00:00Z",
            "rate_cents_per_hour": 100,
            "pauses": [
                {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"},
                {"start": "2026-09-10T05:00:00Z", "end": "2026-09-10T06:00:00Z"},
            ],
            "allowed_seconds": 9000,
        },
    )
    assert record["allowed_seconds_used"] == 9000
    assert record["billable_seconds"] == 12600

    resp = timeline(client, record["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["segments"] == [
        # 第一个工作段被允许时长整段抵扣
        {"start": "2026-09-10T00:00:00Z", "end": "2026-09-10T01:00:00Z",
         "seconds": 3600, "category": "allowed"},
        {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z",
         "seconds": 3600, "category": "pause"},
        # 第二个工作段内精确切分：前 5400 秒抵扣，余额 5400 秒计费
        {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T03:30:00Z",
         "seconds": 5400, "category": "allowed"},
        {"start": "2026-09-10T03:30:00Z", "end": "2026-09-10T05:00:00Z",
         "seconds": 5400, "category": "billable"},
        {"start": "2026-09-10T05:00:00Z", "end": "2026-09-10T06:00:00Z",
         "seconds": 3600, "category": "pause"},
        # 允许时长已耗尽，第三个工作段整段计费
        {"start": "2026-09-10T06:00:00Z", "end": "2026-09-10T08:00:00Z",
         "seconds": 7200, "category": "billable"},
    ]
    assert_contiguous(
        data["segments"], "2026-09-10T00:00:00Z", "2026-09-10T08:00:00Z"
    )


def test_segments_after_allowed_exhaustion_billed_and_summary_matches(client):
    """允许时长耗尽后的工作段计费，汇总回显与原结算结果吻合。"""
    record = create(
        client,
        {
            "work_start": "2026-09-10T00:00:00Z",
            "work_end": "2026-09-10T08:00:00Z",
            "rate_cents_per_hour": 100,
            "pauses": [
                {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"},
                {"start": "2026-09-10T05:00:00Z", "end": "2026-09-10T06:00:00Z"},
            ],
            "allowed_seconds": 9000,
        },
    )
    resp = timeline(client, record["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 分段合计与汇总回显一致，且汇总与原记录逐项吻合
    totals = category_totals(data["segments"])
    assert totals["pause"] == record["paused_seconds"] == data["paused_seconds"]
    assert (
        totals["allowed"]
        == record["allowed_seconds_used"]
        == data["allowed_seconds_used"]
    )
    assert (
        totals["billable"]
        == record["billable_seconds"]
        == data["billable_seconds"]
    )
    assert data["billable_hours"] == record["billable_hours"] == 4
    assert data["total_cents"] == record["total_cents"] == 400


def test_timeline_without_allowance_is_pause_and_billable_only(client):
    """旧格式结果（允许秒数为 0）时间线只含暂停与计费段。"""
    record = create(
        client,
        {
            "work_start": "2026-09-10T00:00:00Z",
            "work_end": "2026-09-10T04:00:00Z",
            "rate_cents_per_hour": 250,
            "pauses": [
                {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"}
            ],
        },
    )
    resp = timeline(client, record["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["segments"] == [
        {"start": "2026-09-10T00:00:00Z", "end": "2026-09-10T01:00:00Z",
         "seconds": 3600, "category": "billable"},
        {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z",
         "seconds": 3600, "category": "pause"},
        {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T04:00:00Z",
         "seconds": 7200, "category": "billable"},
    ]
    assert data["allowed_seconds_used"] == 0
    assert data["billable_seconds"] == 3 * 3600
    assert data["total_cents"] == 750


def test_missing_id_returns_404_with_id_and_nothing_written(client):
    before = row_count()
    resp = timeline(client, MISSING_ID)
    assert resp.status_code == 404
    assert MISSING_ID in resp.json()["detail"]
    assert row_count() == before


def _insert_inconsistent(record_id: str, pauses_merged: str, paused: int,
                         allowed_used: int, billable: int) -> None:
    """以原始 SQL 模拟一条快照不自洽的持久化记录。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO demurrage_records (
                    id, work_start, work_end, pauses, rate_cents_per_hour,
                    pauses_merged, work_seconds, paused_seconds,
                    allowed_seconds, allowed_seconds_used,
                    billable_seconds, billable_hours, total_cents, created_at
                ) VALUES (
                    :id,
                    '2026-09-10 00:00:00+00:00',
                    '2026-09-10 08:00:00+00:00',
                    '[]', 100, :pauses_merged, 28800, :paused,
                    :allowed_used, :allowed_used, :billable, 5, 500,
                    '2026-09-10 00:00:00+00:00'
                )
                """
            ),
            {
                "id": record_id,
                "pauses_merged": pauses_merged,
                "paused": paused,
                "allowed_used": allowed_used,
                "billable": billable,
            },
        )


INCONSISTENT_SNAPSHOTS = [
    # 合并暂停互相重叠：不是已合并快照，边界扫描拒绝
    (
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
        '[{"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T03:00:00Z"},'
        ' {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T04:00:00Z"}]',
        10800, 0, 18000,
    ),
    # 合并暂停超出作业边界：无法覆盖成连续时间线
    (
        "dddddddd-dddd-dddd-dddd-dddddddddddd",
        '[{"start": "2026-09-10T07:00:00Z", "end": "2026-09-10T09:00:00Z"}]',
        7200, 0, 21600,
    ),
    # 暂停分段合计与记录的暂停秒数不符
    (
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        '[{"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"}]',
        7200, 0, 21600,
    ),
    # 实际抵扣秒数超过非暂停时长，无法被完整消耗
    (
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        '[{"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"}]',
        3600, 8 * 3600, 0,
    ),
    # 计费分段合计与记录的计费秒数不符
    (
        "99999999-9999-9999-9999-999999999999",
        '[]',
        0, 3600, 99999,
    ),
]


def test_inconsistent_snapshot_returns_409_and_record_untouched(client):
    for record_id, pauses_merged, paused, allowed_used, billable in (
        INCONSISTENT_SNAPSHOTS
    ):
        _insert_inconsistent(record_id, pauses_merged, paused,
                             allowed_used, billable)
        before = row_count()

        resp = timeline(client, record_id)
        assert resp.status_code == 409, (record_id, resp.text)
        detail = resp.json()["detail"]
        assert record_id in detail
        assert "时间线" in detail

        # 不写库：记录数不变，原记录内容保持原样仍可回查
        assert row_count() == before
        fetched = client.get(f"{PATH}/{record_id}")
        assert fetched.status_code == 200
        assert fetched.json()["paused_seconds"] == paused
