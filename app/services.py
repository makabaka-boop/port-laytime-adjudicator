"""结算业务逻辑：计算、持久化、序列化、结果对比。"""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.intervals import Calculation, IntervalError, calculate as run_calculate
from app.models import DemurrageRecord
from app.schemas import DemurrageCreate
from app.timeparse import format_utc_second, parse_utc_second


def _to_out(record: DemurrageRecord) -> dict:
    return {
        "id": record.id,
        "work_start": format_utc_second(record.work_start),
        "work_end": format_utc_second(record.work_end),
        "pauses": record.pauses,
        "pauses_merged": record.pauses_merged,
        "rate_cents_per_hour": record.rate_cents_per_hour,
        "allowed_seconds": record.allowed_seconds,
        "allowed_seconds_used": record.allowed_seconds_used,
        "work_seconds": record.work_seconds,
        "paused_seconds": record.paused_seconds,
        "billable_seconds": record.billable_seconds,
        "billable_hours": record.billable_hours,
        "total_cents": record.total_cents,
        "created_at": format_utc_second(record.created_at),
    }


def compute(payload: DemurrageCreate) -> Calculation:
    """把已通过 schema 校验的请求重新解析为 datetime 并执行纯计算。"""
    work_start = parse_utc_second(payload.work_start)
    work_end = parse_utc_second(payload.work_end)
    raw_pauses = [
        (parse_utc_second(p.start), parse_utc_second(p.end)) for p in payload.pauses
    ]
    return run_calculate(
        work_start=work_start,
        work_end=work_end,
        raw_pauses=raw_pauses,
        rate_cents_per_hour=payload.rate_cents_per_hour,
        allowed_seconds=payload.allowed_seconds,
    )


def persist(db: Session, payload: DemurrageCreate, result: Calculation) -> dict:
    """落库并返回可序列化结果。仅在计算成功后调用。"""
    record = DemurrageRecord(
        id=str(uuid.uuid4()),
        work_start=result.work_start,
        work_end=result.work_end,
        pauses=[{"start": p.start, "end": p.end} for p in payload.pauses],
        rate_cents_per_hour=result.rate_cents_per_hour,
        allowed_seconds=result.allowed_seconds,
        pauses_merged=[
            {"start": format_utc_second(s), "end": format_utc_second(e)}
            for s, e in result.pauses_merged
        ],
        work_seconds=result.work_seconds,
        paused_seconds=result.paused_seconds,
        allowed_seconds_used=result.allowed_seconds_used,
        billable_seconds=result.billable_seconds,
        billable_hours=result.billable_hours,
        total_cents=result.total_cents,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_out(record)


def create_calculation(db: Session, payload: DemurrageCreate) -> dict:
    # 服务层再校验一次：任何倒置/空暂停/负费率都不会写入记录。
    result = compute(payload)
    if result.billable_seconds < 0 or result.total_cents < 0:
        raise IntervalError("计算结果不合法")
    return persist(db, payload, result)


def get_calculation(db: Session, result_id: str) -> dict | None:
    record = db.get(DemurrageRecord, result_id)
    return _to_out(record) if record is not None else None


class ComparisonTargetMissing(LookupError):
    """对比引用的结果标识不存在；``field`` 指明缺失的是基准还是候选。"""

    def __init__(self, field: str, result_id: str) -> None:
        self.field = field
        self.result_id = result_id
        label = "基准" if field == "base_id" else "候选"
        super().__init__(f"找不到{label}结算结果（{field}）：{result_id}")


def _sorted_pause_keys(pauses: list[dict]) -> list[tuple[datetime, datetime]]:
    """把持久化的暂停列表规范化为按时间排序的键。

    解析为时间值后排序比较，仅提交顺序或记法（Z / +00:00）不同
    的相同区间不会产生虚假差异。
    """
    keys = [
        (parse_utc_second(p["start"]), parse_utc_second(p["end"])) for p in pauses
    ]
    return sorted(keys)


def compare_calculations(db: Session, base_id: str, candidate_id: str) -> dict:
    """对比两条已持久化结果，只读不写库。

    差异以序列化后的持久化值为准；暂停列表先按时间排序再比较。
    增减值 = 候选 − 基准（有符号）。两个标识相同时自然得到空差异与全零增减。
    """
    base = db.get(DemurrageRecord, base_id)
    if base is None:
        raise ComparisonTargetMissing("base_id", base_id)
    candidate = db.get(DemurrageRecord, candidate_id)
    if candidate is None:
        raise ComparisonTargetMissing("candidate_id", candidate_id)

    base_out = _to_out(base)
    candidate_out = _to_out(candidate)

    changes = {
        "work_start": base_out["work_start"] != candidate_out["work_start"],
        "work_end": base_out["work_end"] != candidate_out["work_end"],
        "pauses": _sorted_pause_keys(base_out["pauses"])
        != _sorted_pause_keys(candidate_out["pauses"]),
        "pauses_merged": _sorted_pause_keys(base_out["pauses_merged"])
        != _sorted_pause_keys(candidate_out["pauses_merged"]),
        "rate_cents_per_hour": base_out["rate_cents_per_hour"]
        != candidate_out["rate_cents_per_hour"],
        "allowed_seconds": base_out["allowed_seconds"]
        != candidate_out["allowed_seconds"],
    }
    deltas = {
        field: candidate_out[field] - base_out[field]
        for field in (
            "paused_seconds",
            "billable_seconds",
            "billable_hours",
            "total_cents",
        )
    }
    return {
        "base_id": base_id,
        "candidate_id": candidate_id,
        "changes": changes,
        "deltas": deltas,
    }
