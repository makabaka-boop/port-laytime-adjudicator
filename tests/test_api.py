from app.models import DemurrageRecord
from app.db import SessionLocal

PATH = "/api/v1/demurrage/calculations"

BASE_BODY = {
    "work_start": "2026-09-10T00:00:00Z",
    "work_end": "2026-09-10T08:00:00Z",
    "rate_cents_per_hour": 100,
    "pauses": [],
}


def row_count() -> int:
    with SessionLocal() as db:
        return db.query(DemurrageRecord).count()


def test_create_and_fetch_by_id(client):
    body = {
        **BASE_BODY,
        "pauses": [
            {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T03:00:00Z"},
            {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T05:00:00Z"},
        ],
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["pauses_merged"] == [
        {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T05:00:00Z"}
    ]
    assert data["paused_seconds"] == 4 * 3600
    assert data["billable_seconds"] == 4 * 3600
    assert data["billable_hours"] == 4
    assert data["total_cents"] == 400
    # 原始输入已持久化
    assert data["pauses"] == body["pauses"]
    assert row_count() == 1

    fetched = client.get(f"{PATH}/{data['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == data["id"]
    assert fetched.json()["pauses_merged"] == data["pauses_merged"]


def test_overlap_deducted_once_and_cross_boundary(client):
    body = {
        "work_start": "2026-09-10T08:00:00Z",
        "work_end": "2026-09-10T09:00:00Z",
        "rate_cents_per_hour": 600,
        "pauses": [
            {"start": "2026-09-10T07:00:00Z", "end": "2026-09-10T08:30:00Z"},
            {"start": "2026-09-10T08:45:00Z", "end": "2026-09-10T10:00:00Z"},
            # 作业外、仅相接：裁剪为空
            {"start": "2026-09-10T10:00:00Z", "end": "2026-09-10T11:00:00Z"},
            {"start": "2026-09-10T07:00:00Z", "end": "2026-09-10T08:00:00Z"},
        ],
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["pauses_merged"] == [
        {"start": "2026-09-10T08:00:00Z", "end": "2026-09-10T08:30:00Z"},
        {"start": "2026-09-10T08:45:00Z", "end": "2026-09-10T09:00:00Z"},
    ]
    assert data["paused_seconds"] == 45 * 60
    assert data["billable_seconds"] == 15 * 60
    assert data["billable_hours"] == 1
    assert data["total_cents"] == 600


def test_zero_seconds_zero_fee(client):
    body = {
        "work_start": "2026-09-10T00:00:00Z",
        "work_end": "2026-09-10T01:00:00Z",
        "rate_cents_per_hour": 250,
        "pauses": [
            {"start": "2026-09-10T00:00:00Z", "end": "2026-09-10T01:00:00Z"}
        ],
    }
    data = client.post(PATH, json=body).json()
    assert data["billable_seconds"] == 0
    assert data["billable_hours"] == 0
    assert data["total_cents"] == 0


def test_get_missing_returns_404(client):
    resp = client.get(f"{PATH}/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert "00000000" in resp.json()["detail"]


def test_allowed_seconds_less_than_net_only_balance_billed(client):
    body = {
        **BASE_BODY,
        "work_end": "2026-09-10T04:00:00Z",
        "pauses": [
            {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"}
        ],
        "allowed_seconds": 3600,
    }
    resp = client.post(PATH, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["work_seconds"] == 4 * 3600
    assert data["paused_seconds"] == 3600
    assert data["allowed_seconds"] == 3600
    assert data["allowed_seconds_used"] == 3600
    assert data["billable_seconds"] == 2 * 3600
    assert data["billable_hours"] == 2
    assert data["total_cents"] == 200

    fetched = client.get(f"{PATH}/{data['id']}").json()
    assert fetched["allowed_seconds"] == 3600
    assert fetched["allowed_seconds_used"] == 3600
    assert fetched["billable_seconds"] == 2 * 3600
    assert fetched["total_cents"] == 200


def test_allowed_seconds_equal_or_exceed_net_is_zero_fee(client):
    common = {
        "work_start": "2026-09-10T00:00:00Z",
        "work_end": "2026-09-10T04:00:00Z",
        "rate_cents_per_hour": 100,
        "pauses": [
            {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"}
        ],
    }
    # 等于净作业时长（3 小时）
    equal = client.post(PATH, json={**common, "allowed_seconds": 3 * 3600})
    assert equal.status_code == 201, equal.text
    data = equal.json()
    assert data["allowed_seconds_used"] == 3 * 3600
    assert data["billable_seconds"] == 0
    assert data["billable_hours"] == 0
    assert data["total_cents"] == 0

    # 超过净作业时长：约定值原样回显，实际扣减以净作业为上限
    exceed = client.post(PATH, json={**common, "allowed_seconds": 99 * 3600})
    assert exceed.status_code == 201, exceed.text
    data = exceed.json()
    assert data["allowed_seconds"] == 99 * 3600
    assert data["allowed_seconds_used"] == 3 * 3600
    assert data["billable_seconds"] == 0
    assert data["total_cents"] == 0


def test_merge_pauses_before_deducting_allowed(client):
    # 重叠暂停合并后为 2 小时（01:00-03:00），净作业 1 小时；
    # 允许 1 小时 -> 余额 0。允许扣减必须发生在合并之后，
    # 且实际扣减以净作业为上限，暂停不会被允许时长补回。
    body = {
        "work_start": "2026-09-10T00:00:00Z",
        "work_end": "2026-09-10T03:00:00Z",
        "rate_cents_per_hour": 1000,
        "pauses": [
            {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:30:00Z"},
            {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T03:00:00Z"},
        ],
        "allowed_seconds": 2 * 3600,
    }
    data = client.post(PATH, json=body).json()
    assert data["pauses_merged"] == [
        {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T03:00:00Z"}
    ]
    assert data["paused_seconds"] == 2 * 3600
    assert data["allowed_seconds"] == 2 * 3600
    assert data["allowed_seconds_used"] == 3600
    assert data["billable_seconds"] == 0
    assert data["total_cents"] == 0


def test_allowed_seconds_balance_rounds_up(client):
    body = {
        "work_start": "2026-09-10T00:00:00Z",
        "work_end": "2026-09-10T01:00:01Z",
        "rate_cents_per_hour": 250,
        "pauses": [],
        "allowed_seconds": 3600,
    }
    data = client.post(PATH, json=body).json()
    assert data["allowed_seconds_used"] == 3600
    assert data["billable_seconds"] == 1
    assert data["billable_hours"] == 1
    assert data["total_cents"] == 250


def test_legacy_request_without_allowed_seconds_defaults_zero(client):
    resp = client.post(PATH, json=BASE_BODY)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["allowed_seconds"] == 0
    assert data["allowed_seconds_used"] == 0
    # 旧结果数字保持不变
    assert data["work_seconds"] == 8 * 3600
    assert data["paused_seconds"] == 0
    assert data["billable_seconds"] == 8 * 3600
    assert data["billable_hours"] == 8
    assert data["total_cents"] == 800

    fetched = client.get(f"{PATH}/{data['id']}").json()
    assert fetched["allowed_seconds"] == 0
    assert fetched["allowed_seconds_used"] == 0
    assert fetched["total_cents"] == 800


ALLOWED_INVALID_CASES = [
    ("negative", -1),
    ("float", 1.5),
    ("float integral", 3600.0),
    ("string", "3600"),
    ("boolean", True),
    ("null", None),
]


def test_invalid_allowed_seconds_returns_located_422_and_nothing_persisted(client):
    for label, value in ALLOWED_INVALID_CASES:
        before = row_count()
        resp = client.post(PATH, json={**BASE_BODY, "allowed_seconds": value})
        assert resp.status_code == 422, f"{label}: {resp.text}"
        payload = resp.json()
        assert "id" not in payload, label
        assert "body/allowed_seconds" in _locs(payload), (label, _locs(payload))
        assert row_count() == before, f"{label}: 非法允许秒数写入了记录"


def test_pre_migration_records_remain_queryable_with_zero_allowance(client):
    """迁移前记录经 Alembic 回填零允许时长后仍可查询，费用原样可复核。

    回填行为本身（对既有行写入 0、旧结构无此两列）在 test_migrations 中验证；
    此处直接以原始 SQL 模拟一条回填后的历史记录。
    """
    from sqlalchemy import text

    from app.db import engine

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
                    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    '2026-09-10 00:00:00+00:00',
                    '2026-09-10 08:00:00+00:00',
                    '[]', 100, '[]', 28800, 0, 0, 0, 28800, 8, 800,
                    '2026-09-10 00:00:00+00:00'
                )
                """
            )
        )

    resp = client.get(f"{PATH}/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert data["allowed_seconds"] == 0
    assert data["allowed_seconds_used"] == 0
    assert data["billable_seconds"] == 28800
    assert data["total_cents"] == 800


def _locs(payload):
    return ["/".join(str(p) for p in e["loc"]) for e in payload["detail"]]


INVALID_CASES = [
    (
        "fractional seconds",
        {**BASE_BODY, "work_start": "2026-09-10T00:00:00.5Z"},
        "body/work_start",
    ),
    (
        "non-utc offset",
        {**BASE_BODY, "work_end": "2026-09-10T16:00:00+08:00"},
        "body/work_end",
    ),
    (
        "work end before start",
        {**BASE_BODY, "work_end": "2026-09-09T23:00:00Z"},
        "body/work_end",
    ),
    (
        "work end equals start",
        {**BASE_BODY, "work_end": "2026-09-10T00:00:00Z"},
        "body/work_end",
    ),
    (
        "pause inverted",
        {
            **BASE_BODY,
            "pauses": [
                {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T01:00:00Z"}
            ],
        },
        "body/pauses/0/end",
    ),
    (
        "pause zero length",
        {
            **BASE_BODY,
            "pauses": [
                {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T01:00:00Z"}
            ],
        },
        "body/pauses/0/end",
    ),
    (
        "negative rate",
        {**BASE_BODY, "rate_cents_per_hour": -5},
        "body/rate_cents_per_hour",
    ),
    (
        "float rate rejected",
        {**BASE_BODY, "rate_cents_per_hour": 1.5},
        "body/rate_cents_per_hour",
    ),
    (
        "missing field",
        {"work_start": "2026-09-10T00:00:00Z",
         "work_end": "2026-09-10T08:00:00Z"},
        "body/rate_cents_per_hour",
    ),
    (
        "unknown field",
        {**BASE_BODY, "extra": 1},
        "body/extra",
    ),
]


def test_invalid_requests_return_located_errors_and_nothing_persisted(client):
    for label, body, expected_loc in INVALID_CASES:
        before = row_count()
        resp = client.post(PATH, json=body)
        assert resp.status_code == 422, f"{label}: {resp.text}"
        payload = resp.json()
        assert "id" not in payload, label
        assert expected_loc in _locs(payload), (label, _locs(payload))
        assert row_count() == before, f"{label}: 非法请求写入了记录"


def test_empty_body_is_422(client):
    resp = client.post(PATH, json={})
    assert resp.status_code == 422
    locs = _locs(resp.json())
    for field in ("body/work_start", "body/work_end", "body/rate_cents_per_hour"):
        assert field in locs


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
