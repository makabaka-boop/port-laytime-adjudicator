from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
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


class VoyageCapList(Base):
    """一次航次封顶清单：创建后不可变，明细与合计均为创建时刻的快照。"""

    __tablename__ = "voyage_cap_lists"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # 赔付上限（非负整数分）与创建时刻的合计快照
    cap_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    original_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    items: Mapped[list["VoyageCapItem"]] = relationship(
        back_populates="cap_list",
        cascade="all, delete-orphan",
        order_by="VoyageCapItem.position",
    )


class VoyageCapItem(Base):
    """清单明细：按提交顺序（position）记录每个结果的原费用与分配费用。"""

    __tablename__ = "voyage_cap_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    list_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("voyage_cap_lists.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 提交顺序下标，从 0 开始；回放按此排序
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # 引用的既有结算结果标识（快照，不做外键约束）
    result_id: Mapped[str] = mapped_column(String(36), nullable=False)
    original_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    cap_list: Mapped[VoyageCapList] = relationship(back_populates="items")
