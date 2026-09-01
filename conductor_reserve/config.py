"""Load and validate config.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.yaml"


def load_config(path: Path | str = CONFIG_FILE) -> dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text())
    res = cfg.get("reservation", {})
    for req in ("title", "project", "milestone", "team_name"):
        if not res.get(req):
            raise ValueError(f"config.reservation.{req} is required")
    if len(res["title"]) < 3 or len(res["project"]) < 3:
        raise ValueError("title and project must each be at least 3 characters")
    if not cfg.get("pools"):
        raise ValueError("config.pools must list at least one pool id")
    cfg.setdefault("policy", {})
    return cfg
