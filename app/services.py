"""结算业务逻辑：计算、持久化、序列化。"""

import uuid

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
    )


def persist(db: Session, payload: DemurrageCreate, result: Calculation) -> dict:
    """落库并返回可序列化结果。仅在计算成功后调用。"""
    record = DemurrageRecord(
        id=str(uuid.uuid4()),
        work_start=result.work_start,
        work_end=result.work_end,
        pauses=[{"start": p.start, "end": p.end} for p in payload.pauses],
        rate_cents_per_hour=result.rate_cents_per_hour,
        pauses_merged=[
            {"start": format_utc_second(s), "end": format_utc_second(e)}
            for s, e in result.pauses_merged
        ],
        work_seconds=result.work_seconds,
        paused_seconds=result.paused_seconds,
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
