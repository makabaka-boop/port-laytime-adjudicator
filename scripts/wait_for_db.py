"""等待 PostgreSQL 可连接，供容器入口使用。"""

import sys
import time

from sqlalchemy import create_engine, text

from app.config import get_settings


def main() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    deadline = time.time() + 60
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("database is ready")
            return
        except Exception as exc:  # 数据库尚未就绪
            if time.time() >= deadline:
                print(f"等待数据库超时: {exc}", file=sys.stderr)
                sys.exit(1)
            time.sleep(1)


if __name__ == "__main__":
    main()
