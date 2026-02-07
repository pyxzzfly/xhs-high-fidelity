#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"

stop_pidfile() {
  local name="$1"
  local pidfile="$LOG_DIR/$name.pid"

  if [[ ! -f "$pidfile" ]]; then
    return 0
  fi

  local pid
  pid="$(cat "$pidfile" | tr -d '[:space:]' || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$pidfile" || true
    return 0
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "[stop] $name pid=$pid"
    kill "$pid" >/dev/null 2>&1 || true
    # best-effort wait
    for _ in 1 2 3 4 5; do
      if ! kill -0 "$pid" >/dev/null 2>&1; then
        break
      fi
      sleep 0.3
    done
    if kill -0 "$pid" >/dev/null 2>&1; then
      # Only force kill if user explicitly wants it.
      if [[ "${FORCE_KILL:-0}" == "1" ]]; then
        kill -9 "$pid" >/dev/null 2>&1 || true
      fi
    fi
  fi

  rm -f "$pidfile" || true
}

stop_pidfile "frontend-streamlit"
stop_pidfile "backend-uvicorn"
stop_pidfile "matting-uvicorn"

