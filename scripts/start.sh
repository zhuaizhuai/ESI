#!/bin/bash
set -e
cd "$(dirname "$0")/.."
if [ ! -x backend/.venv/bin/python ]; then
  echo '请先执行 python3 -m venv backend/.venv 并安装依赖'; exit 1
fi
if [ -f .env ]; then set -a; source .env; set +a; fi
mkdir -p runtime
backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 &
API_PID=$!
(cd backend && exec .venv/bin/python -m app.worker) &
WORKER_PID=$!
(cd frontend && exec node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173 --strictPort) &
FRONT_PID=$!
trap 'kill "$API_PID" "$WORKER_PID" "$FRONT_PID" 2>/dev/null || true' EXIT INT TERM
while kill -0 "$API_PID" 2>/dev/null && kill -0 "$WORKER_PID" 2>/dev/null && kill -0 "$FRONT_PID" 2>/dev/null; do
  sleep 1
done
echo '一个服务已经退出，正在停止其余服务。'
exit 1
