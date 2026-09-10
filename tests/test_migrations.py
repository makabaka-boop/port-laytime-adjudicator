import os
import subprocess
import sys

import sqlalchemy as sa


def _alembic(*args: str, url: str) -> None:
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        check=True, env=env, capture_output=True, text=True,
    )


EXPECTED_COLUMNS = {
    "id", "work_start", "work_end", "pauses", "rate_cents_per_hour",
    "pauses_merged", "work_seconds", "paused_seconds",
    "allowed_seconds", "allowed_seconds_used",
    "billable_seconds", "billable_hours", "total_cents", "created_at",
}


def test_alembic_upgrade_and_downgrade(tmp_path):
    db_path = tmp_path / "migration.db"
    url = f"sqlite+pysqlite:///{db_path}"

    _alembic("upgrade", "head", url=url)
    engine = sa.create_engine(url)
    inspector = sa.inspect(engine)
    assert "demurrage_records" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("demurrage_records")}
    assert EXPECTED_COLUMNS <= columns
    engine.dispose()

    _alembic("downgrade", "base", url=url)
    engine = sa.create_engine(url)
    assert "demurrage_records" not in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_0002_backfills_zero_allowance_for_existing_rows(tmp_path):
    """升级到 0001 后写入一条历史记录，再升级 0002，允许时长必须回填为 0，
    且原费用结果不变；回滚 0002 后两列消失。"""
    db_path = tmp_path / "backfill.db"
    url = f"sqlite+pysqlite:///{db_path}"

    _alembic("upgrade", "0001_initial", url=url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO demurrage_records (
                    id, work_start, work_end, pauses, rate_cents_per_hour,
                    pauses_merged, work_seconds, paused_seconds,
                    billable_seconds, billable_hours, total_cents, created_at
                ) VALUES (
                    'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                    '2026-09-10 00:00:00+00:00',
                    '2026-09-10 08:00:00+00:00',
                    '[]', 100, '[]', 28800, 0, 28800, 8, 800,
                    '2026-09-10 00:00:00+00:00'
                )
                """
            )
        )
    engine.dispose()

    _alembic("upgrade", "head", url=url)
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                """
                SELECT allowed_seconds, allowed_seconds_used,
                       billable_seconds, total_cents
                FROM demurrage_records
                WHERE id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
                """
            )
        ).one()
    assert row.allowed_seconds == 0
    assert row.allowed_seconds_used == 0
    assert row.billable_seconds == 28800
    assert row.total_cents == 800
    engine.dispose()

    # 降级 0002 后列消失，历史记录仍在（回到 0001 结构）。
    _alembic("downgrade", "0001_initial", url=url)
    engine = sa.create_engine(url)
    columns = {
        c["name"]
        for c in sa.inspect(engine).get_columns("demurrage_records")
    }
    assert "allowed_seconds" not in columns
    assert "allowed_seconds_used" not in columns
    with engine.connect() as conn:
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM demurrage_records")
        ).scalar_one()
    assert count == 1
    engine.dispose()
