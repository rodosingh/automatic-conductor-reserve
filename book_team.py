#!/usr/bin/env python
"""Book one assigned node per credential, on behalf of a team.

Reads `team_booking.assignments` from config.yaml: each entry maps a .env credential
(`cred: default` -> AMD_EMAIL/ATS_SECRET, `cred: <N>` -> CRED_<N>_EMAIL/CRED_<N>_SECRET)
to exactly one node. For each assignment we authenticate as that identity and reserve
ONLY that node's currently-free window (shared with reservation.users), reusing the same
engine as `cli.py run`. Re-running grabs newly-free time and extends the hold, so a cron
keeps the nodes held.

Each identity runs in its own subprocess so credentials never bleed between bookings.

    python book_team.py                 # dry-run: show what every key would book
    python book_team.py --commit        # actually create the reservations
    python book_team.py --only 3        # just credential 3's assignment
    python book_team.py --only <node-name>          # just that node
    python book_team.py --probe         # SSH-health-check each node before booking (slower)

Nothing here is secret: credentials come from .env, the node map from config.yaml
(both gitignored).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from conductor_reserve import creds
from conductor_reserve.config import load_config


def _cred_env(label: str) -> tuple[str, str]:
    """Resolve a credential label ('default' or 'N') to (email, secret) from the env."""
    if str(label) == "default":
        email, secret = os.getenv("AMD_EMAIL"), os.getenv("ATS_SECRET")
    else:
        email = os.getenv(f"CRED_{label}_EMAIL")
        secret = os.getenv(f"CRED_{label}_SECRET")
    if not email or not secret:
        raise SystemExit(f"missing credential '{label}' in .env "
                         f"(need {'AMD_EMAIL/ATS_SECRET' if label=='default' else f'CRED_{label}_EMAIL/CRED_{label}_SECRET'})")
    return email, secret


def _assignments(cfg: dict) -> list[dict]:
    items = (cfg.get("team_booking") or {}).get("assignments") or []
    if not items:
        raise SystemExit("no team_booking.assignments in config.yaml")
    return items


def run_worker(node: str, commit: bool, probe: bool) -> int:
    """In-subprocess: book one node as whatever identity the env already holds."""
    from conductor_reserve.engine import run
    cfg = load_config()
    client = None
    try:
        from conductor_reserve.conductor import ConductorClient
        client = ConductorClient()
        who = client.me().get("email")
    except Exception as e:  # noqa: BLE001
        print(f"  AUTH FAILED: {e}")
        return 1
    print(f"  authenticated as {who}")
    result = run(cfg, commit=commit, client=client, node_names=[node],
                 probe_health=probe, filter_windows=False, cancel_fragmented=False)
    plan = result.plan
    if not plan:
        print(f"  node {node}: nothing free to book right now "
              f"(0 reservations) {'— ' + '; '.join(result.errors) if result.errors else ''}")
        return 0
    users = len(plan[0].user_ids)
    win = f"{plan[0].date_start:%m-%d %H:%M} -> {plan[-1].date_end:%m-%d %H:%M} UTC"
    print(f"  node {node}: {len(plan)} reservation(s)  window {win}  shared_with={users} user(s)")
    if commit:
        for it in plan:
            tag = getattr(it, "status", "?")
            extra = getattr(it, "reservation_id", None) or getattr(it, "message", "")
            print(f"    - {tag}: {extra}")
    else:
        print(f"    (dry-run — would create the above; re-run with --commit)")
    for e in result.errors:
        print(f"    error: {e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Book one assigned node per credential.")
    ap.add_argument("--commit", action="store_true", help="actually create reservations")
    ap.add_argument("--only", help="limit to one credential label or node name")
    ap.add_argument("--probe", action="store_true", help="SSH health-check each node first")
    # internal:
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--node", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        return run_worker(args.node, args.commit, args.probe)

    creds.load_env()  # populate AMD_EMAIL / CRED_*_ * into the environment
    cfg = load_config()
    items = _assignments(cfg)
    if args.only:
        items = [a for a in items
                 if str(a.get("cred")) == args.only or a.get("node") == args.only]
        if not items:
            raise SystemExit(f"--only {args.only!r} matched no assignment")

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"=== book_team.py [{mode}] — {len(items)} assignment(s) ===")
    rc = 0
    for a in items:
        label, node = str(a.get("cred")), a.get("node")
        email, secret = _cred_env(label)
        print(f"\n[cred {label}] {email}  ->  {node}")
        env = dict(os.environ)
        env["AMD_EMAIL"], env["ATS_SECRET"] = email, secret
        env["VERIFY_CERTS"] = os.getenv("VERIFY_CERTS", "false")
        cmd = [sys.executable, __file__, "--worker", "--node", node]
        if args.commit:
            cmd.append("--commit")
        if args.probe:
            cmd.append("--probe")
        p = subprocess.run(cmd, env=env, text=True)
        rc = rc or p.returncode
    print(f"\n=== done ({mode}) ===")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
