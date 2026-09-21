#!/usr/bin/env bash
# Install (or replace) the book_team.py crontab entry.
# On a schedule (default every 15 min): run_book_team_cron.sh → venv python book_team.py --commit
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${BOOK_TEAM_LOG:-$HOME/book_team.log}"
LOCK_FILE="${BOOK_TEAM_LOCK:-$HOME/.cache/automatic-conductor-reserve/book_team.lock}"
VENV="${CONDUCTOR_VENV:-$HOME/conductor-venv}"
PYTHON="${CONDUCTOR_PYTHON:-$VENV/bin/python}"
RUNNER="$REPO_DIR/run_book_team_cron.sh"

# How often to run. Set BOOK_TEAM_INTERVAL_MIN=N for an every-N-minutes cadence (default 15),
# or BOOK_TEAM_SCHEDULE="<5-field cron>" to override the whole schedule (e.g. "0 * * * *").
INTERVAL_MIN="${BOOK_TEAM_INTERVAL_MIN:-55}"
if [[ -z "${BOOK_TEAM_SCHEDULE:-}" && ! "$INTERVAL_MIN" =~ ^[0-9]+$ ]]; then
  echo "error: BOOK_TEAM_INTERVAL_MIN must be a positive integer (got '$INTERVAL_MIN')" >&2
  exit 1
fi
SCHEDULE="${BOOK_TEAM_SCHEDULE:-*/${INTERVAL_MIN} * * * *}"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: python not found or not executable: $PYTHON" >&2
  echo "set CONDUCTOR_PYTHON to the venv python (e.g. \$HOME/conductor-venv/bin/python)" >&2
  exit 1
fi

if [[ ! -f "$REPO_DIR/book_team.py" ]]; then
  echo "error: book_team.py not found in $REPO_DIR" >&2
  exit 1
fi

if [[ ! -f "$RUNNER" ]]; then
  echo "error: runner not found: $RUNNER" >&2
  exit 1
fi
chmod +x "$RUNNER"

echo "checking interpreter: $PYTHON"
"$PYTHON" - <<'PY'
import sys
print("python:", sys.executable)
print("prefix:", sys.prefix)
try:
    import conductor_sdk
except ImportError as e:
    sys.exit(f"error: {sys.executable} cannot import conductor_sdk ({e})")
print("conductor_sdk:", conductor_sdk.__file__)
PY

# Bake the interpreter into the crontab so cron does not depend on PATH or a login shell.
# flock lives inside the runner (user-owned lockfile; skipped ticks are logged).
JOB="# automatic-conductor-reserve: book assigned team nodes (${SCHEDULE})
# python env: ${PYTHON}
${SCHEDULE} CONDUCTOR_PYTHON=${PYTHON} CONDUCTOR_VENV=${VENV} BOOK_TEAM_LOCK=${LOCK_FILE} ${RUNNER} >> ${LOG_FILE} 2>&1"

# Replace any previous book_team.py job; keep everything else.
{
  crontab -l 2>/dev/null | grep -v -E 'book_team\.py|automatic-conductor-reserve: book assigned|run_book_team_cron\.sh|python env:' || true
  printf '%s\n' "$JOB"
} | crontab -

echo
echo "cron job installed (schedule: ${SCHEDULE}):"
echo "  runner: $RUNNER"
echo "  python: $PYTHON"
echo "  lock:   $LOCK_FILE"
echo "  log:    $LOG_FILE"
echo
crontab -l

if pgrep -x cron >/dev/null || pgrep -x crond >/dev/null; then
  echo
  echo "cron daemon is running."
else
  echo
  echo "warning: cron daemon is not running. start it with:"
  echo "  sudo service cron start"
fi
