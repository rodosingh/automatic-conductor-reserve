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
    common.add_argument("--node", action="append", metavar="NAME",
                        help="restrict to this node name/hostname (repeatable) — e.g. one picked from `status`")
    common.add_argument("--no-probe", action="store_true",
                        help="skip the SSH health probe (faster, but reserves without "
                             "verifying login / rocm-smi / docker)")
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
    cu = sub.add_parser("cancel-unhealthy", parents=[common],
                        help="cancel OUR reservations on nodes that fail the SSH health probe")
    cu.add_argument("--commit", action="store_true", help="actually cancel (default: dry-run)")
    cu.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    cu.add_argument("--include-access", action="store_true",
                    help="also release nodes we simply cannot log into from here "
                         "(default: only genuinely broken/unreachable nodes)")
    su = sub.add_parser("sync-users", parents=[common],
                        help="add config's default users to our existing (ongoing+upcoming) reservations")
    su.add_argument("--commit", action="store_true", help="actually update (default: dry-run)")
    su.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    st = sub.add_parser("status", parents=[common],
                        help="report which nodes are free & healthy, and why the rest are not")
    st.add_argument("--free", action="store_true", help="show only free & healthy nodes")
    st.add_argument("--unhealthy", action="store_true",
                    help="show only unhealthy nodes, with the reason each one failed")
    st.add_argument("--reserved", action="store_true",
                    help="show only currently-reserved nodes, with who holds them and until when")
    st.add_argument("--fast", action="store_true", help="skip the reservation check (health only)")
    st.add_argument("--ssh-user", default=None, help="username for the printed rocm-smi ssh command")
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

    if args.cmd == "cancel-unhealthy":
        return _cancel_unhealthy(args)

    if args.cmd == "sync-users":
        return _sync_users(args)

    if args.cmd == "status":
        return _status(args)

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

    result = run(cfg, commit=commit, node_names=getattr(args, "node", None),
                 probe_health=not args.no_probe,
                 progress=lambda m: print("·", m) if args.verbose else None)
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


def _gpu_cell(r: dict) -> str:
    """GPUs as rocm-smi actually reports them, falling back to Conductor's numbers."""
    if r.get("probe_gpus") is not None:
        return f"{r['probe_gpus']}/{r['gpu_expected']}"
    return f"{r['gpu_detected']}/{r['gpu_expected']}"


