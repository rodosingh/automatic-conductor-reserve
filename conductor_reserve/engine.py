"""Orchestrates a run: enumerate pools/nodes -> build greedy plan -> (optionally) commit.

Dry-run is the default everywhere. `commit=True` is the only path that writes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from . import probe, scheduler
from .conductor import ConductorClient
from .models import RunResult
from .notify import write_run_log

LOG = logging.getLogger("conductor_reserve.engine")


def _norm(hostname: str) -> str:
    """Normalize a node name/hostname for matching: first dot-label, lower-cased.
    So 'node-01.example.internal' and 'node-01' compare equal."""
    return str(hostname).strip().split(".")[0].lower()


def _merge_iv(intervals):
    """Merge overlapping (start, end) intervals; return sorted list."""
    intervals = sorted(intervals)
    merged = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _enumerate_nodes(client: ConductorClient, config: dict, emit):
    """Every configured pool's systems, with each pool's `only_nodes` filter applied.

    Returns (pairs, pools, nodes, errors); `pairs` are the (node, pool) tuples that pass
    Conductor-side eligibility. Shared by `run` and `cancel_unhealthy` so both see exactly
    the same node set.
    """
    min_gpus = int(config.get("policy", {}).get("min_gpus", 2))
    pairs, pools, all_nodes, errors = [], [], [], []
    for entry in config["pools"]:
        pool = client.get_pool(entry["id"])
        pools.append(pool)
        emit(f"pool {pool.name}: "
             + ("eligible" if pool.eligible else f"INELIGIBLE ({pool.reason})"))
        nodes = client.list_nodes(pool, min_gpus=min_gpus)

        only = entry.get("only_nodes")
        if only:
            wanted = {_norm(x) for x in only}
            selected = [n for n in nodes if _norm(n.name) in wanted]
            found = {_norm(n.name) for n in selected}
            missing = [x for x in only if _norm(x) not in found]
            if missing:
                errors.append(f"pool {pool.name}: only_nodes not found: {', '.join(missing)}")
                emit(f"  WARNING: only_nodes not found in pool: {', '.join(missing)}")
            emit(f"  restricted to {len(selected)}/{len(nodes)} named node(s)")
            nodes = selected

        all_nodes.extend(nodes)
        pairs.extend((n, pool) for n in nodes if n.eligible)
        emit(f"  {sum(1 for n in nodes if n.eligible)}/{len(nodes)} systems eligible")
    return pairs, pools, all_nodes, errors


def _probe_filter(config: dict, pairs: list, emit,
                  block_classes: Optional[tuple] = None) -> tuple[list, list]:
    """Split (node, pool) pairs by the live SSH health probe.

    A node is healthy only if we can log in with our key (no password), `rocm-smi` reports
    at least min_gpus GPUs, and `docker ps` works. Annotates each NodeInfo with
    probe_ok / probe_reason / probe_cls.

    `block_classes` decides which failures actually disqualify a node. Only failures we can
    attribute to the machine (`broken`, `unreachable`) do by default — an `access` failure
    means we could not log in, which is NOT evidence the node is bad, and treating it as
    such is a trap: we would stop reserving the node and so never regain the access needed
    to check it. Returns (allowed, blocked).
    """
    if block_classes is None:
        block_classes = _block_classes(config)
    settings = probe.probe_settings(config)
    if not settings["user"]:
        emit("WARNING: no ssh_user / health_probe.user configured — skipping health probe")
        return pairs, []
    min_gpus = int(config.get("policy", {}).get("min_gpus", 2))
    hosts = sorted({(n.hostname or n.name) for n, _ in pairs})
    emit(f"SSH health probe on {len(hosts)} node(s) as {settings['user']}...")
    results = probe.probe_hosts(
        hosts, user=settings["user"], key=settings["key"], min_gpus=min_gpus,
        timeout_s=settings["timeout_s"], workers=settings["workers"], progress=emit)

    allowed, blocked, unverified = [], [], 0
    for node, pool in pairs:
        r = (results.get(node.hostname or node.name)
             or {"ok": False, "reason": "not probed", "cls": probe.CLASS_UNREACHABLE})
        node.probe_ok, node.probe_reason = bool(r["ok"]), r["reason"]
        node.probe_cls = r.get("cls", "")
        if r["ok"] or node.probe_cls not in block_classes:
            if not r["ok"]:
                unverified += 1
            allowed.append((node, pool))
        else:
            node.eligible = False
            node.reason = "; ".join(x for x in (node.reason, r["reason"]) if x)
            blocked.append((node, pool))
    ok = sum(1 for n, _ in allowed if n.probe_ok)
    emit(f"health probe: {ok} healthy, {blocked and len(blocked) or 0} blocked "
         f"({', '.join(block_classes)}), {unverified} unverified but kept")
    return allowed, blocked


def run(config: dict, *, commit: bool = False, client: Optional[ConductorClient] = None,
        progress=None, node_names: Optional[list] = None,
        probe_health: bool = True) -> RunResult:
    """Execute one planning (and optionally committing) run.

    progress: optional callable(str) for live status lines (used by the web UI).
    node_names: if given, restrict to eligible nodes whose name matches one of these
        (domain-insensitive) — e.g. reserve a single node picked from `status`.
    probe_health: SSH-probe each candidate node and reserve only the healthy ones.
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
    eligible_nodes, pools, nodes, errors = _enumerate_nodes(client, config, emit)
    result.pools.extend(pools)
    result.nodes.extend(nodes)
    result.errors.extend(errors)

    if node_names:
        want = {_norm(x) for x in node_names}
        before = len(eligible_nodes)
        eligible_nodes = [(n, p) for (n, p) in eligible_nodes if _norm(n.name) in want]
        found = {_norm(n.name) for n, _ in eligible_nodes}
        missing = [x for x in node_names if _norm(x) not in found]
        if missing:
            result.errors.append("requested nodes not eligible/found: " + ", ".join(missing))
            emit("WARNING: requested nodes not eligible/found: " + ", ".join(missing))
        emit(f"restricted to {len(eligible_nodes)}/{before} eligible node(s) by --node")

    # 1b) drop nodes the probe shows are faulty or unreachable. Nodes we merely could not
    # log into are kept — that is a credentials fact, not a health fact, and skipping them
    # would mean never reserving them again (and so never being able to verify them).
    if probe_health and probe.probe_settings(config)["enabled"]:
        eligible_nodes, blocked = _probe_filter(config, eligible_nodes, emit)
        for n, _ in blocked:
            emit(f"  skipping {n.name}: {n.probe_reason}")

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


