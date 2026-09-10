"""add allowed_seconds and allowed_seconds_used

Revision ID: 0002_allowed_seconds
Revises: 0001_initial
Create Date: 2026-09-10 00:01:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_allowed_seconds"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 既有记录没有免计滞期约定：以 NOT NULL + 服务端默认 0 回填，
    # 保证旧记录的费用结果与迁移前完全一致（允许扣减为 0）。
    op.add_column(
        "demurrage_records",
        sa.Column(
            "allowed_seconds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "demurrage_records",
        sa.Column(
            "allowed_seconds_used",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("demurrage_records", "allowed_seconds_used")
    op.drop_column("demurrage_records", "allowed_seconds")
