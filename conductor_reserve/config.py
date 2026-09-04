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


def add_node_user(node: str, user: str, path: Path | str = CONFIG_FILE) -> bool:
    """Persist `user` under node_users[node] in config.yaml so future runs include them on that
    node. Round-trips with ruamel to preserve the file's comments/formatting. Idempotent — writes
    only when the entry is new; returns True if the file changed. Used by `add-user --future`."""
    from ruamel.yaml import YAML
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    p = Path(path)
    data = yaml_rt.load(p.read_text())
    nu = data.get("node_users")
    if nu is None:
        data["node_users"] = nu = {}
    users = nu.get(node)
    if users is None:
        nu[node] = users = []
    if user in users:
        return False
    users.append(user)
    with p.open("w") as f:
        yaml_rt.dump(data, f)
    return True
