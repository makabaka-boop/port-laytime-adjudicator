"""create demurrage_records

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-10 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "demurrage_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("work_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pauses", sa.JSON(), nullable=False),
        sa.Column("rate_cents_per_hour", sa.Integer(), nullable=False),
        sa.Column("pauses_merged", sa.JSON(), nullable=False),
        sa.Column("work_seconds", sa.Integer(), nullable=False),
        sa.Column("paused_seconds", sa.Integer(), nullable=False),
        sa.Column("billable_seconds", sa.Integer(), nullable=False),
        sa.Column("billable_hours", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("demurrage_records")
