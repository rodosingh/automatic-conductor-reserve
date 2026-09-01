#!/usr/bin/env python
"""Command-line entry point for Conductor auto-reservation.

    python cli.py plan            # dry-run: show what WOULD be reserved (default, no writes)
    python cli.py run --commit    # actually create the reservations
    python cli.py whoami          # verify auth + show your teams
    python cli.py runs            # list past run summaries

Dry-run is the default. `run` without --commit is also a dry-run.
"""
from __future__ import annotations

import argparse
import logging
import sys

from conductor_reserve import notify
from conductor_reserve.config import load_config
from conductor_reserve.conductor import ConductorClient
from conductor_reserve.engine import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Conductor auto-reservation for Hyperloom")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="live progress lines")
    common.add_argument("--pool", action="append", metavar="POOL_ID",
                        help="restrict to this pool id (repeatable); default = all configured pools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", parents=[common], help="dry-run: show the plan, create nothing")
    r = sub.add_parser("run", parents=[common],
                       help="build the plan and (with --commit) create reservations")
    r.add_argument("--commit", action="store_true", help="actually create reservations")
    r.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    sub.add_parser("whoami", parents=[common], help="verify authentication and list your teams")
    sub.add_parser("runs", parents=[common], help="list past run summaries")
    c = sub.add_parser("cancel-small-gpu", parents=[common],
                       help="cancel OUR current+future reservations on nodes below min_gpus")
    c.add_argument("--commit", action="store_true", help="actually cancel (default: dry-run)")
    c.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "whoami":
        me = ConductorClient().me()
        print(f"email: {me['email']}\nid:    {me['id']}\nname:  {me['full_name']}")
        print("teams for allocation:")
        for t in me["teams"]:
            print(f"  - {t['name']}  ({t['id']})")
        return 0

    if args.cmd == "runs":
        for s in notify.list_runs():
            print(f"{s['started_at']}  {'COMMIT' if s.get('committed') else 'dryrun'}  "
                  f"planned={s.get('planned')} created={s.get('created')} "
                  f"failed={s.get('failed')}  {s.get('file')}")
        return 0

    if args.cmd == "cancel-small-gpu":
        return _cancel_small_gpu(args)

    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
        if not cfg["pools"]:
            print(f"no configured pools match --pool {args.pool}")
            return 1

    commit = bool(getattr(args, "commit", False))
    if commit and not getattr(args, "yes", False):
        print(">>> COMMIT MODE: this will create real reservations on shared lab nodes.")
        if input(">>> Type 'yes' to proceed: ").strip().lower() != "yes":
            print("aborted.")
            return 1

    result = run(cfg, commit=commit, progress=lambda m: print("·", m) if args.verbose else None)
    print()
    print(notify.format_console(result))
    return 0


def _filter_pools(cfg: dict, pool_ids: list) -> dict:
    """Return a shallow copy of cfg whose pools are restricted to the given ids."""
    want = set(pool_ids)
    return {**cfg, "pools": [e for e in cfg["pools"] if e["id"] in want]}


def _cancel_small_gpu(args) -> int:
    """Find and (with --commit) cancel our reservations on sub-min_gpus nodes."""
    from conductor_reserve.engine import cancel_small_gpu
    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    # First pass: find only (no writes) so we can show and confirm.
    res = cancel_small_gpu(cfg, commit=False)
    found, min_gpus = res["found"], res["min_gpus"]
    print(f"nodes below min_gpus={min_gpus}: {res['nodes_below']}")
    if not found:
        print("no reservations of ours on those nodes — nothing to cancel.")
        return 0
    print(f"\nour reservations on sub-{min_gpus}-GPU nodes ({len(found)}):")
    for r in found:
        print(f"  {r['node_name']:24} {r['date_start'][:16]} -> {r['date_end'][:16]} "
              f"| {r['title']} | {r['id']}")
    if not args.commit:
        print("\n(dry-run) re-run with --commit to cancel these.")
        return 0
    if not args.yes and input(
            f"\nCancel these {len(found)} reservation(s)? Type 'yes': ").strip().lower() != "yes":
        print("aborted.")
        return 1
    out = cancel_small_gpu(cfg, commit=True)
    print(f"cancelled {len(out['cancelled'])} reservation(s): {', '.join(out['cancelled'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
