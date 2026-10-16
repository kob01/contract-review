#!/usr/bin/env bash
# 本地开发启动脚本（macOS / Linux / Windows Git Bash）
#   ./scripts/dev.sh                起数据库 + Redis + 后端/worker(后台) + 前端(前台)
#   ./scripts/dev.sh --db-only      只起数据库、Redis 与 Qdrant
#   ./scripts/dev.sh --no-frontend  只起数据库 + Redis + 后端（后端日志留在前台）
# Windows PowerShell 用户请改用 .\scripts\dev.ps1，本文件无法被 powershell 执行。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_ONLY=0
NO_FRONTEND=0
for arg in "$@"; do
  case "$arg" in
    --db-only) DB_ONLY=1 ;;
    --no-frontend) NO_FRONTEND=1 ;;
    *) echo "未知参数：$arg（可用 --db-only / --no-frontend）" >&2; exit 2 ;;
  esac
done

# 在仓库根目录执行 compose，保证能读到根目录的 .env（PGVECTOR_IMAGE / DB_PORT / REDIS_IMAGE / QDRANT_IMAGE）
echo "==> 启动 PostgreSQL、Redis(队列) 与 Qdrant(向量)"
cd "$ROOT"
docker compose up -d db redis qdrant

if [[ $DB_ONLY -eq 1 ]]; then
  docker compose ps db redis qdrant
  # 实际端口取自 .env 的 DB_PORT（没配则用 compose 默认的 5433），避免提示信息里写错端口
  PORT="$(grep -E '^[[:space:]]*DB_PORT[[:space:]]*=' "$ROOT/.env" 2>/dev/null | tail -n1 | cut -d= -f2 | tr -dc '0-9' || true)"
  echo "数据库已就绪：localhost:${PORT:-5433} (contract_review/contract_review)"
  echo "Redis 已就绪：localhost:6379（队列后端）"
  exit 0
fi

# 审查任务已交给独立 worker 进程：不起 worker 的话，任务会一直排在队列里
echo "==> 启动审查 worker（后台）"
(
  cd "$ROOT/backend"
  uv run python -m app.tasks.worker
) &
WORKER_PID=$!

if [[ $NO_FRONTEND -eq 1 ]]; then
  echo "==> 启动后端（前台，Ctrl+C 结束）"
  trap 'kill $WORKER_PID 2>/dev/null || true' EXIT
  cd "$ROOT/backend"
  exec uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
fi

echo "==> 启动后端"
(
  cd "$ROOT/backend"
  uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
) &
BACKEND_PID=$!
trap 'kill $BACKEND_PID $WORKER_PID 2>/dev/null || true' EXIT

echo "==> 启动前端"
cd "$ROOT/frontend"
npm run dev
