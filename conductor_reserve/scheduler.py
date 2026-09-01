"""Greedy reservation planner.

For each eligible node we fill its free time with back-to-back reservations, each of
at most the pool's max duration, from now until the pool's future horizon. Busy time is
discovered from the server (check_conflicts), so we never double-book, and the split
respects existing reservations. Planning is entirely read-only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import NodeInfo, PlanItem, PoolInfo

LOG = logging.getLogger("conductor_reserve.scheduler")


def _round_down(dt: datetime, minutes: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % minutes)


def _round_up(dt: datetime, minutes: int) -> datetime:
    floored = _round_down(dt, minutes)
    return floored if floored == dt.replace(second=0, microsecond=0) else floored + timedelta(minutes=minutes)


def plan_node(
    client,
    node: NodeInfo,
    pool: PoolInfo,
    *,
    reservation_defaults: dict,
    policy: dict,
    now: Optional[datetime] = None,
) -> list[PlanItem]:
    """Return the list of reservations to create for one node (possibly empty)."""
    now = now or datetime.now(timezone.utc)
    rnd = int(policy.get("round_minutes", 10))
    lead = int(policy.get("start_lead_minutes", 20))
    min_dur = int(policy.get("min_reservation_minutes", 60)) * 60
    max_per_node = policy.get("max_reservations_per_node")

    # Pools may leave limits unset (null) = no server-enforced cap. Fall back to configured
    # defaults so greedy fill stays bounded and predictable.
    dur_limit = pool.duration_limit_s or int(policy.get("default_duration_hours", 24)) * 3600
    # furthest_future caps how far ahead a reservation may END (verified live: the server
    # rejects any reservation whose date_end exceeds now + furthest_future). So this is the
    # hard ceiling on date_end — you cannot chain past it.
    future_s = pool.furthest_future_s or int(policy.get("default_horizon_days", 14)) * 86400

    horizon = _round_down(now + timedelta(seconds=future_s), rnd)   # latest allowed date_END
    earliest = _round_up(now + timedelta(minutes=lead), rnd)
    if earliest >= horizon:
        return []

    # Real existing reservations (ours or others') across the window.
    busy = client.busy_intervals(node.id, now, horizon)
    milestone = _milestone(reservation_defaults)
    items: list[PlanItem] = []

    def add(s, e):
        items.append(PlanItem(
            node_id=node.id, node_name=node.name, pool_name=pool.name,
            date_start=s, date_end=e, milestone=max(milestone, e),
            title=reservation_defaults["title"], project=reservation_defaults["project"],
            description=reservation_defaults.get("description"),
            team_id=reservation_defaults["team_id"],
            user_ids=list(reservation_defaults["user_ids"]),
            batch_opt_out=bool(reservation_defaults.get("batch_opt_out", True)),
        ))

    # Tile free gaps between existing reservations, from `earliest` up to the horizon, with
    # reservations of at most dur_limit. Every block must END by `horizon` (the hard cap),
    # so a node busy until near the horizon yields only a short tail; a node free now yields
    # a full dur_limit block; where the horizon is far (null-limit pools) this chains many.
    cursor = earliest
    for bs, be in busy + [(horizon, horizon)]:
        gap_end = min(_round_down(bs, rnd), horizon)
        while cursor < gap_end:
            if max_per_node is not None and len(items) >= int(max_per_node):
                return items
            end = _round_down(min(cursor + timedelta(seconds=dur_limit), gap_end), rnd)
            if (end - cursor).total_seconds() < min_dur:
                break
            add(cursor, end)
            cursor = end
        if be > cursor:
            cursor = _round_up(be, rnd)  # jump past this existing reservation
        if cursor >= horizon:
            break

    return items


def _milestone(reservation_defaults: dict) -> datetime:
    m = reservation_defaults["milestone"]
    if isinstance(m, datetime):
        dt = m
    else:
        dt = datetime.strptime(str(m), "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
