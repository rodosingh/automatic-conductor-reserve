"""Orchestrates a run: enumerate pools/nodes -> build greedy plan -> (optionally) commit.

Dry-run is the default everywhere. `commit=True` is the only path that writes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from . import scheduler
from .conductor import ConductorClient
from .models import RunResult
from .notify import write_run_log

LOG = logging.getLogger("conductor_reserve.engine")


def _norm(hostname: str) -> str:
    """Normalize a node name/hostname for matching: first dot-label, lower-cased.
    So 'node-01.example.internal' and 'node-01' compare equal."""
    return str(hostname).strip().split(".")[0].lower()


def run(config: dict, *, commit: bool = False, client: Optional[ConductorClient] = None,
        progress=None) -> RunResult:
    """Execute one planning (and optionally committing) run.

    progress: optional callable(str) for live status lines (used by the web UI).
    """
    def emit(msg: str):
        LOG.info(msg)
        if progress:
            progress(msg)

    result = RunResult(started_at=datetime.now(timezone.utc), committed=commit)
    client = client or ConductorClient()

    # 0) identity + resolve reservation defaults (team, users)
    me = client.me()
    emit(f"authenticated as {me['email']}")
    res_cfg = config["reservation"]
    policy = config.get("policy", {})
    try:
        team_id = client.resolve_team_id(res_cfg["team_name"])
    except ValueError as e:
        result.errors.append(str(e))
        result.finished_at = datetime.now(timezone.utc)
        write_run_log(result)
        return result

    user_ids, unresolved = client.resolve_users(res_cfg.get("users", []))
    if unresolved:
        result.errors.append("could not resolve users: " + ", ".join(unresolved))
        emit("WARNING: unresolved users skipped: " + ", ".join(unresolved))

    reservation_defaults = {
        "title": res_cfg["title"],
        "project": res_cfg["project"],
        "description": res_cfg.get("description"),
        "milestone": res_cfg["milestone"],
        "team_id": team_id,
        "user_ids": user_ids,
        "batch_opt_out": res_cfg.get("batch_opt_out", True),
    }

    # 1) enumerate pools + nodes
    max_nodes = policy.get("max_nodes")
    min_gpus = int(policy.get("min_gpus", 2))
    eligible_nodes = []
    for entry in config["pools"]:
        pool = client.get_pool(entry["id"])
        result.pools.append(pool)
        tag = "eligible" if pool.eligible else f"INELIGIBLE ({pool.reason})"
        emit(f"pool {pool.name}: {tag}")
        nodes = client.list_nodes(pool, min_gpus=min_gpus)

        only = entry.get("only_nodes")
        if only:
            wanted = {_norm(x) for x in only}
            selected = [n for n in nodes if _norm(n.name) in wanted]
            found = {_norm(n.name) for n in selected}
            missing = [x for x in only if _norm(x) not in found]
            if missing:
                result.errors.append(
                    f"pool {pool.name}: only_nodes not found: {', '.join(missing)}")
                emit(f"  WARNING: only_nodes not found in pool: {', '.join(missing)}")
            emit(f"  restricted to {len(selected)}/{len(nodes)} named node(s)")
            nodes = selected

        result.nodes.extend(nodes)
        for n in nodes:
            if n.eligible:
                eligible_nodes.append((n, pool))
        emit(f"  {sum(1 for n in nodes if n.eligible)}/{len(nodes)} systems eligible")

    if max_nodes is not None:
        eligible_nodes = eligible_nodes[: int(max_nodes)]
        emit(f"capping to first {max_nodes} eligible nodes")

    # 2) build greedy plan (read-only conflict checks)
    emit(f"planning across {len(eligible_nodes)} eligible node(s)...")
    for node, pool in eligible_nodes:
        try:
            items = scheduler.plan_node(
                client, node, pool,
                reservation_defaults={**reservation_defaults},
                policy=policy, now=result.started_at,
            )
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"plan {node.name}: {e}")
            emit(f"  plan error on {node.name}: {e}")
            continue
        result.plan.extend(items)
        if items:
            emit(f"  {node.name}: {len(items)} reservation(s) planned "
                 f"({items[0].date_start:%m-%d %H:%M} -> {items[-1].date_end:%m-%d %H:%M} UTC)")

    # 2b) auto-extend the milestone/deadline to cover the furthest reservation end
    # (milestone must be >= every date_end). Keeps it at the most-future date needed.
    if result.plan:
        furthest_end = max(p.date_end for p in result.plan)
        for p in result.plan:
            if p.milestone < furthest_end:
                p.milestone = furthest_end
        emit(f"milestone set to {furthest_end:%Y-%m-%d %H:%M} UTC (covers furthest reservation)")

    # 3) commit (only if asked)
    if commit and result.plan:
        emit(f"COMMITTING {len(result.plan)} reservation(s)...")
        _commit(client, result, emit)
    elif commit:
        emit("nothing to commit")
    else:
        emit(f"dry-run complete: {len(result.plan)} reservation(s) would be created")

    result.finished_at = datetime.now(timezone.utc)
    write_run_log(result)
    return result


def cancel_small_gpu(config: dict, *, commit: bool = False,
                     client: Optional[ConductorClient] = None, progress=None) -> dict:
    """Find (and, with commit=True, cancel) OUR reservations on sub-min_gpus nodes.

    Shared by the CLI and the web app. Returns a dict with the found reservations and,
    when committed, the cancelled ids.
    """
    def emit(msg: str):
        LOG.info(msg)
        if progress:
            progress(msg)

    client = client or ConductorClient()
    policy = config.get("policy", {})
    min_gpus = int(policy.get("min_gpus", 2))
    res_cfg = config["reservation"]
    me = client.me()
    our_emails = {me["email"]}
    for u in res_cfg.get("users", []):
        if "@" in str(u):
            our_emails.add(str(u).lower())
    titles = {res_cfg["title"]}

    small = []
    for entry in config["pools"]:
        pool = client.get_pool(entry["id"])
        for n in client.list_nodes(pool, min_gpus=min_gpus):
            if n.gpu_count is not None and n.gpu_count < min_gpus:
                small.append((n.name, n.id))
    name_by_id = {nid: nm for nm, nid in small}
    found = client.find_our_reservations([nid for _, nid in small], our_emails, titles)
    for r in found:
        r["node_name"] = name_by_id.get(r["node_id"], r["node_id"])
        r["date_start"] = str(r.get("date_start"))
        r["date_end"] = str(r.get("date_end"))
    emit(f"{len(small)} node(s) below min_gpus={min_gpus}; our reservations on them: {len(found)}")

    cancelled: list[str] = []
    if commit and found:
        cancelled = client.cancel_reservations([r["id"] for r in found])
        emit(f"cancelled {len(cancelled)} reservation(s)")
    return {"min_gpus": min_gpus, "nodes_below": len(small),
            "found": found, "cancelled": cancelled, "committed": bool(commit)}


def _commit(client: ConductorClient, result: RunResult, emit) -> None:
    """Create each planned reservation individually so per-item status is unambiguous.

    One create call per reservation: a returned record => created; a 422 'overlaps' =>
    already reserved; any other error => failed with the reason. This avoids the batch
    result-matching ambiguity and reports exactly what the server did.
    """
    from collections import Counter
    per_node = Counter()
    for it in result.plan:
        payload = {
            "title": it.title, "project": it.project, "description": it.description,
            "milestone": it.milestone, "target_id": it.node_id, "team_id": it.team_id,
            "user_ids": it.user_ids, "date_start": it.date_start, "date_end": it.date_end,
            "batch_opt_out": it.batch_opt_out,
        }
        try:
            created, notes = client.create_reservations([payload])
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            it.status = "failed"
            it.message = "already reserved (overlap)" if "overlap" in msg.lower() else _short(msg)
            continue
        if created:
            it.status = "created"
            it.reservation_id = str(created[0].get("id"))
            per_node[it.node_name] += 1
        else:
            it.status = "failed"
            it.message = _flatten_notes(notes) or "not created (see server notes)"
    for node, n in per_node.items():
        emit(f"  {node}: created {n} reservation(s)")


def _short(msg: str) -> str:
    """Trim a server error to the informative part."""
    if "::" in msg:
        msg = msg.split("::", 1)[1]
    return msg.strip()[:160]


def _flatten_notes(notes: dict) -> str:
    if not notes:
        return ""
    parts = []
    for key, msgs in notes.items():
        if isinstance(msgs, list):
            parts.extend(str(m) for m in msgs)
        else:
            parts.append(f"{key}: {msgs}")
    return "; ".join(parts)
