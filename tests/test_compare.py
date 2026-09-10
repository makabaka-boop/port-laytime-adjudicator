"""结果对比接口：结构化差异、有符号增减、定向 404、不写库。"""

from app.db import SessionLocal
from app.models import DemurrageRecord

PATH = "/api/v1/demurrage/calculations"
COMPARE_PATH = "/api/v1/demurrage/comparisons"

BASE_BODY = {
    "work_start": "2026-09-10T00:00:00Z",
    "work_end": "2026-09-10T08:00:00Z",
    "rate_cents_per_hour": 100,
    "pauses": [],
}

MISSING_ID = "00000000-0000-0000-0000-000000000000"


def row_count() -> int:
    with SessionLocal() as db:
        return db.query(DemurrageRecord).count()


def create(client, body) -> dict:
    resp = client.post(PATH, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def compare(client, base_id, candidate_id):
    return client.post(
        COMPARE_PATH, json={"base_id": base_id, "candidate_id": candidate_id}
    )


def assert_empty_diff(payload: dict) -> None:
    assert payload["changes"] == {
        "work_start": False,
        "work_end": False,
        "pauses": False,
        "pauses_merged": False,
        "rate_cents_per_hour": False,
        "allowed_seconds": False,
    }
    assert payload["deltas"] == {
        "paused_seconds": 0,
        "billable_seconds": 0,
        "billable_hours": 0,
        "total_cents": 0,
    }


def test_rate_change_reflected_in_fee_delta(client):
    base = create(client, BASE_BODY)
    candidate = create(client, {**BASE_BODY, "rate_cents_per_hour": 150})
    before = row_count()

    resp = compare(client, base["id"], candidate["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["base_id"] == base["id"]
    assert data["candidate_id"] == candidate["id"]
    # 仅费率变化
    assert data["changes"] == {
        "work_start": False,
        "work_end": False,
        "pauses": False,
        "pauses_merged": False,
        "rate_cents_per_hour": True,
        "allowed_seconds": False,
    }
    # 8 小时 × (150 − 100) 分/小时 = +400 分
    assert data["deltas"] == {
        "paused_seconds": 0,
        "billable_seconds": 0,
        "billable_hours": 0,
        "total_cents": 400,
    }
    # 对比不生成新的结算记录
    assert row_count() == before


def test_allowed_seconds_change_reflected_in_fee_delta(client):
    base = create(client, BASE_BODY)
    candidate = create(client, {**BASE_BODY, "allowed_seconds": 7200})

    resp = compare(client, base["id"], candidate["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["changes"]["allowed_seconds"] is True
    assert data["changes"]["rate_cents_per_hour"] is False
    # 允许 2 小时：可计费 8h -> 6h，费用 800 -> 600
    assert data["deltas"] == {
        "paused_seconds": 0,
        "billable_seconds": -7200,
        "billable_hours": -2,
        "total_cents": -200,
    }


def test_pause_order_change_not_reported(client):
    pauses = [
        {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"},
        {"start": "2026-09-10T03:00:00Z", "end": "2026-09-10T04:00:00Z"},
    ]
    base = create(client, {**BASE_BODY, "pauses": pauses})
    # 同一组暂停，仅提交顺序颠倒
    candidate = create(client, {**BASE_BODY, "pauses": list(reversed(pauses))})

    resp = compare(client, base["id"], candidate["id"])
    assert resp.status_code == 200, resp.text
    assert_empty_diff(resp.json())


def test_real_pause_interval_change_visible(client):
    base = create(
        client,
        {
            **BASE_BODY,
            "pauses": [
                {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"}
            ],
        },
    )
    candidate = create(
        client,
        {
            **BASE_BODY,
            "pauses": [
                {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T03:00:00Z"}
            ],
        },
    )

    resp = compare(client, base["id"], candidate["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["changes"]["pauses"] is True
    assert data["changes"]["pauses_merged"] is True
    # 暂停 1h -> 2h：可计费 7h -> 6h，费用 700 -> 600
    assert data["deltas"] == {
        "paused_seconds": 3600,
        "billable_seconds": -3600,
        "billable_hours": -1,
        "total_cents": -100,
    }


def test_work_period_change_visible(client):
    base = create(client, BASE_BODY)
    candidate = create(client, {**BASE_BODY, "work_end": "2026-09-10T10:00:00Z"})

    resp = compare(client, base["id"], candidate["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["changes"]["work_start"] is False
    assert data["changes"]["work_end"] is True
    # 作业 8h -> 10h：费用 800 -> 1000
    assert data["deltas"]["billable_seconds"] == 7200
    assert data["deltas"]["billable_hours"] == 2
    assert data["deltas"]["total_cents"] == 200


def test_same_id_returns_empty_diff(client):
    base = create(
        client,
        {
            **BASE_BODY,
            "pauses": [
                {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T02:00:00Z"}
            ],
            "allowed_seconds": 3600,
        },
    )

    resp = compare(client, base["id"], base["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["base_id"] == base["id"]
    assert data["candidate_id"] == base["id"]
    assert_empty_diff(data)


def test_missing_ids_return_directed_404_and_nothing_persisted(client):
    base = create(client, BASE_BODY)
    before = row_count()

    # 基准缺失：错误指向 base_id
    resp = compare(client, MISSING_ID, base["id"])
    assert resp.status_code == 404, resp.text
    assert "base_id" in resp.json()["detail"]
    assert MISSING_ID in resp.json()["detail"]

    # 候选缺失：错误指向 candidate_id
    resp = compare(client, base["id"], MISSING_ID)
    assert resp.status_code == 404, resp.text
    assert "candidate_id" in resp.json()["detail"]
    assert MISSING_ID in resp.json()["detail"]

    # 两个都缺失：优先指出基准
    resp = compare(client, MISSING_ID, MISSING_ID)
    assert resp.status_code == 404, resp.text
    assert "base_id" in resp.json()["detail"]

    # 失败请求不写库
    assert row_count() == before


def test_invalid_compare_request_is_422_and_nothing_persisted(client):
    before = row_count()
    for body in (
        {},
        {"base_id": "x"},
        {"candidate_id": "x"},
        {"base_id": "x", "candidate_id": "y", "extra": 1},
    ):
        resp = client.post(COMPARE_PATH, json=body)
        assert resp.status_code == 422, resp.text
    assert row_count() == before