def _status(args) -> int:
    """Report which nodes are free and healthy, and why the unhealthy ones failed."""
    import os
    from conductor_reserve.engine import status_report
    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    ssh_user = args.ssh_user or cfg.get("ssh_user") or os.getenv("USER") or "<your-ntid>"
    if args.reserved and args.fast:
        print("--reserved needs the reservation check; ignoring --fast.")
        args.fast = False
    # For "reserved by me" show the full future holdings, not just the next 48h.
    window_hours = 48
    if args.reserved:
        window_hours = max(48, int(cfg.get("policy", {}).get("default_horizon_days", 14)) * 24 + 48)
    rows = status_report(cfg, check_reservations=not args.fast, window_hours=window_hours,
                         probe_health=not args.no_probe,
                         progress=lambda m: print("·", m) if args.verbose else None)
    unhealthy = [r for r in rows if not r["healthy"]]

    if args.reserved:
        # nodes YOU have reserved (ongoing + upcoming)
        held = [r for r in rows if r["reserved_by_me"]]
        print(f"{'node':26} {'pool':20} {'gpu':>6} {'health':7} {'your hold (UTC)':29} "
              f"{'#':>2} {'state':8} title")
        print("-" * 116)
        for r in sorted(held, key=lambda r: (r["mine"][0]["date_start"], r["name"])):
            mine = r["mine"]
            start = mine[0]["date_start"][5:16]
            end = max(m["date_end"] for m in mine)[5:16]
            state = "ACTIVE" if any(m["active"] for m in mine) else "upcoming"
            print(f"{r['name'][:26]:26} {r['pool'][:20]:20} {_gpu_cell(r):>6} "
                  f"{('OK' if r['healthy'] else 'UNHEALTHY'):7} {start+' -> '+end:29} "
                  f"{len(mine):>2} {state:8} {str(mine[0]['title'] or '')[:22]}")
        print(f"\n{len(held)} node(s) reserved by you (ongoing + upcoming).")
        bad_held = [r for r in held if not r["healthy"]]
        if bad_held:
            print(f"\n{len(bad_held)} of them are UNHEALTHY:")
            for r in bad_held:
                print(f"  {r['name'][:26]:26} {r['health_reason'][:70]}")
            print("\n  Release them with:  python cli.py cancel-unhealthy --commit")
        return 0

    if args.unhealthy:
        rows = unhealthy
    elif args.free:
        rows = [r for r in rows if r["free_and_healthy"]]

    def resv(r):
        if r["reserved_now"] is None:
            return "?"
        if r["reserved_now"]:
            return "busy→" + (r["free_at"] or "")[5:16]
        fh = r["free_for_h"]
        return "FREE" if fh == float("inf") else f"free {fh:.0f}h"

    print(f"{'node':26} {'pool':20} {'gpu':>6} {'docker':7} {'reserved':15} health / reason")
    print("-" * 118)
    healthy_free = []
    for r in sorted(rows, key=lambda r: (not r["free_and_healthy"], r["pool"], r["name"])):
        health = "OK" if r["healthy"] else "BAD: " + r["health_reason"]
        print(f"{r['name'][:26]:26} {r['pool'][:20]:20} {_gpu_cell(r):>6} "
              f"{str(r.get('probe_docker') or '-'):7} {resv(r):15} {health[:44]}")
        if r["free_and_healthy"]:
            healthy_free.append(r)

    def _by_class(rows_):
        """Group unhealthy rows by why they failed — the classes mean different things."""
        from conductor_reserve.probe import CLASS_ACCESS, CLASS_BROKEN, CLASS_UNREACHABLE
        blurb = {
            CLASS_BROKEN: "logged in, machine unusable — NOT reserved, safe to release",
            CLASS_UNREACHABLE: "nothing answered — NOT reserved, safe to release",
            CLASS_ACCESS: ("could not log in — health UNVERIFIED, not a fault. "
                           "Still reserved, still kept"),
        }
        for cls in (CLASS_BROKEN, CLASS_UNREACHABLE, CLASS_ACCESS):
            group = [r for r in rows_ if r["health_class"] == cls]
            if not group:
                continue
            print(f"\n  {cls.upper()} ({len(group)}) — {blurb[cls]}")
            for r in sorted(group, key=lambda r: (r["pool"], r["name"])):
                held = " [you hold it now]" if r.get("held_now") else ""
                print(f"    {r['name'][:26]:26} {r['health_reason'][:60]}{held}")

    if args.unhealthy:
        nblocked = sum(1 for r in rows if r.get("blocked"))
        print(f"\n{len(rows)} node(s) failed the health probe — {nblocked} excluded from "
              f"reservation, {len(rows) - nblocked} unverified but still reserved.")
        _by_class(rows)
        print("\nRelease broken/unreachable ones:  python cli.py cancel-unhealthy --commit")
        print("Include the no-access ones too:   python cli.py cancel-unhealthy --include-access --commit")
        return 0

    print(f"\n{len(healthy_free)} free & healthy node(s).")
    if unhealthy and not args.free:
        blocked = [r for r in unhealthy if r.get("blocked")]
        print(f"\n{len(unhealthy)} node(s) failed the health probe "
              f"({len(blocked)} excluded from reservation, "
              f"{len(unhealthy) - len(blocked)} unverified but still reserved):")
        _by_class(unhealthy)
    if healthy_free:
        print("\nCheck GPUs live (copy-paste):")
        for r in healthy_free[:20]:
            print(f"  ssh {ssh_user}@{r['hostname']} 'watch -n 0.2 rocm-smi'")
        print("\nReserve one (copy-paste):")
        for r in healthy_free[:20]:
            print(f"  python cli.py run --commit --node {r['name']}")
    return 0


