"""create voyage_cap_lists and voyage_cap_items

Revision ID: 0003_voyage_caps
Revises: 0002_allowed_seconds
Create Date: 2026-09-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_voyage_caps"
down_revision: Union[str, None] = "0002_allowed_seconds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 航次封顶清单：不可变快照表。result_id 仅作快照引用，不加外键，
    # 使清单与结算结果的生命周期解耦（回放永远以快照为准）。
    op.create_table(
        "voyage_cap_lists",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cap_cents", sa.Integer(), nullable=False),
        sa.Column("original_total_cents", sa.Integer(), nullable=False),
        sa.Column("allocated_total_cents", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "voyage_cap_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("list_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("result_id", sa.String(length=36), nullable=False),
        sa.Column("original_cents", sa.Integer(), nullable=False),
        sa.Column("allocated_cents", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["list_id"], ["voyage_cap_lists.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_voyage_cap_items_list_id", "voyage_cap_items", ["list_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_voyage_cap_items_list_id", table_name="voyage_cap_items")
    op.drop_table("voyage_cap_items")
    op.drop_table("voyage_cap_lists")
