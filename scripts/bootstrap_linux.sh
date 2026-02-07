#!/usr/bin/env bash
set -euo pipefail

# One-click bootstrap for a single-machine deployment (no Docker).
# Tested intent: Ubuntu 22.04/24.04 class machines with outbound network access.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
MATTING_DIR="$ROOT/matting-service"
LOG_DIR="$ROOT/logs"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-8501}"
MATTING_PORT="${MATTING_PORT:-8911}"

# 0/1
ENABLE_MATTING="${ENABLE_MATTING:-0}"

mkdir -p "$LOG_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[bootstrap] ERROR: python3 not found"
  exit 1
fi

echo "[bootstrap] repo=$ROOT"

# Stop previous processes if pid files exist.
if [[ -x "$ROOT/scripts/stop_all.sh" ]]; then
  "$ROOT/scripts/stop_all.sh" || true
fi

echo "[bootstrap] backend venv + deps..."
if [[ ! -d "$BACKEND_DIR/venv" ]]; then
  python3 -m venv "$BACKEND_DIR/venv"
fi
"$BACKEND_DIR/venv/bin/pip" install -U pip
"$BACKEND_DIR/venv/bin/pip" install -r "$BACKEND_DIR/requirements.txt"

echo "[bootstrap] playwright chromium..."
"$BACKEND_DIR/venv/bin/python" -m playwright install --with-deps chromium

if [[ ! -f "$BACKEND_DIR/.env" ]]; then
  cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
  echo "[bootstrap] created backend/.env from .env.example (please edit keys if you need rewrite/generate)"
fi

echo "[bootstrap] frontend venv + deps..."
if [[ ! -d "$FRONTEND_DIR/venv" ]]; then
  python3 -m venv "$FRONTEND_DIR/venv"
fi
"$FRONTEND_DIR/venv/bin/pip" install -U pip
"$FRONTEND_DIR/venv/bin/pip" install -r "$FRONTEND_DIR/requirements.txt"

if [[ "$ENABLE_MATTING" == "1" ]]; then
  echo "[bootstrap] matting-service venv + deps..."
  if [[ ! -d "$MATTING_DIR/venv" ]]; then
    python3 -m venv "$MATTING_DIR/venv"
  fi
  "$MATTING_DIR/venv/bin/pip" install -U pip
  "$MATTING_DIR/venv/bin/pip" install -r "$MATTING_DIR/requirements.txt"

  echo "[bootstrap] starting matting-service :$MATTING_PORT ..."
  nohup "$MATTING_DIR/venv/bin/uvicorn" app:app --host 127.0.0.1 --port "$MATTING_PORT" > "$LOG_DIR/matting-uvicorn.log" 2>&1 &
  echo $! > "$LOG_DIR/matting-uvicorn.pid"
fi

echo "[bootstrap] starting backend :$BACKEND_PORT ..."
nohup "$BACKEND_DIR/venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" > "$LOG_DIR/backend-uvicorn.log" 2>&1 &
echo $! > "$LOG_DIR/backend-uvicorn.pid"

echo "[bootstrap] starting frontend :$FRONTEND_PORT ..."
nohup "$FRONTEND_DIR/venv/bin/python" -m streamlit run "$FRONTEND_DIR/app.py" \
  --server.address 0.0.0.0 \
  --server.port "$FRONTEND_PORT" > "$LOG_DIR/frontend-streamlit.log" 2>&1 &
echo $! > "$LOG_DIR/frontend-streamlit.pid"

echo
echo "[bootstrap] OK"
echo "[bootstrap] backend  : http://127.0.0.1:$BACKEND_PORT/health"
echo "[bootstrap] frontend : http://127.0.0.1:$FRONTEND_PORT"
echo "[bootstrap] logs     : $LOG_DIR"

