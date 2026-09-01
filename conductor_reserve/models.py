"""Plain data structures passed between the layers (no SDK types leak upward)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional


@dataclass
class NodeInfo:
    """An enumerated system and whether it is eligible for reservation."""
    id: str
    name: str
    pool_id: str
    pool_name: str
    archived: bool
    reservation_only: bool
    status: Optional[str]
    gpu_count: Optional[int]
    eligible: bool
    reason: str = ""  # why ineligible (empty when eligible)


@dataclass
class PoolInfo:
    id: str
    name: str
    strategy: str
    duration_limit_s: Optional[int]
    furthest_future_s: Optional[int]
    block_api_access: bool
    group_restrictions_met: bool
    archived: bool
    eligible: bool
    reason: str = ""


@dataclass
class PlanItem:
    """One reservation we intend to (or did) create."""
    node_id: str
    node_name: str
    pool_name: str
    date_start: datetime
    date_end: datetime
    milestone: datetime
    title: str
    project: str
    description: Optional[str]
    team_id: str
    user_ids: list[str]
    batch_opt_out: bool
    # filled after a commit attempt:
    status: str = "planned"          # planned | created | failed | skipped
    reservation_id: Optional[str] = None
    message: str = ""

    @property
    def duration_hours(self) -> float:
        return (self.date_end - self.date_start).total_seconds() / 3600.0

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("date_start", "date_end", "milestone"):
            d[k] = getattr(self, k).isoformat()
        d["duration_hours"] = round(self.duration_hours, 2)
        return d


@dataclass
class RunResult:
    started_at: datetime
    finished_at: Optional[datetime] = None
    committed: bool = False
    pools: list[PoolInfo] = field(default_factory=list)
    nodes: list[NodeInfo] = field(default_factory=list)
    plan: list[PlanItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(1 for p in self.plan if p.status == "created")

    @property
    def failed(self) -> int:
        return sum(1 for p in self.plan if p.status == "failed")

    def summary(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "committed": self.committed,
            "pools_total": len(self.pools),
            "pools_eligible": sum(1 for p in self.pools if p.eligible),
            "nodes_total": len(self.nodes),
            "nodes_eligible": sum(1 for n in self.nodes if n.eligible),
            "planned": len(self.plan),
            "created": self.created,
            "failed": self.failed,
            "errors": self.errors,
        }
