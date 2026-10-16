#!/usr/bin/env bash
# Cron entrypoint: always run book_team.py --commit inside conductor-venv.
# Never rely on cron PATH (that would pick /usr/bin/python3, which has no SDK).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${CONDUCTOR_VENV:-$HOME/conductor-venv}"
PYTHON="${CONDUCTOR_PYTHON:-$VENV/bin/python}"
LOCK_FILE="${BOOK_TEAM_LOCK:-$HOME/.cache/automatic-conductor-reserve/book_team.lock}"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: python not executable: $PYTHON" >&2
  echo "set CONDUCTOR_PYTHON or CONDUCTOR_VENV" >&2
  exit 1
fi

# Same effect as `source "$VENV/bin/activate"` without depending on bash-only activate.
export VIRTUAL_ENV="$(cd "$(dirname "$PYTHON")/.." && pwd)"
export PATH="$VIRTUAL_ENV/bin:/usr/bin:/bin"
unset PYTHONHOME
# Do not inherit a foreign PYTHONPATH (cron/user env can point at another install).
unset PYTHONPATH

mkdir -p "$(dirname "$LOCK_FILE")"
cd "$REPO_DIR"

# Non-blocking: if the previous tick is still running, skip instead of stacking --commit.
exec {lock_fd}>"$LOCK_FILE"
if ! flock -n "$lock_fd"; then
  echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
  echo "skipped: previous run still holds $LOCK_FILE"
  exit 0
fi

echo "===== $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="

# Prune old run artifacts so runs/ doesn't grow without bound (default: older than 2 days).
# Kept resilient: a cleanup hiccup must never abort the booking run below.
RETENTION_DAYS="${BOOK_TEAM_RUNS_RETENTION_DAYS:-2}"
if [[ "$RETENTION_DAYS" =~ ^[0-9]+$ && -d "$REPO_DIR/runs" ]]; then
  pruned=$(find "$REPO_DIR/runs" -maxdepth 1 -type f -name '*.jsonl' \
             -mmin "+$((RETENTION_DAYS * 1440))" -print -delete 2>/dev/null | wc -l || true)
  if [[ "${pruned:-0}" -gt 0 ]]; then
    echo "pruned ${pruned} run artifact(s) older than ${RETENTION_DAYS}d from runs/"
  fi
fi

"$PYTHON" -c 'import sys, conductor_sdk; print("python:", sys.executable); print("prefix:", sys.prefix); print("conductor_sdk:", conductor_sdk.__file__)'
exec "$PYTHON" book_team.py --commit "$@"
