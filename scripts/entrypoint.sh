#!/bin/sh
set -e

echo "等待数据库..."
python -m scripts.wait_for_db

echo "执行数据库迁移..."
alembic upgrade head

echo "启动 API 服务..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
