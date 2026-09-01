"""Load Conductor credentials from .env into the process environment.

The conductor_sdk / at_scale_python_api layer reads AMD_EMAIL and ATS_SECRET from
os.environ at call time and sends `Authorization: <email>:<secret>`. We just make
sure those are populated (from .env) before any SDK import that reaches the backend.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(env_file: Path | str = ENV_FILE) -> None:
    """Minimal .env loader (KEY=VALUE lines). Does not overwrite already-set vars."""
    p = Path(env_file)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def require_credentials() -> tuple[str, str]:
    """Return (email, secret), raising a clear error if either is missing."""
    load_env()
    email = os.getenv("AMD_EMAIL") or os.getenv("ATS_EMAIL")
    secret = os.getenv("ATS_SECRET")
    if not email or not secret:
        raise RuntimeError(
            "Missing credentials. Set AMD_EMAIL and ATS_SECRET in "
            f"{ENV_FILE} (chmod 600). Get your API key from Conductor > profile > API key."
        )
    # at_scale reads these; keep TLS-verify off unless explicitly enabled (corp MITM cert).
    os.environ.setdefault("VERIFY_CERTS", "false")
    return email, secret
