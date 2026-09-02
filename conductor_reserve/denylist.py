"""Persistent denylist of nodes confirmed BROKEN by the SSH probe.

When `cancel-unhealthy --commit` releases a node we logged into and found unusable (the
`broken` class — 0/too few GPUs, no `rocm-smi`, dead docker), the node is recorded here so
future `run`/`plan` never reserve it again — even if a later probe cannot reach it (flips to
`access`), or the probe is skipped with `--no-probe`.

Only `broken` is recorded. An `access` failure is not a fault (we simply could not log in),
and an `unreachable` timeout may be transient — those stay re-checked every run rather than
permanently banned. Remove an entry with `python cli.py allow <node>` once the box is fixed.

Local state that holds real hostnames, so the file is gitignored.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DENYLIST_FILE = PROJECT_ROOT / "denylist.yaml"


def _norm(name: str) -> str:
    """Match the engine's node-name normalisation: first dot-label, lower-cased."""
    return str(name).strip().split(".")[0].lower()


def load(path: Path | str = DENYLIST_FILE) -> dict[str, dict]:
    """Return {normalized_name: {hostname, reason, cls, added}}; empty if no file yet."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return {_norm(k): v for k, v in data.items()}


def names(path: Path | str = DENYLIST_FILE) -> set:
    """Normalized names on the denylist — for fast eligibility checks."""
    return set(load(path))


def add(entries: list[dict], path: Path | str = DENYLIST_FILE) -> list[str]:
    """Add nodes to the denylist. Each entry: {name, hostname?, reason?, cls?}.

    Returns the names newly added; nodes already listed keep their original entry.
    """
    current = load(path)
    added: list[str] = []
    for e in entries:
        key = _norm(e["name"])
        if key in current:
            continue
        current[key] = {
            "hostname": e.get("hostname") or e["name"],
            "reason": e.get("reason", ""),
            "cls": e.get("cls", ""),
            "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        added.append(e["name"])
    if added:
        _write(current, path)
    return added


def remove(node: str, path: Path | str = DENYLIST_FILE) -> bool:
    """Remove one node from the denylist. Returns True if it was present."""
    current = load(path)
    if _norm(node) not in current:
        return False
    del current[_norm(node)]
    _write(current, path)
    return True


def _write(data: dict, path: Path | str = DENYLIST_FILE) -> None:
    header = (
        "# Nodes confirmed BROKEN by the SSH probe and released by "
        "`cancel-unhealthy --commit`.\n"
        "# run/plan skip these unconditionally. Remove one with "
        "`python cli.py allow <node>`.\n"
    )
    Path(path).write_text(header + yaml.safe_dump(data, sort_keys=True))
