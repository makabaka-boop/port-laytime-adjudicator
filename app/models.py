from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base


class DemurrageRecord(Base):
    """一次成功的滞期费结算。非法输入在进入此表之前即被拒绝。"""

    __tablename__ = "demurrage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # 原始输入
    work_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    work_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 形如 [{"start": "..", "end": ".."}]
    pauses: Mapped[list] = mapped_column(JSON, nullable=False)
    rate_cents_per_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    # 租约约定的免计滞期允许秒数；历史记录由迁移回填为 0。
    allowed_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 计算结果
    pauses_merged: Mapped[list] = mapped_column(JSON, nullable=False)
    work_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    paused_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # 实际扣减的允许秒数（以净作业秒数为上限，allowed_seconds 超出时小于约定值）
    allowed_seconds_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    billable_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    billable_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
