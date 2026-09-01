"""Thin, well-typed wrapper over the Conductor Python SDK.

Every method here corresponds to a call that was validated live against
conductor.amd.com. Nothing in this module creates a reservation except
`create_reservations`, which is only ever called from the engine in commit mode.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime
from typing import Any, Optional

from . import creds
from .models import NodeInfo, PoolInfo

LOG = logging.getLogger("conductor_reserve.conductor")

# Statuses that are purely cosmetic per the SDK (must NOT gate reservations).
_VALID_STRATEGY = "calendar"


def _is_uuid(v: Any) -> bool:
    try:
        _uuid.UUID(str(v))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _first(x):
    """find_pool_by_id and friends return a list; normalize to a single object."""
    if isinstance(x, list):
        return x[0] if x else None
    return x


def _dig(d: dict, *path, default=None):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _aware(dt):
    """Ensure a datetime is tz-aware (assume UTC if naive)."""
    from datetime import timezone
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _merge(intervals):
    """Merge overlapping/adjacent (start, end) intervals; return sorted list."""
    intervals = sorted(intervals)
    merged: list[tuple] = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


class ConductorClient:
    def __init__(self) -> None:
        creds.require_credentials()  # populates env before SDK import reaches backend
        # Imported lazily so credential env vars are set first.
        from conductor_sdk.resources.my import MyResources
        from conductor_sdk.resources.pools.queries import PoolQuerier
        from conductor_sdk.resources.systems import SystemQuerier
        from conductor_sdk.resources.users.queries import UserQuerier
        from at_scale_python_api.backend.v2.reservation import ReservationController

        self._MyResources = MyResources
        self._pools = PoolQuerier()
        self._systems = SystemQuerier()
        self._users = UserQuerier()
        self._rc = ReservationController()
        self._me: Optional[dict] = None

    # ---- identity ---------------------------------------------------------
    def me(self) -> dict:
        if self._me is None:
            u = self._MyResources().user
            teams = []
            for t in (getattr(u, "teams", None) or []):
                teams.append({"name": getattr(t, "name", None), "id": str(getattr(t, "id", None))})
            self._me = {
                "id": str(getattr(u, "id", None)),
                "email": getattr(u, "email", None),
                "full_name": getattr(u, "full_name", None),
                "teams": teams,
            }
        return self._me

    def resolve_team_id(self, team_name: str) -> str:
        if _is_uuid(team_name):
            return str(team_name)
        for t in self.me()["teams"]:
            if (t["name"] or "").lower() == team_name.lower():
                return t["id"]
        available = ", ".join(t["name"] for t in self.me()["teams"])
        raise ValueError(
            f"Team {team_name!r} not found among your teams for allocation. Available: {available}"
        )

    def resolve_users(self, entries: list[str]) -> tuple[list[str], list[str]]:
        """Resolve emails / 'Last, First' names / UUIDs to user ids.

        Returns (resolved_ids, unresolved_inputs).
        """
        resolved: list[str] = []
        unresolved: list[str] = []
        for entry in entries:
            if _is_uuid(entry):
                resolved.append(str(entry))
                continue
            try:
                rows = self._users.lookup_advanced(user=[entry]).all()
            except Exception as e:  # noqa: BLE001
                LOG.warning("user lookup failed for %r: %s", entry, e)
                rows = []
            if rows:
                resolved.append(str(getattr(rows[0], "id")))
            else:
                unresolved.append(entry)
        return resolved, unresolved

    # ---- pools & systems --------------------------------------------------
    def get_pool(self, pool_id: str) -> PoolInfo:
        p = _first(self._pools.find_pool_by_id(pool_id))
        if p is None:
            return PoolInfo(pool_id, "<unknown>", "", None, None, True, False, True,
                            eligible=False, reason="pool not found")
        d = p.model_dump()
        strategy = d.get("reservation_strategy")
        block_api = bool(d.get("block_api_access"))
        restr_met = bool(d.get("group_restrictions_met", True))
        archived = bool(d.get("archived"))
        reasons = []
        if strategy != _VALID_STRATEGY:
            reasons.append(f"strategy={strategy!r} (not calendar-based)")
        if archived:
            reasons.append("pool archived")
        if block_api:
            reasons.append("block_api_access=True")
        if not restr_met:
            reasons.append("group restrictions not met")
        return PoolInfo(
            id=str(d.get("id")),
            name=d.get("name"),
            strategy=strategy,
            duration_limit_s=d.get("reservation_duration_limit"),
            furthest_future_s=d.get("furthest_future_reservation"),
            block_api_access=block_api,
            group_restrictions_met=restr_met,
            archived=archived,
            eligible=not reasons,
            reason="; ".join(reasons),
        )

    def list_nodes(self, pool: PoolInfo, min_gpus: int = 2) -> list[NodeInfo]:
        nodes: list[NodeInfo] = []
        systems = self._systems.find_system_advanced(pool=[pool.name]).all()
        for s in systems:
            d = s.model_dump()
            name = _dig(d, "system_datas", "name") or _dig(d, "system_datas", "hostname_ip") or str(d.get("id"))
            archived = bool(d.get("archived"))
            status = d.get("status")
            status = getattr(status, "value", status)  # EntityStatus enum -> str
            # GPU count: prefer configured platform spec, fall back to SSH-scraped.
            gpu = _dig(d, "system_datas", "platform_config", "num_dgpus")
            if gpu is None:
                gpu = _dig(d, "system_ssh_data", "gpu_count")
            reasons = []
            if not pool.eligible:
                reasons.append(f"pool ineligible ({pool.reason})")
            if archived:
                reasons.append("system archived")
            if gpu is not None and gpu < min_gpus:
                reasons.append(f"{gpu}-GPU node (< min_gpus={min_gpus})")
            nodes.append(NodeInfo(
                id=str(d.get("id")),
                name=name,
                pool_id=pool.id,
                pool_name=pool.name,
                archived=archived,
                reservation_only=bool(d.get("reservation_only")),
                status=str(status) if status is not None else None,
                gpu_count=gpu,
                eligible=not reasons,
                reason="; ".join(reasons),
            ))
        return nodes

    def node_health(self, pool: PoolInfo, min_gpus: int = 2) -> list[dict]:
        """Per-node health from Conductor's own scraped data (no SSH). Read-only."""
        out: list[dict] = []
        for s in self._systems.find_system_advanced(pool=[pool.name]).all():
            d = s.model_dump()
            ssh = d.get("system_ssh_data") or {}
            st = d.get("system_states") or {}
            gpu_det = ssh.get("gpu_count")
            gpu_exp = _dig(d, "system_datas", "platform_config", "num_dgpus")
            out.append({
                "id": str(d.get("id")),
                "name": _dig(d, "system_datas", "name") or str(d.get("id")),
                "hostname": _dig(d, "system_datas", "hostname_ip"),
                "pool": pool.name,
                "gpu_detected": gpu_det, "gpu_expected": gpu_exp,
                "reachable": ssh.get("reachability_status") == "SUCCESS",
                "reachability": ssh.get("reachability_status"),
                "disabled": bool(st.get("disabled")),
                "disabled_reason": st.get("disabled_reason"),
                "ssh_enabled": bool(st.get("ssh_enabled")),
                "telemetry": ssh.get("fleet_telemetry_state"),
                "driver": ssh.get("current_gpu_driver_loaded"),
                "rocm_ver": ssh.get("rocm_ver"),
                "util_24h": st.get("utilization_24hrs"),
                "archived": bool(d.get("archived")),
                "min_gpus": min_gpus,
            })
        return out

    # ---- our reservations / cancellation ---------------------------------
    def find_our_reservations(self, node_ids: list[str], our_emails: set[str],
                              titles: set[str], *, future_only: bool = False) -> list[dict]:
        """Reservations on the given nodes that are 'ours' (matching title OR any of our
        users). Includes active + future by default. Read-only."""
        from conductor_sdk.resources.reservations.queries import ReservationQuerier
        from ats_models.pydantic.conductor_query import DateLookup
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        rq = ReservationQuerier()
        out: list[dict] = []
        for nid in node_ids:
            kwargs = dict(entity=[str(nid)],
                          date_end=[DateLookup(value=now, operation="gt")], page_size=50)
            if future_only:
                kwargs["date_start"] = [DateLookup(value=now, operation="gt")]
            res = rq.lookup_advanced(**kwargs)
            page = res.next()
            for r in (getattr(page, "data", None) or []):
                d = r.model_dump()
                emails = {(u.get("email") if isinstance(u, dict) else None)
                          for u in (d.get("users") or [])}
                if d.get("title") in titles or (emails & our_emails):
                    out.append({"id": str(d.get("id")), "node_id": str(nid),
                                "title": d.get("title"), "date_start": d.get("date_start"),
                                "date_end": d.get("date_end"), "emails": sorted(e for e in emails if e)})
        return out

    def cancel_reservations(self, reservation_ids: list[str]) -> list[str]:
        """DELETE the given reservations. Destructive — callers must confirm first."""
        from uuid import UUID
        ids = [UUID(str(x)) for x in reservation_ids]
        return [str(x) for x in self._rc.delete(ids)]

    def my_reservations(self, title: Optional[str] = None, *, future: bool = True) -> list[dict]:
        """My ongoing (and, with future=True, upcoming) reservations, optionally filtered by
        title. Scoped to me (I'm a member), so I have edit rights on them. Read-only."""
        gen = self._MyResources().reservations(return_future_reservations=future)
        out: list[dict] = []
        pages = 0
        while pages < 60:
            page = gen.next()
            if page is None:
                break
            for r in (getattr(page, "data", None) or []):
                d = r.model_dump()
                if title is not None and d.get("title") != title:
                    continue
                out.append({
                    "id": str(d.get("id")), "title": d.get("title"),
                    "date_start": str(d.get("date_start")), "date_end": str(d.get("date_end")),
                    "users": [(u.get("email") if isinstance(u, dict) else None)
                              for u in (d.get("users") or [])],
                })
            pages += 1
            if getattr(page, "last_page", True):
                break
        return out

    def reservations_window(self, node_id: str, start: datetime, end: datetime) -> list[dict]:
        """Raw reservations on a node overlapping [start, end]: id, title, start, end, user
        emails. One query; used to derive free/busy AND 'mine'. Read-only."""
        from conductor_sdk.resources.reservations.queries import ReservationQuerier
        from ats_models.pydantic.conductor_query import DateLookup
        res = ReservationQuerier().lookup_advanced(
            entity=[str(node_id)],
            date_end=[DateLookup(value=start, operation="gt")],
            date_start=[DateLookup(value=end, operation="lt")],
            page_size=50)
        out = []
        pages = 0
        while pages < 40:
            page = res.next()
            if page is None:
                break
            for r in (getattr(page, "data", None) or []):
                d = r.model_dump()
                s, e = d.get("date_start"), d.get("date_end")
                if s and e:
                    out.append({"id": str(d.get("id")), "title": d.get("title"),
                                "date_start": _aware(s), "date_end": _aware(e),
                                "users": [(u.get("email") if isinstance(u, dict) else None)
                                          for u in (d.get("users") or [])]})
            pages += 1
            if getattr(page, "last_page", True):
                break
        return out

    def add_users_to_reservation(self, reservation_id: str, user_ids: list[str]) -> None:
        """Add users to an existing reservation (mode='add' — never removes anyone)."""
        from uuid import UUID
        from ats_models.pydantic.reservations.actions.update import UpdateReservation
        upd = UpdateReservation(id=UUID(str(reservation_id)),
                                user_ids=[UUID(str(u)) for u in user_ids],
                                user_ids_mode="add")
        self._rc.update([upd])

    # ---- availability -----------------------------------------------------
    def busy_intervals(self, target_id: str, start: datetime, end: datetime
                       ) -> list[tuple[datetime, datetime]]:
        """Real existing reservations overlapping [start, end] for this node, merged.

        Uses ReservationQuerier (authoritative). NOTE: check_conflicts was found to be
        unreliable — it echoes the query window rather than the real bookings — so we read
        the actual reservation list instead. Returns merged, sorted (date_start, date_end)
        tuples. Read-only.
        """
        from conductor_sdk.resources.reservations.queries import ReservationQuerier
        from ats_models.pydantic.conductor_query import DateLookup

        res = ReservationQuerier().lookup_advanced(
            entity=[str(target_id)],
            date_end=[DateLookup(value=start, operation="gt")],   # not yet finished
            date_start=[DateLookup(value=end, operation="lt")],   # starts before our window ends
            page_size=50,
        )
        intervals: list[tuple[datetime, datetime]] = []
        pages = 0
        while pages < 40:
            page = res.next()
            if page is None:
                break
            rows = getattr(page, "data", None) or []
            for r in rows:
                d = r.model_dump()
                s, e = d.get("date_start"), d.get("date_end")
                if s and e:
                    intervals.append((_aware(s), _aware(e)))
            pages += 1
            if getattr(page, "last_page", True):
                break
        return _merge(intervals)

    # ---- writes (commit only) --------------------------------------------
    def create_reservations(self, payloads: list[dict]) -> tuple[list[dict], dict]:
        """Create reservations. `payloads` are kwargs for CreateReservation.
        Returns (created_records, notes). Only called in commit mode."""
        from ats_models.pydantic.reservations.actions.create import CreateReservation
        models = [CreateReservation(**p) for p in payloads]
        result = self._rc.create(models)
        created = [r.model_dump() for r in getattr(result, "reservations", []) or []]
        notes = getattr(result, "notes", {}) or {}
        return created, notes
