#!/bin/bash
set -e
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; source .env; set +a; fi
if [ "${ESI_COOKIE_SECURE:-0}" != "1" ] || [[ "${ESI_PUBLIC_ORIGIN:-}" != https://* ]]; then
  echo '团队部署必须配置 HTTPS 的 ESI_PUBLIC_ORIGIN 和 ESI_COOKIE_SECURE=1'; exit 1
fi
if [ "${ESI_EXECUTION_MODE:-docker}" != "docker" ]; then
  echo '团队部署要求 ESI_EXECUTION_MODE=docker'; exit 1
fi
if [ ! -f frontend/dist/index.html ]; then
  echo '请先执行 npm --prefix frontend run build'; exit 1
fi
backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 &
API_PID=$!
(cd backend && exec .venv/bin/python -m app.worker) &
WORKER_PID=$!
trap 'kill "$API_PID" "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM
while kill -0 "$API_PID" 2>/dev/null && kill -0 "$WORKER_PID" 2>/dev/null; do sleep 1; done
echo '一个服务已退出，停止其余服务。'
exit 1
