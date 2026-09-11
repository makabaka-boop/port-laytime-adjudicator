"""航次封顶清单：照录 / 比例分配 / 最大余数 / 失败不留痕 / 完整回放。"""

from app.db import SessionLocal
from app.models import VoyageCapItem, VoyageCapList

CALC_PATH = "/api/v1/demurrage/calculations"
CAPS_PATH = "/api/v1/demurrage/voyage-caps"

MISSING_ID = "00000000-0000-0000-0000-000000000000"


def list_count() -> int:
    with SessionLocal() as db:
        return db.query(VoyageCapList).count()


def item_count() -> int:
    with SessionLocal() as db:
        return db.query(VoyageCapItem).count()


def counts() -> tuple[int, int]:
    return list_count(), item_count()


def make_result(client, fee_cents: int) -> str:
    """创建一条 1 小时作业的结算结果，费用恰为 fee_cents 分。"""
    resp = client.post(
        CALC_PATH,
        json={
            "work_start": "2026-09-10T00:00:00Z",
            "work_end": "2026-09-10T01:00:00Z",
            "rate_cents_per_hour": fee_cents,
            "pauses": [],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["total_cents"] == fee_cents
    return data["id"]


def make_results(client, fees: list[int]) -> list[str]:
    return [make_result(client, fee) for fee in fees]


def create_cap_list(client, result_ids: list[str], cap_cents: int):
    return client.post(
        CAPS_PATH, json={"result_ids": result_ids, "cap_cents": cap_cents}
    )


def test_cap_not_triggered_records_items_as_submitted(client):
    ids = make_results(client, [100, 200, 300])
    before = counts()

    resp = create_cap_list(client, ids, cap_cents=600)  # 合计恰好等于上限
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["capped"] is False
    assert data["cap_cents"] == 600
    assert data["original_total_cents"] == 600
    assert data["allocated_total_cents"] == 600
    assert [item["position"] for item in data["items"]] == [0, 1, 2]
    assert [item["result_id"] for item in data["items"]] == ids
    assert [item["original_cents"] for item in data["items"]] == [100, 200, 300]
    assert [item["allocated_cents"] for item in data["items"]] == [100, 200, 300]
    assert "T" in data["created_at"] and data["created_at"].endswith("Z")
    assert counts() == (before[0] + 1, before[1] + 3)

    # 上限高于合计时同样逐项照录
    resp = create_cap_list(client, ids, cap_cents=999999)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["capped"] is False
    assert data["allocated_total_cents"] == 600
    assert [item["allocated_cents"] for item in data["items"]] == [100, 200, 300]


def test_cap_exceeded_allocates_proportionally_with_exact_total(client):
    ids = make_results(client, [100, 200, 300])
    resp = create_cap_list(client, ids, cap_cents=450)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["capped"] is True
    assert data["original_total_cents"] == 600
    # 450 × (1:2:3)/6 = 75/150/225，无舍入余量
    assert [item["allocated_cents"] for item in data["items"]] == [75, 150, 225]
    assert data["allocated_total_cents"] == 450  # 总额精确等于上限


def test_largest_remainder_tie_broken_by_submission_order(client):
    # 三项余数完全相同（各 1/3），仅剩 1 分：提交顺序最前者多得
    ids = make_results(client, [100, 100, 100])
    resp = create_cap_list(client, ids, cap_cents=100)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert [item["allocated_cents"] for item in data["items"]] == [34, 33, 33]
    assert data["allocated_total_cents"] == 100

    # 部分同余数：余数相同的两项按下标顺序补足
    ids = make_results(client, [10, 10, 20])
    resp = create_cap_list(client, ids, cap_cents=22)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    # 精确份额 5.5 / 5.5 / 11：仅剩 1 分，前两项余数相同（0.5），
    # 提交顺序最前者多得
    assert [item["allocated_cents"] for item in data["items"]] == [6, 5, 11]
    assert data["allocated_total_cents"] == 22


def test_zero_fee_items_always_get_zero(client):
    ids = make_results(client, [0, 3, 3, 3])
    resp = create_cap_list(client, ids, cap_cents=4)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["capped"] is True
    # 零费用项不参与分配；4/3 的余数相同，按下标顺序补足
    assert [item["allocated_cents"] for item in data["items"]] == [0, 2, 1, 1]
    assert data["allocated_total_cents"] == 4

    # 上限为零：所有正费用项分得零，总额精确为零
    ids = make_results(client, [0, 100])
    resp = create_cap_list(client, ids, cap_cents=0)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["capped"] is True
    assert [item["allocated_cents"] for item in data["items"]] == [0, 0]
    assert data["allocated_total_cents"] == 0

    # 全部零费用：合计为零不触发封顶
    ids = make_results(client, [0, 0])
    resp = create_cap_list(client, ids, cap_cents=100)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["capped"] is False
    assert data["allocated_total_cents"] == 0


def test_create_then_replay_returns_identical_snapshot(client):
    ids = make_results(client, [100, 100, 100])
    created = create_cap_list(client, ids, cap_cents=100).json()

    fetched = client.get(f"{CAPS_PATH}/{created['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == created

    # 再次创建其他清单不影响既有清单的回放（不可变快照）
    other = make_results(client, [50, 50])
    resp = create_cap_list(client, other, cap_cents=10)
    assert resp.status_code == 201, resp.text
    assert client.get(f"{CAPS_PATH}/{created['id']}").json() == created


def test_boundary_sizes_two_and_twenty(client):
    ids = make_results(client, [10, 20])
    resp = create_cap_list(client, ids, cap_cents=15)
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["items"]) == 2

    ids = make_results(client, [10] * 20)
    resp = create_cap_list(client, ids, cap_cents=100)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert len(data["items"]) == 20
    assert data["allocated_total_cents"] == 100


def test_duplicate_result_ids_located_422_and_nothing_persisted(client):
    a, b, c = make_results(client, [100, 200, 300])
    before = counts()

    for ids, dup_index in (
        ([a, a], 1),
        ([a, b, a], 2),
        ([a, b, c, b], 3),
    ):
        resp = create_cap_list(client, ids, cap_cents=10)
        assert resp.status_code == 422, resp.text
        payload = resp.json()
        assert "id" not in payload
        locs = ["/".join(str(p) for p in e["loc"]) for e in payload["detail"]]
        assert f"body/result_ids/{dup_index}" in locs, (ids, locs)
        assert counts() == before, "重复标识写入了清单或明细"


def test_missing_reference_404_points_to_index_and_id(client):
    a = make_result(client, 100)
    before = counts()

    resp = create_cap_list(client, [MISSING_ID, a], cap_cents=10)
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert MISSING_ID in detail
    assert "result_ids[0]" in detail

    resp = create_cap_list(client, [a, MISSING_ID], cap_cents=10)
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert MISSING_ID in detail
    assert "result_ids[1]" in detail

    assert counts() == before, "缺失引用写入了清单或明细"


def test_invalid_cap_422_and_nothing_persisted(client):
    ids = make_results(client, [100, 200])
    before = counts()

    for bad_cap in (-1, 1.5, 100.0, "100", True, None):
        resp = create_cap_list(client, ids, cap_cents=bad_cap)
        assert resp.status_code == 422, (bad_cap, resp.text)
        payload = resp.json()
        assert "id" not in payload
        locs = ["/".join(str(p) for p in e["loc"]) for e in payload["detail"]]
        assert "body/cap_cents" in locs, (bad_cap, locs)
        assert counts() == before, f"非法上限 {bad_cap!r} 写入了清单或明细"


def test_too_few_or_too_many_results_422_and_nothing_persisted(client):
    a = make_result(client, 100)
    before = counts()

    # 不足两个
    for ids in ([], [a]):
        resp = create_cap_list(client, ids, cap_cents=10)
        assert resp.status_code == 422, resp.text
        locs = ["/".join(str(p) for p in e["loc"]) for e in resp.json()["detail"]]
        assert "body/result_ids" in locs

    # 超过二十个（schema 在重复检查之前拒绝）
    resp = create_cap_list(client, [a] * 21, cap_cents=10)
    assert resp.status_code == 422, resp.text
    locs = ["/".join(str(p) for p in e["loc"]) for e in resp.json()["detail"]]
    assert "body/result_ids" in locs

    assert counts() == before, "非法结果数量写入了清单或明细"


def test_missing_fields_and_unknown_fields_422(client):
    ids = make_results(client, [100, 200])
    before = counts()

    for body in (
        {},
        {"result_ids": ids},
        {"cap_cents": 10},
        {"result_ids": ids, "cap_cents": 10, "extra": 1},
    ):
        resp = client.post(CAPS_PATH, json=body)
        assert resp.status_code == 422, resp.text
    assert counts() == before


def test_get_missing_list_returns_404(client):
    resp = client.get(f"{CAPS_PATH}/{MISSING_ID}")
    assert resp.status_code == 404
    assert MISSING_ID in resp.json()["detail"]
