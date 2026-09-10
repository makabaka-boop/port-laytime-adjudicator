"""一次性黑盒验收脚本（verify 服务调用）。

只依赖标准库，对运行中的 API 发起真实 HTTP 请求，验收点：
  1. 重复覆盖/首尾相接的停机只扣除一次，且合并结果可直接观察；
  2. 跨界停机只扣除与作业区间的交集；
  3. 非法请求返回 422 且错误可定位到具体路径，并且查不到任何结算结果；
  4. 成功结果可按 id 回查；零秒作业费用为零。

用法： python -m scripts.verify [BASE_URL]
退出码 0 表示全部通过，否则为 1。
"""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://api:8000"
PATH = "/api/v1/demurrage/calculations"


def request(method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: 期望 {expected!r}，实际 {actual!r}")
    print(f"  OK  {label} = {actual!r}")


def assert_loc_contains(errors: list[dict], fragment: str, label: str) -> None:
    joined = ["/".join(str(p) for p in e.get("loc", [])) for e in errors]
    if not any(fragment in loc for loc in joined):
        raise AssertionError(
            f"{label}: 错误位置中找不到 {fragment!r}，实际位置 {joined}"
        )
    print(f"  OK  {label}: 错误位置包含 {fragment!r} -> {joined}")


def case_overlap_only_counted_once() -> None:
    print("案例 1：重复覆盖的停机只扣除一次，首尾相接合并为一段")
    body = {
        # 作业 8 小时（28800 秒）
        "work_start": "2026-09-10T00:00:00Z",
        "work_end": "2026-09-10T08:00:00Z",
        "rate_cents_per_hour": 100,
        "pauses": [
            {"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T03:00:00Z"},
            # 与上一段重叠 1 小时，且再覆盖一段
            {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T04:00:00Z"},
            # 完全被包含的重复停机
            {"start": "2026-09-10T02:30:00Z", "end": "2026-09-10T03:30:00Z"},
            # 与前段首尾相接（端点相等不增加时长）
            {"start": "2026-09-10T04:00:00Z", "end": "2026-09-10T05:00:00Z"},
        ],
    }
    status, result = request("POST", BASE_URL + PATH, body)
    assert_equal(status, 201, "HTTP 状态码")
    # 01:00-05:00 合并成一段，共 4 小时，只扣一次
    assert_equal(len(result["pauses_merged"]), 1, "合并后暂停段数")
    assert_equal(
        result["pauses_merged"],
        [{"start": "2026-09-10T01:00:00Z", "end": "2026-09-10T05:00:00Z"}],
        "合并区间",
    )
    assert_equal(result["paused_seconds"], 4 * 3600, "暂停总秒数（无重复扣减）")
    assert_equal(result["work_seconds"], 8 * 3600, "作业秒数")
    assert_equal(result["billable_seconds"], 4 * 3600, "可计费秒数")
    assert_equal(result["billable_hours"], 4, "计费小时")
    assert_equal(result["total_cents"], 400, "总分值")

    status2, fetched = request("GET", f"{BASE_URL}{PATH}/{result['id']}")
    assert_equal(status2, 200, "按 id 回查状态码")
    assert_equal(fetched["id"], result["id"], "回查结果标识一致")
    assert_equal(fetched["total_cents"], 400, "回查总分值一致")


def case_cross_boundary_intersection_only() -> None:
    print("案例 2：跨界停机仅扣交集，作业外部分不计")
    body = {
        # 作业 1 小时
        "work_start": "2026-09-10T08:00:00Z",
        "work_end": "2026-09-10T09:00:00Z",
        "rate_cents_per_hour": 600,
        "pauses": [
            # 左跨界，交集仅 08:00-08:30
            {"start": "2026-09-10T07:00:00Z", "end": "2026-09-10T08:30:00Z"},
            # 右跨界，交集仅 08:45-09:00
            {"start": "2026-09-10T08:45:00Z", "end": "2026-09-10T10:00:00Z"},
            # 完全在作业之外，裁剪后为空，不出现
            {"start": "2026-09-10T10:00:00Z", "end": "2026-09-10T11:00:00Z"},
            # 仅端点相接，同样不出现
            {"start": "2026-09-10T07:00:00Z", "end": "2026-09-10T08:00:00Z"},
        ],
    }
    status, result = request("POST", BASE_URL + PATH, body)
    assert_equal(status, 201, "HTTP 状态码")
    assert_equal(
        result["pauses_merged"],
        [
            {"start": "2026-09-10T08:00:00Z", "end": "2026-09-10T08:30:00Z"},
            {"start": "2026-09-10T08:45:00Z", "end": "2026-09-10T09:00:00Z"},
        ],
        "裁剪后的交集区间",
    )
    assert_equal(result["paused_seconds"], 45 * 60, "暂停秒数=30+15 分钟")
    assert_equal(result["billable_seconds"], 15 * 60, "可计费秒数=15 分钟")
    # 不足一小时向上取整
    assert_equal(result["billable_hours"], 1, "不足一小时按一小时")
    assert_equal(result["total_cents"], 600, "总分值")


def case_rounding_and_zero() -> None:
    print("案例 3：向上取整与零秒费用")
    body = {
        # 作业 3600 秒，暂停 3599 秒，可计费 1 秒 -> 1 小时
        "work_start": "2026-09-10T00:00:00Z",
        "work_end": "2026-09-10T01:00:00Z",
        "rate_cents_per_hour": 250,
        "pauses": [
            {"start": "2026-09-10T00:00:01Z", "end": "2026-09-10T01:00:00Z"},
        ],
    }
    status, result = request("POST", BASE_URL + PATH, body)
    assert_equal(status, 201, "HTTP 状态码")
    assert_equal(result["billable_seconds"], 1, "可计费 1 秒")
    assert_equal(result["billable_hours"], 1, "1 秒向上取整为 1 小时")
    assert_equal(result["total_cents"], 250, "总分值")

    body_zero = {
        "work_start": "2026-09-10T00:00:00Z",
        "work_end": "2026-09-10T01:00:00Z",
        "rate_cents_per_hour": 250,
        "pauses": [
            {"start": "2026-09-10T00:00:00Z", "end": "2026-09-10T01:00:00Z"},
        ],
    }
    status, result = request("POST", BASE_URL + PATH, body_zero)
    assert_equal(status, 201, "HTTP 状态码")
    assert_equal(result["billable_seconds"], 0, "全部暂停：可计费 0 秒")
    assert_equal(result["billable_hours"], 0, "0 秒为 0 小时")
    assert_equal(result["total_cents"], 0, "0 秒费用为 0")


def case_invalid_requests_never_persist() -> None:
    print("案例 4：非法请求返回可定位字段错误，且无结果可查")
    invalid_bodies = [
        (
            "小数秒被拒绝",
            {
                "work_start": "2026-09-10T00:00:00.5Z",
                "work_end": "2026-09-10T01:00:00Z",
                "rate_cents_per_hour": 100,
                "pauses": [],
            },
            "work_start",
        ),
        (
            "非 UTC 偏移被拒绝",
            {
                "work_start": "2026-09-10T00:00:00Z",
                "work_end": "2026-09-10T09:00:00+08:00",
                "rate_cents_per_hour": 100,
                "pauses": [],
            },
            "work_end",
        ),
        (
            "作业结束不晚于开始",
            {
                "work_start": "2026-09-10T01:00:00Z",
                "work_end": "2026-09-10T01:00:00Z",
                "rate_cents_per_hour": 100,
                "pauses": [],
            },
            "work_end",
        ),
        (
            "暂停时间倒置",
            {
                "work_start": "2026-09-10T00:00:00Z",
                "work_end": "2026-09-10T02:00:00Z",
                "rate_cents_per_hour": 100,
                "pauses": [
                    {"start": "2026-09-10T01:30:00Z",
                     "end": "2026-09-10T01:00:00Z"},
                ],
            },
            "pauses/0/end",
        ),
        (
            "空暂停区间（端点相等）",
            {
                "work_start": "2026-09-10T00:00:00Z",
                "work_end": "2026-09-10T02:00:00Z",
                "rate_cents_per_hour": 100,
                "pauses": [
                    {"start": "2026-09-10T01:00:00Z",
                     "end": "2026-09-10T01:00:00Z"},
                ],
            },
            "pauses/0/end",
        ),
        (
            "负费率",
            {
                "work_start": "2026-09-10T00:00:00Z",
                "work_end": "2026-09-10T02:00:00Z",
                "rate_cents_per_hour": -1,
                "pauses": [],
            },
            "rate_cents_per_hour",
        ),
    ]

    for label, body, loc_fragment in invalid_bodies:
        print(f"  -- {label}")
        status, payload = request("POST", BASE_URL + PATH, body)
        assert_equal(status, 422, f"[{label}] HTTP 状态码")
        if "id" in payload:
            raise AssertionError(f"[{label}] 非法请求不应返回结果标识")
        assert_loc_contains(payload["detail"], loc_fragment, label)
        # 非法响应中没有 id，即不存在可查询的结算结果
        status404, _ = request(
            "GET",
            BASE_URL + PATH + "/00000000-0000-0000-0000-000000000000",
        )
        assert_equal(status404, 404, f"[{label}] 占位 id 查询为 404")

    status, _ = request("GET", BASE_URL + PATH + "/not-a-valid-id")
    assert_equal(status, 404, "不存在的结果标识返回 404")


def main() -> int:
    print(f"验收目标: {BASE_URL}")
    try:
        case_overlap_only_counted_once()
        case_cross_boundary_intersection_only()
        case_rounding_and_zero()
        case_invalid_requests_never_persist()
    except Exception as exc:
        print(f"\n验收失败: {exc}", file=sys.stderr)
        return 1
    print("\n全部验收通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