# Which probe failures we attribute to the machine rather than to our own credentials.
# "access" is deliberately excluded: failing to log in is not evidence a node is faulty —
# it may need a key we don't have here, or access may be gated on holding the reservation
# we would be giving up. Used both to decide what NOT to reserve and what to release.
CANCELLABLE_CLASSES = (probe.CLASS_BROKEN, probe.CLASS_UNREACHABLE)


def _block_classes(config: dict) -> tuple:
    """Probe failure classes that disqualify a node from being reserved."""
    hp = config.get("health_probe") or {}
    return tuple(hp.get("block_classes") or CANCELLABLE_CLASSES)


def cancel_unhealthy(config: dict, *, commit: bool = False, classes: tuple = CANCELLABLE_CLASSES,
                     client: Optional[ConductorClient] = None, progress=None) -> dict:
    """Find (and, with commit=True, cancel) our reservations on nodes failing the SSH probe.

    `classes` selects which failures count: by default only genuinely unusable nodes
    (`broken` — no/too few GPUs, no working docker; `unreachable` — down or hung). Pass
    `probe.CLASS_ACCESS` too to also release nodes we simply cannot log into from here.

    Scoped to reservations carrying our configured title, i.e. the ones this tool created.
    A colleague's own reservation on the same node is never touched, even if they are one
    of our default users.
    """
    def emit(msg: str):
        LOG.info(msg)
        if progress:
            progress(msg)

    client = client or ConductorClient()
    title = config["reservation"]["title"]
    pairs, _, _, _ = _enumerate_nodes(client, config, emit)
    # Treat every failure class as blocking here so the report can show them all; the
    # `classes` argument below decides which ones we actually act on.
    all_classes = (probe.CLASS_ACCESS, probe.CLASS_BROKEN, probe.CLASS_UNREACHABLE)
    healthy, unhealthy = _probe_filter(config, pairs, emit, block_classes=all_classes)

    skipped = [{"name": n.name, "pool": n.pool_name, "cls": n.probe_cls,
                "reason": n.probe_reason} for n, _ in unhealthy if n.probe_cls not in classes]
    if skipped:
        emit(f"keeping {len(skipped)} unhealthy node(s) outside classes {list(classes)} "
             f"(e.g. no access from here — the node may be fine for others)")
    unhealthy = [(n, p) for n, p in unhealthy if n.probe_cls in classes]

    nodes = [{"name": n.name, "hostname": n.hostname, "pool": n.pool_name, "id": n.id,
              "reason": n.probe_reason, "cls": n.probe_cls} for n, _ in unhealthy]
    by_id = {n["id"]: n for n in nodes}
    found = client.find_our_reservations([n["id"] for n in nodes], set(), {title})
    for r in found:
        node = by_id.get(r["node_id"], {})
        r["node_name"] = node.get("name", r["node_id"])
        r["node_reason"] = node.get("reason", "")
        r["date_start"] = str(r.get("date_start"))
        r["date_end"] = str(r.get("date_end"))
    emit(f"{len(nodes)} cancellable unhealthy node(s); our reservations on them: {len(found)}")

    cancelled: list[str] = []
    if commit and found:
        cancelled = client.cancel_reservations([r["id"] for r in found])
        emit(f"cancelled {len(cancelled)} reservation(s)")
    return {"title": title, "healthy": len(healthy), "unhealthy_nodes": nodes,
            "kept": skipped, "classes": list(classes), "found": found,
            "cancelled": cancelled, "committed": bool(commit)}


