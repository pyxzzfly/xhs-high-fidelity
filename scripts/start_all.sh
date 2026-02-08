#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-8501}"
MATTING_PORT="${MATTING_PORT:-8911}"

# 0/1: whether to start matting-service
START_MATTING="${START_MATTING:-1}"

mkdir -p "$LOG_DIR"

if [[ ! -x "$ROOT/backend/venv/bin/uvicorn" ]]; then
  echo "[start] ERROR: backend venv missing: $ROOT/backend/venv"
  echo "[start] Hint: run scripts/bootstrap_linux.sh once (on Linux) or create venvs manually."
  exit 1
fi

if [[ ! -x "$ROOT/frontend/venv/bin/python" ]]; then
  echo "[start] ERROR: frontend venv missing: $ROOT/frontend/venv"
  exit 1
fi

if [[ "$START_MATTING" == "1" && ! -x "$ROOT/matting-service/venv/bin/uvicorn" ]]; then
  echo "[start] WARN: matting-service venv missing, skip starting matting-service"
  START_MATTING="0"
fi

if [[ "$START_MATTING" == "1" ]]; then
  echo "[start] matting-service :$MATTING_PORT"
  (
    cd "$ROOT/matting-service"
    nohup ./venv/bin/uvicorn app:app --host 127.0.0.1 --port "$MATTING_PORT" > "$LOG_DIR/matting-uvicorn.log" 2>&1 &
    echo $! > "$LOG_DIR/matting-uvicorn.pid"
  )
fi

echo "[start] backend :$BACKEND_PORT"
(
  cd "$ROOT/backend"
  nohup ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" > "$LOG_DIR/backend-uvicorn.log" 2>&1 &
  echo $! > "$LOG_DIR/backend-uvicorn.pid"
)

echo "[start] frontend :$FRONTEND_PORT"
(
  cd "$ROOT/frontend"
  nohup ./venv/bin/python -m streamlit run app.py \
    --server.address 0.0.0.0 \
    --server.port "$FRONTEND_PORT" > "$LOG_DIR/frontend-streamlit.log" 2>&1 &
  echo $! > "$LOG_DIR/frontend-streamlit.pid"
)

# Wait for readiness (best effort).
for _ in $(seq 1 40); do
  ok=1
  curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1 || ok=0
  curl -fsS "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null 2>&1 || ok=0
  if [[ "$START_MATTING" == "1" ]]; then
    curl -fsS "http://127.0.0.1:${MATTING_PORT}/health" >/dev/null 2>&1 || ok=0
  fi
  if [[ "$ok" == "1" ]]; then
    echo "[start] OK"
    echo "[start] backend  : http://127.0.0.1:${BACKEND_PORT}/health"
    echo "[start] frontend : http://127.0.0.1:${FRONTEND_PORT}"
    if [[ "$START_MATTING" == "1" ]]; then
      echo "[start] matting  : http://127.0.0.1:${MATTING_PORT}/health"
    fi
    exit 0
  fi
  sleep 0.5
done

echo "[start] ERROR: not ready, check logs in: $LOG_DIR"
exit 1

