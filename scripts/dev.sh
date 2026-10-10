#!/usr/bin/env bash
# 本地开发启动脚本（macOS / Linux）
#   ./scripts/dev.sh            起数据库 + 后端 + 前端
#   ./scripts/dev.sh --db-only
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_ONLY=0
[[ "${1:-}" == "--db-only" ]] && DB_ONLY=1

echo "==> 启动 PostgreSQL(pgvector)"
docker compose -f "$ROOT/docker-compose.yml" up -d db

if [[ $DB_ONLY -eq 1 ]]; then
  echo "数据库已就绪：localhost:5432 (contract_review/contract_review)"
  exit 0
fi

echo "==> 启动后端"
(
  cd "$ROOT/backend"
  uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
) &
BACKEND_PID=$!
trap 'kill $BACKEND_PID' EXIT

echo "==> 启动前端"
cd "$ROOT/frontend"
npm run dev