def _conductor_health_reason(h: dict, min_gpus: int) -> str:
    """Why Conductor's own scraped data considers a node unhealthy (probe disabled)."""
    bad = []
    if not h["reachable"]:
        bad.append(h["reachability"] or "unreachable")
    elif h["gpu_detected"] != h["gpu_expected"]:
        bad.append(f"gpu {h['gpu_detected']}/{h['gpu_expected']}")
    elif (h["gpu_detected"] or 0) < min_gpus:
        bad.append(f"{h['gpu_detected']}gpu<min{min_gpus}")
    if h["disabled"]:
        bad.append("disabled")
    if h["archived"]:
        bad.append("archived")
    return "; ".join(bad)


def cancel_ids(reservation_ids: list[str], *,
               client: Optional[ConductorClient] = None) -> list[str]:
    """Cancel exactly these reservation ids.

    Used after a reviewed dry-run so the commit cancels precisely the list the user was
    shown — re-running discovery could return a different set (the probe is live).
    """
    client = client or ConductorClient()
    return client.cancel_reservations(list(reservation_ids))


def status_report(config: dict, *, client: Optional[ConductorClient] = None,
                  check_reservations: bool = True, window_hours: int = 48,
                  probe_health: bool = True, progress=None) -> list[dict]:
    """Read-only 'free & healthy' report for every node across configured pools.

    Health is the live SSH probe (key-only login + rocm-smi + docker), falling back to
    Conductor's scraped data when the probe is disabled. Free/busy comes from the live
    reservation list. window_hours: how far ahead to look for reservations (48h for
    free/busy; widen it to capture your full holdings for a 'reserved by me' view).
    """
    from datetime import timedelta

    def emit(msg: str):
        LOG.info(msg)
        if progress:
            progress(msg)

    client = client or ConductorClient()
    policy = config.get("policy", {})
    min_gpus = int(policy.get("min_gpus", 2))
    now = datetime.now(timezone.utc)
    my_email = (client.me().get("email") or "").lower()

    rows: list[dict] = []
    for entry in config["pools"]:
        pool = client.get_pool(entry["id"])
        only = entry.get("only_nodes")
        wanted = {_norm(x) for x in only} if only else None
        health = client.node_health(pool, min_gpus=min_gpus)
        emit(f"pool {pool.name}: {len(health)} node(s)")
        for h in health:
            if wanted is not None and _norm(h["name"]) not in wanted:
                continue
            h["gpu_ok"] = (h["gpu_detected"] is not None and h["gpu_expected"] is not None
                           and h["gpu_detected"] == h["gpu_expected"] and h["gpu_detected"] >= min_gpus)
            h.update(probe_ok=None, probe_reason="", probe_gpus=None, probe_docker=None,
                     probe_cls="")
            rows.append(h)

    # Live SSH probe — the authoritative verdict. Skip nodes we'd never reserve anyway
    # (too few GPUs, archived) so we don't spend connections on them.
    settings = probe.probe_settings(config)
    block_classes = _block_classes(config)
    do_probe = bool(probe_health and settings["enabled"] and settings["user"])
    if do_probe:
        candidates = [h for h in rows
                      if (h["gpu_expected"] or 0) >= min_gpus and not h["archived"]]
        hosts = sorted({h["hostname"] or h["name"] for h in candidates})
        emit(f"SSH health probe on {len(hosts)} node(s) as {settings['user']}...")
        results = probe.probe_hosts(
            hosts, user=settings["user"], key=settings["key"], min_gpus=min_gpus,
            timeout_s=settings["timeout_s"], workers=settings["workers"], progress=emit)
        for h in candidates:
            r = results.get(h["hostname"] or h["name"])
            if r:
                h["probe_ok"], h["probe_reason"] = bool(r["ok"]), r["reason"]
                h["probe_gpus"], h["probe_docker"] = r["gpus"], r["docker"]
                h["probe_cls"] = r.get("cls", "")

    for h in rows:
        if do_probe:
            bad = []
            if h["probe_ok"] is None:
                bad.append(f"{h['gpu_expected']}gpu<min{min_gpus}"
                           if (h["gpu_expected"] or 0) < min_gpus else "not probed")
                h["probe_cls"] = probe.CLASS_BROKEN
            elif not h["probe_ok"]:
                bad.append(h["probe_reason"])
            if h["disabled"]:
                bad.append("disabled")
            if h["archived"]:
                bad.append("archived")
            h["healthy"] = not bad
            h["health_reason"] = "; ".join(bad)
            h["health_class"] = "" if not bad else (h["probe_cls"] or probe.CLASS_BROKEN)
        else:
            h["healthy"] = h["reachable"] and h["gpu_ok"] and not h["disabled"] and not h["archived"]
            h["health_reason"] = "" if h["healthy"] else _conductor_health_reason(h, min_gpus)
            h["health_class"] = "" if h["healthy"] else probe.CLASS_BROKEN
        # Excluded from reservation? Only machine-attributable failures count.
        h["blocked"] = h["health_class"] in block_classes

        h["reserved_now"] = None
        h["free_for_h"] = None
        h["free_at"] = None
        h["reserved_by_me"] = False
        h["held_now"] = False
        h["mine"] = []
        if check_reservations:
            resv = client.reservations_window(h["id"], now, now + timedelta(hours=window_hours))
            intervals = _merge_iv([(r["date_start"], r["date_end"]) for r in resv])
            active = [iv for iv in intervals if iv[0] <= now < iv[1]]
            h["reserved_now"] = bool(active)
            if active:
                h["free_at"] = max(iv[1] for iv in active).isoformat()
            else:
                upcoming = [iv[0] for iv in intervals if iv[0] > now]
                h["free_for_h"] = ((min(upcoming) - now).total_seconds() / 3600.0
                                   if upcoming else float("inf"))
            # which of these are mine (creator/member by email)?
            mine = [r for r in resv
                    if my_email in [(e or "").lower() for e in r["users"]]]
            if mine:
                mine.sort(key=lambda r: r["date_start"])
                h["reserved_by_me"] = True
                h["held_now"] = any(r["date_start"] <= now < r["date_end"] for r in mine)
                h["mine"] = [{"title": r["title"],
                              "date_start": r["date_start"].isoformat(),
                              "date_end": r["date_end"].isoformat(),
                              "active": r["date_start"] <= now < r["date_end"]}
                             for r in mine]
        h["free_and_healthy"] = bool(h["healthy"] and h["reserved_now"] is False)
    return rows


