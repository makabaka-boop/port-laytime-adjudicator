from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.eventbook import EventCompileError
from app.intervals import IntervalError
from app.schemas import (
    DemurrageCompareRequest,
    DemurrageComparison,
    DemurrageCreate,
    DemurrageResult,
    DemurrageTimeline,
    EventLogCreate,
    EventLogResult,
    VoyageCapCreate,
    VoyageCapListResult,
)
from app.services import (
    ComparisonTargetMissing,
    DuplicateResultId,
    EventLogResultMissing,
    TimelineInconsistency,
    VoyageCapTargetMissing,
    compare_calculations,
    create_calculation,
    create_event_log,
    create_voyage_cap_list,
    get_calculation,
    get_calculation_timeline,
    get_event_log,
    get_voyage_cap_list,
)

router = APIRouter(prefix="/api/v1/demurrage", tags=["demurrage"])


@router.post(
    "/calculations",
    response_model=DemurrageResult,
    status_code=status.HTTP_201_CREATED,
)
def calculate_demurrage(
    payload: DemurrageCreate, db: Session = Depends(get_db)
) -> dict:
    return create_calculation(db, payload)


@router.get("/calculations/{result_id}", response_model=DemurrageResult)
def read_calculation(
    result_id: str, db: Session = Depends(get_db)
) -> dict:
    result = get_calculation(db, result_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"找不到结算结果：{result_id}",
        )
    return result


@router.get(
    "/calculations/{result_id}/timeline",
    response_model=DemurrageTimeline,
)
def read_calculation_timeline(
    result_id: str, db: Session = Depends(get_db)
) -> dict:
    try:
        result = get_calculation_timeline(db, result_id)
    except TimelineInconsistency as exc:
        # 持久化快照不自洽：可识别的 409 数据一致性错误，记录保持原样。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"找不到结算结果：{result_id}",
        )
    return result


@router.post("/comparisons", response_model=DemurrageComparison)
def compare_demurrage(
    payload: DemurrageCompareRequest, db: Session = Depends(get_db)
) -> dict:
    try:
        return compare_calculations(db, payload.base_id, payload.candidate_id)
    except ComparisonTargetMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/voyage-caps",
    response_model=VoyageCapListResult,
    status_code=status.HTTP_201_CREATED,
)
def create_voyage_cap(
    payload: VoyageCapCreate, db: Session = Depends(get_db)
) -> dict:
    try:
        return create_voyage_cap_list(db, payload)
    except DuplicateResultId as exc:
        # 与 schema 校验一致的 422 结构，loc 定位到重复出现的下标。
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "result_ids", exc.index),
                    "msg": str(exc),
                    "type": "value_error",
                }
            ]
        ) from exc
    except VoyageCapTargetMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.get("/voyage-caps/{list_id}", response_model=VoyageCapListResult)
def read_voyage_cap(
    list_id: str, db: Session = Depends(get_db)
) -> dict:
    result = get_voyage_cap_list(db, list_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"找不到航次封顶清单：{list_id}",
        )
    return result


def _raise_event_compile_422(exc: EventCompileError) -> None:
    """事件簿编译失败：与 schema 校验一致的 422 结构，loc 定位到事件下标。"""
    raise RequestValidationError(
        [
            {
                "loc": ("body", "events", exc.index),
                "msg": str(exc),
                "type": "value_error",
            }
        ]
    ) from exc


@router.post(
    "/event-logs",
    response_model=EventLogResult,
    status_code=status.HTTP_201_CREATED,
)
def create_event_log_entry(
    payload: EventLogCreate, db: Session = Depends(get_db)
) -> dict:
    try:
        return create_event_log(db, payload)
    except EventCompileError as exc:
        # 首尾/配对/严格递增非法转换：422 定位到事件下标，且已整体回滚。
        _raise_event_compile_422(exc)
    except IntervalError as exc:
        # 计费失败同样不得留下事件簿或结算记录（事务已回滚）。
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "events"),
                    "msg": f"事件簿计费失败：{exc}",
                    "type": "value_error",
                }
            ]
        ) from exc


@router.get("/event-logs/{event_log_id}", response_model=EventLogResult)
def read_event_log(
    event_log_id: str, db: Session = Depends(get_db)
) -> dict:
    try:
        result = get_event_log(db, event_log_id)
    except EventLogResultMissing as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"找不到事件簿：{event_log_id}",
        )
    return result
