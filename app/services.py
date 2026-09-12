"""结算业务逻辑：计算、持久化、序列化、结果对比、航次封顶清单、计费时间线。"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.intervals import (
    Calculation,
    IntervalError,
    billable_hours as billable_hours_of,
    build_timeline,
    calculate as run_calculate,
)
from app.models import DemurrageRecord, VoyageCapItem, VoyageCapList
from app.schemas import DemurrageCreate, VoyageCapCreate
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


class TimelineInconsistency(RuntimeError):
    """持久化快照无法形成连续计费时间线（409 语义）；记录保持原样。"""

    def __init__(self, result_id: str, reason: str) -> None:
        self.result_id = result_id
        super().__init__(
            f"结算结果 {result_id} 的快照无法形成连续计费时间线：{reason}"
        )


def _as_utc(value: datetime) -> datetime:
    """把持久化时间规范化为带 UTC 时区的值（SQLite 读出时可能不带时区）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_calculation_timeline(db: Session, result_id: str) -> dict | None:
    """按结果标识读取计费时间线；只读不写库。

    分段由持久化的作业边界与合并暂停扫描生成：先标记暂停，再按时间先后
    把实际抵扣秒数消耗在非暂停区间，剩余部分计费。回放前对快照做完整的
    一致性核对，任一不符即抛出 ``TimelineInconsistency``（409）：

    1. 作业起止边界时长必须等于记录的作业总秒数（快照不一致）；
    2. 合并暂停必须可形成覆盖整个作业区间的连续分段（越界/重叠等拒绝）；
    3. 暂停/允许抵扣/计费三类分段合计必须等于记录的对应秒数；
    4. 实际抵扣必须等于 ``min(约定允许秒数, 净作业秒数)``（异常抵扣拒绝）；
    5. 计费小时必须由计费秒数向上取整得到（汇总不一致）；
    6. 总分必须等于计费小时 × 费率（费用不一致）。
    """
    record = db.get(DemurrageRecord, result_id)
    if record is None:
        return None

    work_start = _as_utc(record.work_start)
    work_end = _as_utc(record.work_end)
    boundary_seconds = int((work_end - work_start).total_seconds())
    if boundary_seconds != record.work_seconds:
        raise TimelineInconsistency(
            result_id,
            f"作业起止边界时长 {boundary_seconds} 秒"
            f"与记录的作业总秒数 {record.work_seconds} 秒不符（快照不一致）",
        )

    try:
        pauses_merged = [
            (parse_utc_second(p["start"]), parse_utc_second(p["end"]))
            for p in record.pauses_merged
        ]
        segments = build_timeline(
            work_start=work_start,
            work_end=work_end,
            pauses_merged=pauses_merged,
            allowed_seconds_used=record.allowed_seconds_used,
        )
    except (IntervalError, ValueError, KeyError, TypeError) as exc:
        raise TimelineInconsistency(result_id, str(exc)) from exc

    totals = {"pause": 0, "allowed": 0, "billable": 0}
    for segment in segments:
        totals[segment.category] += segment.seconds
    persisted = {
        "pause": record.paused_seconds,
        "allowed": record.allowed_seconds_used,
        "billable": record.billable_seconds,
    }
    for category, expected in persisted.items():
        if totals[category] != expected:
            raise TimelineInconsistency(
                result_id,
                f"{category} 分段合计 {totals[category]} 秒"
                f"与记录的 {expected} 秒不符",
            )

    # 约定允许时长冲抵的是暂停后的净作业时长，实际扣减以净作业秒数为上限；
    # 实际抵扣超过约定值或上限都属于异常快照，必须拒绝回放。
    net_seconds = record.work_seconds - record.paused_seconds
    expected_allowed_used = min(record.allowed_seconds, net_seconds)
    if record.allowed_seconds_used != expected_allowed_used:
        raise TimelineInconsistency(
            result_id,
            f"实际抵扣 {record.allowed_seconds_used} 秒与约定允许"
            f" {record.allowed_seconds} 秒、净作业 {net_seconds} 秒下应扣的"
            f" {expected_allowed_used} 秒不符（异常抵扣快照）",
        )

    # 可计费秒数与计费小时必须满足“不足一小时向上取整、零秒为零小时”。
    expected_hours = billable_hours_of(record.billable_seconds)
    if record.billable_hours != expected_hours:
        raise TimelineInconsistency(
            result_id,
            f"可计费 {record.billable_seconds} 秒应折算 {expected_hours} 个"
            f"计费小时，与记录的 {record.billable_hours} 小时不符"
            f"（汇总不一致）",
        )

    # 总分必须等于计费小时 × 费率。
    expected_total = record.billable_hours * record.rate_cents_per_hour
    if record.total_cents != expected_total:
        raise TimelineInconsistency(
            result_id,
            f"{record.billable_hours} 个计费小时 × 费率"
            f" {record.rate_cents_per_hour} 分/小时应为 {expected_total} 分，"
            f"与记录的 {record.total_cents} 分不符（费用不一致）",
        )

    return {
        "id": record.id,
        "segments": [
            {
                "start": format_utc_second(segment.start),
                "end": format_utc_second(segment.end),
                "seconds": segment.seconds,
                "category": segment.category,
            }
            for segment in segments
        ],
        "paused_seconds": record.paused_seconds,
        "allowed_seconds_used": record.allowed_seconds_used,
        "billable_seconds": record.billable_seconds,
        "billable_hours": record.billable_hours,
        "total_cents": record.total_cents,
    }


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