def sync_users(config: dict, *, commit: bool = False,
               client: Optional[ConductorClient] = None, progress=None) -> dict:
    """Ensure every existing (ongoing + upcoming) reservation with our title carries the
    config's default users. Uses an additive update (mode='add'), so it never removes anyone
    and is idempotent. Returns the reservations found and, when committed, per-item outcome.
    """
    def emit(msg: str):
        LOG.info(msg)
        if progress:
            progress(msg)

    client = client or ConductorClient()
    res_cfg = config["reservation"]
    title = res_cfg["title"]
    user_ids, unresolved = client.resolve_users(res_cfg.get("users", []))
    if unresolved:
        emit("WARNING: unresolved users skipped: " + ", ".join(unresolved))
    reservations = client.my_reservations(title=title, future=True)
    emit(f"{len(reservations)} ongoing/upcoming reservation(s) titled {title!r}; "
         f"ensuring {len(user_ids)} default user(s) on each")

    results = []
    for r in reservations:
        row = {**r, "status": "planned", "message": ""}
        if commit:
            try:
                client.add_users_to_reservation(r["id"], user_ids)
                row["status"] = "updated"
            except Exception as e:  # noqa: BLE001
                row["status"] = "failed"
                row["message"] = _short(str(e))
        results.append(row)
    if commit:
        ok = sum(1 for x in results if x["status"] == "updated")
        emit(f"updated {ok}/{len(results)} reservation(s)")
    return {"title": title, "user_ids": user_ids, "unresolved": unresolved,
            "reservations": results, "committed": bool(commit)}


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
