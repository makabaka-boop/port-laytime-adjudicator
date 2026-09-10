import os
import subprocess
import sys

import sqlalchemy as sa


def test_alembic_upgrade_and_downgrade(tmp_path):
    db_path = tmp_path / "migration.db"
    url = f"sqlite+pysqlite:///{db_path}"
    env = {**os.environ, "DATABASE_URL": url}

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True, env=env, capture_output=True, text=True,
    )
    engine = sa.create_engine(url)
    inspector = sa.inspect(engine)
    assert "demurrage_records" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("demurrage_records")}
    assert {
        "id", "work_start", "work_end", "pauses", "rate_cents_per_hour",
        "pauses_merged", "work_seconds", "paused_seconds",
        "billable_seconds", "billable_hours", "total_cents", "created_at",
    } <= columns
    engine.dispose()

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        check=True, env=env, capture_output=True, text=True,
    )
    engine = sa.create_engine(url)
    assert "demurrage_records" not in sa.inspect(engine).get_table_names()
