"""Run logging + notifications. Chosen channels: web-UI summary + local JSONL file."""
from __future__ import annotations

import json
import logging
from datetime import timezone
from pathlib import Path

from .models import RunResult

LOG = logging.getLogger("conductor_reserve.notify")
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


def write_run_log(result: RunResult) -> Path:
    """Append one JSONL record per planned/created reservation, plus a summary line."""
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = result.started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "commit" if result.committed else "dryrun"
    path = RUNS_DIR / f"run-{stamp}-{mode}.jsonl"
    with path.open("w") as fh:
        fh.write(json.dumps({"type": "summary", **result.summary()}) + "\n")
        for pool in result.pools:
            fh.write(json.dumps({"type": "pool", **pool.__dict__}) + "\n")
        for node in result.nodes:
            fh.write(json.dumps({"type": "node", **node.__dict__}) + "\n")
        for item in result.plan:
            fh.write(json.dumps({"type": "reservation", **item.to_json()}) + "\n")
    LOG.info("run log written: %s", path)
    return path


def list_runs() -> list[dict]:
    """Return summaries of past runs, newest first (for the web UI history)."""
    RUNS_DIR.mkdir(exist_ok=True)
    out = []
    for path in sorted(RUNS_DIR.glob("run-*.jsonl"), reverse=True):
        try:
            first = path.open().readline()
            summary = json.loads(first)
            summary["file"] = path.name
            out.append(summary)
        except Exception as e:  # noqa: BLE001
            LOG.warning("could not read %s: %s", path, e)
    return out


def format_console(result: RunResult) -> str:
    """Human-readable table for CLI output."""
    s = result.summary()
    lines = []
    mode = "COMMIT (reservations created)" if result.committed else "DRY-RUN (no writes)"
    lines.append(f"=== Conductor auto-reserve — {mode} ===")
    lines.append(f"pools: {s['pools_eligible']}/{s['pools_total']} eligible | "
                 f"nodes: {s['nodes_eligible']}/{s['nodes_total']} eligible | "
                 f"reservations planned: {s['planned']}"
                 + (f" | created: {s['created']} | failed: {s['failed']}" if result.committed else ""))
    if result.errors:
        lines.append("errors: " + "; ".join(result.errors))
    lines.append("")
    header = f"{'node':30} {'pool':26} {'start (UTC)':17} {'end (UTC)':17} {'hrs':>5}  status"
    lines.append(header)
    lines.append("-" * len(header))
    for p in result.plan:
        lines.append(
            f"{p.node_name[:30]:30} {p.pool_name[:26]:26} "
            f"{p.date_start.strftime('%m-%d %H:%M'):17} {p.date_end.strftime('%m-%d %H:%M'):17} "
            f"{p.duration_hours:5.1f}  {p.status}"
            + (f" — {p.message}" if p.message else "")
        )
    if not result.plan:
        lines.append("(nothing to reserve — all eligible nodes are fully booked to the horizon)")
    return "\n".join(lines)
