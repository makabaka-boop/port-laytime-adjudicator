"""create event_logs

Revision ID: 0004_event_logs
Revises: 0003_voyage_caps
Create Date: 2026-09-12 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_event_logs"
down_revision: Union[str, None] = "0003_voyage_caps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 交接班事件簿：原始事件 + 编译摘要 + 派生暂停 + 关联结果标识。
    # result_id 仅作快照引用，不加外键，使事件簿与结算结果的生命周期解耦
    # （回放永远以持久化的原始事件与关联标识为准）。
    op.create_table(
        "event_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("compilation_summary", sa.JSON(), nullable=False),
        sa.Column("rate_cents_per_hour", sa.Integer(), nullable=False),
        sa.Column("allowed_seconds", sa.Integer(), nullable=False),
        sa.Column("pauses_derived", sa.JSON(), nullable=False),
        sa.Column("result_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_event_logs_result_id", "event_logs", ["result_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_event_logs_result_id", table_name="event_logs")
    op.drop_table("event_logs")
