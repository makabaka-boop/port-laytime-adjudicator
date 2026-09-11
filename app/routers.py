from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    DemurrageCompareRequest,
    DemurrageComparison,
    DemurrageCreate,
    DemurrageResult,
    VoyageCapCreate,
    VoyageCapListResult,
)
from app.services import (
    ComparisonTargetMissing,
    DuplicateResultId,
    VoyageCapTargetMissing,
    compare_calculations,
    create_calculation,
    create_voyage_cap_list,
    get_calculation,
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