def _cancel_unhealthy(args) -> int:
    """Find and (with --commit) cancel our reservations on nodes failing the health probe."""
    from conductor_reserve import probe
    from conductor_reserve.engine import CANCELLABLE_CLASSES, cancel_unhealthy
    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    classes = tuple(CANCELLABLE_CLASSES) + ((probe.CLASS_ACCESS,) if args.include_access else ())
    # First pass: probe + find only (no writes) so we can show and confirm.
    res = cancel_unhealthy(cfg, commit=False, classes=classes,
                           progress=lambda m: print("·", m) if args.verbose else None)
    nodes, found, kept = res["unhealthy_nodes"], res["found"], res["kept"]
    print(f"healthy nodes: {res['healthy']} | cancellable unhealthy: {len(nodes)} "
          f"| kept (out of scope): {len(kept)}")
    print(f"classes in scope: {', '.join(res['classes'])}")
    if nodes:
        print("\nunhealthy & in scope (reason):")
        for n in sorted(nodes, key=lambda n: n["name"]):
            print(f"  {n['name'][:26]:26} [{n['cls']:11}] {n['reason'][:62]}")
    if kept:
        print(f"\nkept — unhealthy but out of scope ({len(kept)}), "
              f"reservations left alone{'' if args.include_access else '; add --include-access to release these'}:")
        for n in sorted(kept, key=lambda n: n["name"])[:50]:
            print(f"  {n['name'][:26]:26} [{n['cls']:11}] {n['reason'][:62]}")
    if not found:
        print(f"\nno reservations titled {res['title']!r} on those nodes — nothing to cancel.")
        return 0
    print(f"\nour reservations on unhealthy nodes ({len(found)}):")
    for r in found:
        print(f"  {r['node_name'][:26]:26} {r['date_start'][:16]} -> {r['date_end'][:16]} "
              f"| {r['title']} | {r['id']}")
    if not args.commit:
        print("\n(dry-run) re-run with --commit to cancel these.")
        return 0
    if not args.yes and input(
            f"\nCancel these {len(found)} reservation(s)? Type 'yes': ").strip().lower() != "yes":
        print("aborted.")
        return 1
    # Cancel exactly the ids listed above — don't re-probe (the verdict could shift).
    from conductor_reserve.engine import cancel_ids
    cancelled = cancel_ids([r["id"] for r in found])
    print(f"cancelled {len(cancelled)}/{len(found)} reservation(s)")
    return 0


def _sync_users(args) -> int:
    """Add config's default users to our existing (ongoing+upcoming) reservations."""
    from conductor_reserve.engine import sync_users
    cfg = load_config()
    res = sync_users(cfg, commit=False)   # find only
    rows = res["reservations"]
    print(f"default users to ensure: {len(res['user_ids'])}"
          + (f" | unresolved: {', '.join(res['unresolved'])}" if res["unresolved"] else ""))
    print(f"ongoing/upcoming reservations titled {res['title']!r}: {len(rows)}")
    for r in rows[:60]:
        print(f"  {r['date_start'][:16]} -> {r['date_end'][:16]} | {len(r['users'])} users | {r['id']}")
    if len(rows) > 60:
        print(f"  ... +{len(rows) - 60} more")
    if not rows:
        return 0
    if not args.commit:
        print("\n(dry-run) re-run with --commit to add the default users to these.")
        return 0
    if not args.yes and input(
            f"\nAdd default users to these {len(rows)} reservation(s)? Type 'yes': "
            ).strip().lower() != "yes":
        print("aborted.")
        return 1
    out = sync_users(cfg, commit=True, progress=lambda m: print("·", m) if args.verbose else None)
    ok = sum(1 for x in out["reservations"] if x["status"] == "updated")
    fail = [x for x in out["reservations"] if x["status"] == "failed"]
    print(f"updated {ok}/{len(out['reservations'])} reservation(s)")
    for x in fail[:10]:
        print(f"  FAILED {x['id']}: {x['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
