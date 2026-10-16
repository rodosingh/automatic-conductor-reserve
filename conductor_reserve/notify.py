"""Run logging + notifications. Chosen channels: web-UI summary + local JSONL file."""
from __future__ import annotations

import json
import logging
import os
from datetime import timezone
from pathlib import Path

from .models import RunResult

LOG = logging.getLogger("conductor_reserve.notify")
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


def write_run_log(result: RunResult) -> Path:
    """Append one JSONL record per planned/created reservation, plus a summary line.

    If BOOK_TEAM_RUN_LOG is set in the environment, append to that fixed path instead of
    minting a per-run filename. book_team.py uses this so every teammate's worker writes
    into a single combined file per cron tick (one JSONL record set per assignment) rather
    than one file each.
    """
    RUNS_DIR.mkdir(exist_ok=True)
    override = os.getenv("BOOK_TEAM_RUN_LOG")
    if override:
        path = Path(override)
        path.parent.mkdir(parents=True, exist_ok=True)
        open_mode = "a"
    else:
        stamp = result.started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        mode = "commit" if result.committed else "dryrun"
        path = RUNS_DIR / f"run-{stamp}-{mode}.jsonl"
        open_mode = "w"
    with path.open(open_mode) as fh:
        fh.write(json.dumps({"type": "summary", **result.summary()}) + "\n")
        for pool in result.pools:
            fh.write(json.dumps({"type": "pool", **pool.__dict__}) + "\n")
        for node in result.nodes:
            fh.write(json.dumps({"type": "node", **node.__dict__}) + "\n")
        for item in result.plan:
            fh.write(json.dumps({"type": "reservation", **item.to_json()}) + "\n")
    LOG.info("run log written: %s", path)
    return path


def combine_team_run_log(path: Path | str, assignments: int) -> None:
    """Rewrite a per-tick book_team file so an aggregate summary is its FIRST line.

    Each worker appended its own `summary`/`pool`/`node`/`reservation` lines. list_runs()
    only reads the first line, so without this the web history would show just one worker.
    We sum the per-worker summaries into a single team summary and prepend it; the original
    per-worker lines are kept intact after it. Best-effort: never raises.
    """
    path = Path(path)
    try:
        if not path.exists():
            return
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        summaries = []
        for ln in lines:
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "summary" and not d.get("team"):
                summaries.append(d)
        if not summaries:
            return

        def _sum(key: str) -> int:
            return sum(int(s.get(key) or 0) for s in summaries)

        starts = [s["started_at"] for s in summaries if s.get("started_at")]
        ends = [s["finished_at"] for s in summaries if s.get("finished_at")]
        errors = [e for s in summaries for e in (s.get("errors") or [])]
        agg = {
            "type": "summary",
            "team": True,
            "assignments": assignments,
            "started_at": min(starts) if starts else None,
            "finished_at": max(ends) if ends else None,
            "committed": any(bool(s.get("committed")) for s in summaries),
            "pools_total": _sum("pools_total"),
            "pools_eligible": _sum("pools_eligible"),
            "nodes_total": _sum("nodes_total"),
            "nodes_eligible": _sum("nodes_eligible"),
            "planned": _sum("planned"),
            "created": _sum("created"),
            "failed": _sum("failed"),
            "errors": errors,
        }
        path.write_text(json.dumps(agg) + "\n" + "\n".join(lines) + "\n")
    except Exception as e:  # noqa: BLE001
        LOG.warning("could not combine team run log %s: %s", path, e)


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
