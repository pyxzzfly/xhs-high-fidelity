#!/usr/bin/env bash
set -euo pipefail

# Clean old run artifacts to keep disk usage under control.
#
# Default: delete run directories older than 7 days under assets/runs (repo root).
# Override:
# - XHS_HF_OUTPUT_DIR: custom runs root
# - DAYS=30: keep last 30 days
#
# Usage:
#   scripts/cleanup_runs.sh            # delete >7 days
#   DAYS=30 scripts/cleanup_runs.sh    # delete >30 days
#   DRY_RUN=1 scripts/cleanup_runs.sh  # show what would be deleted

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNS_DIR="${XHS_HF_OUTPUT_DIR:-"$REPO_ROOT/assets/runs"}"
DAYS="${DAYS:-7}"
DRY_RUN="${DRY_RUN:-0}"

if [[ ! -d "$RUNS_DIR" ]]; then
  echo "runs dir not found: $RUNS_DIR" >&2
  exit 0
fi

echo "runs_dir=$RUNS_DIR days=$DAYS dry_run=$DRY_RUN" >&2

if [[ "$DRY_RUN" == "1" ]]; then
  find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$DAYS" -print
  exit 0
fi

find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$DAYS" -print0 | xargs -0 rm -rf --

echo "cleanup done" >&2