class DuplicateResultId(ValueError):
    """清单引用的结果标识重复；``index`` 是重复出现的下标（第二次及以后）。"""

    def __init__(self, index: int, result_id: str) -> None:
        self.index = index
        self.result_id = result_id
        super().__init__(
            f"结果标识重复（result_ids[{index}]）：{result_id}"
        )


class VoyageCapTargetMissing(LookupError):
    """清单引用的结果标识不存在；``index`` 指明缺失引用所在的下标。"""

    def __init__(self, index: int, result_id: str) -> None:
        self.index = index
        self.result_id = result_id
        super().__init__(
            f"找不到下标 {index} 引用的结算结果（result_ids[{index}]）：{result_id}"
        )


def allocate_cap(originals: list[int], cap: int) -> list[int]:
    """按原费用比例把上限分配到各项（纯函数）。

    原费用合计未超过上限时逐项照录；超过时各项先取 ``cap * 原费用 / 合计``
    的整数部分，剩余整分按最大余数法逐项补足，余数相同按下标（提交顺序）
    小者优先；零费用项始终分得零。保证返回合计恰好等于
    ``min(sum(originals), cap)``。
    """
    total = sum(originals)
    if total <= cap:
        return list(originals)

    allocated = [0] * len(originals)
    quotas: list[tuple[int, int]] = []  # (余数, 下标)，仅正费用项参与
    for index, fee in enumerate(originals):
        if fee <= 0:
            continue  # 零费用项始终分得零
        numerator = cap * fee
        allocated[index] = numerator // total
        quotas.append((numerator % total, index))

    leftover = cap - sum(allocated)
    # 最大余数法：余数大者优先；余数相同按提交顺序（下标小者优先）。
    quotas.sort(key=lambda quota: (-quota[0], quota[1]))
    for _, index in quotas[:leftover]:
        allocated[index] += 1
    return allocated


def _voyage_cap_to_out(cap_list: VoyageCapList) -> dict:
    items = sorted(cap_list.items, key=lambda item: item.position)
    return {
        "id": cap_list.id,
        "cap_cents": cap_list.cap_cents,
        "original_total_cents": cap_list.original_total_cents,
        "allocated_total_cents": cap_list.allocated_total_cents,
        "capped": cap_list.original_total_cents > cap_list.cap_cents,
        "items": [
            {
                "position": item.position,
                "result_id": item.result_id,
                "original_cents": item.original_cents,
                "allocated_cents": item.allocated_cents,
            }
            for item in items
        ],
        "created_at": format_utc_second(cap_list.created_at),
    }


def create_voyage_cap_list(db: Session, payload: VoyageCapCreate) -> dict:
    """生成不可变航次封顶清单并落库。

    重复标识（422 语义）与缺失引用（404 语义）在任何写入之前抛出，
    失败请求不会留下清单或明细。
    """
    seen: dict[str, int] = {}
    for index, result_id in enumerate(payload.result_ids):
        if result_id in seen:
            raise DuplicateResultId(index, result_id)
        seen[result_id] = index

    records: list[DemurrageRecord] = []
    for index, result_id in enumerate(payload.result_ids):
        record = db.get(DemurrageRecord, result_id)
        if record is None:
            raise VoyageCapTargetMissing(index, result_id)
        records.append(record)

    originals = [record.total_cents for record in records]
    allocated = allocate_cap(originals, payload.cap_cents)

    cap_list = VoyageCapList(
        id=str(uuid.uuid4()),
        cap_cents=payload.cap_cents,
        original_total_cents=sum(originals),
        allocated_total_cents=sum(allocated),
        item_count=len(originals),
        items=[
            VoyageCapItem(
                position=index,
                result_id=record.id,
                original_cents=originals[index],
                allocated_cents=allocated[index],
            )
            for index, record in enumerate(records)
        ],
    )
    db.add(cap_list)
    db.commit()
    db.refresh(cap_list)
    return _voyage_cap_to_out(cap_list)


def get_voyage_cap_list(db: Session, list_id: str) -> dict | None:
    cap_list = db.get(VoyageCapList, list_id)
    return _voyage_cap_to_out(cap_list) if cap_list is not None else None
