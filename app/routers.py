from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import DemurrageCreate, DemurrageResult
from app.services import create_calculation, get_calculation

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
