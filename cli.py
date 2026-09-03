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
    swindow_help = ("also reserve fragmented nodes (default: keep only nodes whose total hold "
                    "gives >policy.min_continuous_hours continuous or <policy.max_gap_hours "
                    "inter-block gaps)")
    p = sub.add_parser("plan", parents=[common], help="dry-run: show the plan, create nothing")
    p.add_argument("--include-small-window", action="store_true", help=swindow_help)
    r = sub.add_parser("run", parents=[common],
                       help="build the plan and (with --commit) create reservations")
    r.add_argument("--commit", action="store_true", help="actually create reservations")
    r.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    r.add_argument("--include-small-window", action="store_true", help=swindow_help)
    r.add_argument("--no-cancel-fragmented", action="store_true",
                   help="don't auto-cancel our reservations on nodes still fragmented after the run "
                        "(cleanup is on by default)")
    sub.add_parser("whoami", parents=[common], help="verify authentication and list your teams")
    sub.add_parser("runs", parents=[common], help="list past run summaries")
    sub.add_parser("denylist", parents=[common],
                   help="show nodes confirmed broken and skipped by run/plan")
    al = sub.add_parser("allow", parents=[common],
                        help="re-enable denylisted node(s) and (with --commit) reserve them to re-test")
    al.add_argument("nodes", nargs="+", metavar="NODE", help="node name(s) to re-enable")
    al.add_argument("--commit", action="store_true",
                    help="also create a fresh reservation on each, so it can be re-tested while active")
    al.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    c = sub.add_parser("cancel-small-gpu", parents=[common],
                       help="cancel OUR current+future reservations on nodes below min_gpus")
    c.add_argument("--commit", action="store_true", help="actually cancel (default: dry-run)")
    c.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    cu = sub.add_parser("cancel-unhealthy", parents=[common],
                        help="cancel OUR reservations on nodes that fail the SSH health probe")
    cu.add_argument("--commit", action="store_true", help="actually cancel (default: dry-run)")
    cu.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    csw = sub.add_parser("cancel-small-window", parents=[common],
                         help="cancel OUR reservations on fragmented nodes "
                              "(no run > min_continuous_hours & a gap >= max_gap_hours)")
    csw.add_argument("--commit", action="store_true", help="actually cancel (default: dry-run)")
    csw.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    su = sub.add_parser("sync-users", parents=[common],
                        help="add config's default users to our existing (ongoing+upcoming) reservations")
    su.add_argument("--commit", action="store_true", help="actually update (default: dry-run)")
    su.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    au = sub.add_parser("add-user", parents=[common],
                        help="add ONE person to our reservations (scope with --node / --pool)")
    au.add_argument("user", metavar="USER", help="email / NTID / \"Last, First\" / UUID to add")
    au.add_argument("--commit", action="store_true", help="actually add (default: dry-run)")
    au.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    st = sub.add_parser("status", parents=[common],
                        help="report which nodes are free & healthy, and why the rest are not")
    st.add_argument("--free", action="store_true", help="show only free & healthy nodes")
    st.add_argument("--unhealthy", action="store_true",
                    help="show only unhealthy nodes, with the reason each one failed")
    st.add_argument("--reserved", action="store_true",
                    help="show only currently-reserved nodes, with who holds them and until when")
    st.add_argument("--continuous", action="store_true",
                    help="only nodes you hold ACTIVE now with gap-free coverage into the future")
    st.add_argument("--active_n_healthy", action="store_true",
                    help="probe ONLY nodes you hold an ACTIVE reservation on — the one state where "
                         "an SSH failure is a real verdict; with --commit, denylist + release any "
                         "still-unhealthy held node")
    st.add_argument("--fast", action="store_true", help="skip the reservation check (health only)")
    st.add_argument("--commit", action="store_true",
                    help="with --active_n_healthy: actually denylist + release the bad held nodes "
                         "(default: dry-run)")
    st.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
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

    if args.cmd == "denylist":
        from conductor_reserve import denylist
        d = denylist.load()
        if not d:
            print("denylist is empty — no nodes are being skipped.")
            return 0
        print(f"{len(d)} node(s) denylisted (skipped by run/plan):")
        for name, info in sorted(d.items()):
            print(f"  {name:26} [{info.get('cls', ''):7}] {info.get('reason', '')[:50]:50} "
                  f"since {info.get('added', '')}")
        print("\nRe-enable one with:  python cli.py allow <node>")
        return 0

    if args.cmd == "allow":
        return _allow(args)

    if args.cmd == "cancel-small-gpu":
        return _cancel_small_gpu(args)

    if args.cmd == "cancel-unhealthy":
        return _cancel_unhealthy(args)

    if args.cmd == "cancel-small-window":
        return _cancel_small_window(args)

    if args.cmd == "sync-users":
        return _sync_users(args)

    if args.cmd == "add-user":
        return _add_user(args)

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

    # The window filter is the default; naming nodes explicitly (--node) or opting in with
    # --include-small-window bypasses it — in both cases you clearly want those nodes.
    filter_windows = not getattr(args, "include_small_window", False) and not getattr(args, "node", None)
    result = run(cfg, commit=commit, node_names=getattr(args, "node", None),
                 probe_health=not args.no_probe, filter_windows=filter_windows,
                 cancel_fragmented=not getattr(args, "no_cancel_fragmented", False),
                 progress=lambda m: print("·", m) if args.verbose else None)
    print()
    print(notify.format_console(result))
    sw = result.cancelled_fragmented
    if sw and sw["found"]:
        verb = "cancelled" if sw["committed"] else "would cancel"
        print(f"cleanup: {verb} {len(sw['found'])} reservation(s) on {len(sw['fragmented'])} "
              f"still-fragmented node(s): {', '.join(n['name'] for n in sw['fragmented'])}")
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


def _cancel_small_window(args) -> int:
    """Find and (with --commit) cancel our reservations on fragmented (small-window) nodes."""
    from conductor_reserve.engine import cancel_ids, cancel_small_window
    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    # First pass: find only (no writes) so we can show and confirm.
    res = cancel_small_window(cfg, commit=False,
                              progress=lambda m: print("·", m) if args.verbose else None)
    frag, found = res["fragmented"], res["found"]
    print(f"fragmented nodes (no run > {res['min_continuous_hours']:.0f}h continuous "
          f"& a gap >= {res['max_gap_hours']:.0f}h): {len(frag)} "
          f"| our reservations on them: {len(found)}")
    if frag:
        print("\nfragmented & in scope:")
        for n in sorted(frag, key=lambda n: n["name"]):
            print(f"  {n['name'][:26]:26} best {n['longest_h']:>5.1f}h continuous, "
                  f"largest gap {n['gap_h']:>5.1f}h  ({n['count']} resv)")
    if not found:
        print(f"\nno reservations titled {res['title']!r} on fragmented nodes — nothing to cancel.")
        return 0
    print(f"\nour reservations on fragmented nodes ({len(found)}):")
    for r in sorted(found, key=lambda r: r["node_name"]):
        print(f"  {r['node_name'][:26]:26} {r['date_start'][:16]} -> {r['date_end'][:16]} "
              f"| {r['id']}")
    if not args.commit:
        print("\n(dry-run) re-run with --commit to cancel these.")
        return 0
    if not args.yes and input(
            f"\nCancel these {len(found)} reservation(s)? Type 'yes': ").strip().lower() != "yes":
        print("aborted.")
        return 1
    # Cancel exactly the ids listed above (fragmentation is transient; no denylisting).
    cancelled = cancel_ids([r["id"] for r in found])
    print(f"cancelled {len(cancelled)}/{len(found)} reservation(s)")
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
    if getattr(args, "active_n_healthy", False):
        return _active_n_healthy(args)
    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    ssh_user = args.ssh_user or cfg.get("ssh_user") or os.getenv("USER") or "<your-ntid>"
    if (args.reserved or args.continuous) and args.fast:
        print("--reserved/--continuous need the reservation check; ignoring --fast.")
        args.fast = False
    # For "reserved by me" / "continuous" show the full future holdings, not just the next 48h.
    window_hours = 48
    if args.reserved or args.continuous:
        window_hours = max(48, int(cfg.get("policy", {}).get("default_horizon_days", 14)) * 24 + 48)
    rows = status_report(cfg, check_reservations=not args.fast, window_hours=window_hours,
                         probe_health=not args.no_probe,
                         progress=lambda m: print("·", m) if args.verbose else None)
    unhealthy = [r for r in rows if not r["healthy"]]

    if args.continuous:
        held = [r for r in rows if r.get("held_now") and r.get("held_continuous")]
        gappy = [r for r in rows if r.get("held_now") and not r.get("held_continuous")]
        print(f"{'node':26} {'pool':20} {'gpu':>6} {'health':9} "
              f"{'continuous until (UTC)':22} {'#':>2}")
        print("-" * 92)
        for r in sorted(held, key=lambda r: (r.get("held_until") or "", r["name"])):
            until = (r.get("held_until") or "")[5:16]
            print(f"{r['name'][:26]:26} {r['pool'][:20]:20} {_gpu_cell(r):>6} "
                  f"{('OK' if r['healthy'] else 'UNHEALTHY'):9} {until:22} {len(r['mine']):>2}")
        print(f"\n{len(held)} node(s) held ACTIVE now with gap-free coverage into the future.")
        if gappy:
            print(f"\n{len(gappy)} node(s) active now but with a GAP ahead (excluded — our hold "
                  f"lapses then resumes):")
            for r in sorted(gappy, key=lambda r: r["name"]):
                print(f"  {r['name'][:26]:26} held until {(r.get('held_until') or '')[5:16]}, "
                      f"then a later block")
        return 0

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
        from conductor_reserve.probe import (CLASS_ACCESS, CLASS_BROKEN, CLASS_EXCLUDED,
                                             CLASS_UNCHECKED, CLASS_UNREACHABLE)
        blurb = {
            CLASS_BROKEN: "logged in, machine unusable — NOT reserved, safe to release",
            CLASS_UNREACHABLE: "nothing answered — NOT reserved, safe to release",
            CLASS_EXCLUDED: "below the GPU minimum — never probed; see `cancel-small-gpu`",
            CLASS_UNCHECKED: "probe skipped (--no-probe) — verdict is Conductor's scraped data",
            CLASS_ACCESS: ("could not log in — health UNVERIFIED, not a fault. "
                           "Still reserved, still kept"),
        }
        for cls in (CLASS_BROKEN, CLASS_UNREACHABLE, CLASS_EXCLUDED, CLASS_UNCHECKED,
                    CLASS_ACCESS):
            group = [r for r in rows_ if r["health_class"] == cls]
            if not group:
                continue
            print(f"\n  {cls.upper()} ({len(group)}) — {blurb[cls]}")
            for r in sorted(group, key=lambda r: (r["pool"], r["name"])):
                held = " [you hold it now]" if r.get("held_now") else ""
                print(f"    {r['name'][:26]:26} {r['health_reason'][:60]}{held}")

    if args.unhealthy:
        nblocked = sum(1 for r in rows if r.get("blocked"))
        print(f"\n{len(rows)} node(s) not healthy — {nblocked} excluded from "
              f"reservation, {len(rows) - nblocked} unverified but still reserved.")
        _by_class(rows)
        print("\nRelease broken/unreachable ones:  python cli.py cancel-unhealthy --commit")
        print("Judge no-access nodes you HOLD:    python cli.py status --active_n_healthy --commit")
        return 0

    print(f"\n{len(healthy_free)} free & healthy node(s).")
    if unhealthy and not args.free:
        blocked = [r for r in unhealthy if r.get("blocked")]
        print(f"\n{len(unhealthy)} node(s) not healthy "
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
    # Only genuine machine faults (broken/unreachable). A can't-log-in (access) failure is
    # NOT decisive for a node we don't actively hold — SSH is gated on holding it — so it is
    # never cancelled here; judge those with `status --active_n_healthy` while they're active.
    classes = tuple(CANCELLABLE_CLASSES)
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
        print(f"\nkept — look unhealthy but out of scope ({len(kept)}), reservations left alone "
              f"(access failures are only decisive while you HOLD the node — see "
              f"`status --active_n_healthy`):")
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
    # Record the confirmed-broken ones so future run/plan skip them for good.
    from conductor_reserve import denylist
    newly = denylist.add([{"name": n["name"], "hostname": n.get("hostname"),
                           "reason": n["reason"], "cls": n["cls"]}
                          for n in nodes if n["cls"] == probe.CLASS_BROKEN])
    if newly:
        print(f"denylisted {len(newly)} broken node(s) — run/plan won't reserve them again: "
              f"{', '.join(newly)}")
        print("  (undo with:  python cli.py allow <node>)")
    return 0


def _allow(args) -> int:
    """Re-enable denylisted node(s), and (with --commit) reserve them so they can be re-tested.

    Removing from the denylist is local and reversible, so it happens immediately. The
    reservation is the real action on shared infra, so it needs --commit (+ typed yes). We
    reserve with --no-probe on purpose: the point is to *gain* the access needed to test the
    node, so we must book it regardless of what a probe says while we don't hold it yet.
    """
    from conductor_reserve import denylist
    from conductor_reserve.engine import run
    for n in args.nodes:
        print(f"{'removed from denylist' if denylist.remove(n) else 'not on denylist'}: {n}")

    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    commit = bool(args.commit)
    if commit and not args.yes:
        print("\n>>> COMMIT MODE: this will create real reservations to re-test these nodes.")
        if input(">>> Type 'yes' to proceed: ").strip().lower() != "yes":
            print("aborted (nodes stay removed from the denylist; nothing was reserved).")
            return 1
    # No window filter here: the whole point is to re-book a specific node to re-test it,
    # so we reserve it regardless of how fragmented its window looks.
    result = run(cfg, commit=commit, node_names=args.nodes, probe_health=False,
                 filter_windows=False,
                 progress=lambda m: print("·", m) if args.verbose else None)
    print()
    print(notify.format_console(result))
    if commit:
        print("\nReservations kicked off — they go ACTIVE after policy.start_lead_minutes.")
        print("Once active, re-test with:  python cli.py status --active_n_healthy --commit")
    else:
        print("\n(dry-run) re-run with --commit to actually reserve & re-test these nodes.")
    return 0


def _active_n_healthy(args) -> int:
    """`status --active_n_healthy`: probe nodes we hold ACTIVE now; with --commit, denylist +
    release any still unhealthy. This is the one place a can't-log-in (access) failure is a real
    verdict — the node is allocated to us, so the reservation-gating excuse is gone."""
    from conductor_reserve import denylist
    from conductor_reserve.engine import cancel_ids, verify_held
    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    res = verify_held(cfg, progress=lambda m: print("·", m) if args.verbose else None)
    bad, found = res["bad_nodes"], res["to_cancel"]
    print(f"actively held: {res['held']} | healthy: {res['healthy_held']} "
          f"| still-unhealthy while held: {len(bad)}")
    if not bad:
        print("every node you actively hold is healthy — nothing to denylist or release.")
        return 0
    print("\nunhealthy WHILE WE HOLD IT (genuinely bad — will be denylisted + released):")
    for n in sorted(bad, key=lambda n: n["name"]):
        print(f"  {n['name'][:26]:26} [{n['cls']:11}] {n['reason'][:60]}")
    print(f"\nour reservations to release on them ({len(found)}):")
    for r in sorted(found, key=lambda r: r["node_name"]):
        print(f"  {r['node_name'][:26]:26} {r['date_start'][:16]} -> {r['date_end'][:16]} | {r['id']}")
    if not args.commit:
        print("\n(dry-run) re-run with --commit to denylist + release these.")
        return 0
    if not args.yes and input(
            f"\nDenylist + release these {len(bad)} node(s)? Type 'yes': ").strip().lower() != "yes":
        print("aborted.")
        return 1
    # Act on exactly the ids/nodes shown above — don't re-probe (the live verdict can shift).
    cancelled = cancel_ids([r["id"] for r in found])
    print(f"released {len(cancelled)}/{len(found)} reservation(s)")
    newly = denylist.add([{"name": n["name"], "hostname": n.get("hostname"),
                           "reason": n["reason"], "cls": n["cls"]} for n in bad])
    if newly:
        print(f"denylisted {len(newly)} node(s) — run/plan won't reserve them again: "
              f"{', '.join(newly)}")
        print("  (undo with:  python cli.py allow <node>)")
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


def _add_user(args) -> int:
    """Add ONE person to our reservations, scoped by --node / --pool (default: all)."""
    from conductor_reserve.engine import add_user
    cfg = load_config()
    if getattr(args, "pool", None):
        cfg = _filter_pools(cfg, args.pool)
    nodes = getattr(args, "node", None)
    res = add_user(cfg, user=args.user, node_names=nodes, commit=False)  # find only
    if res["unresolved"]:
        print(f"could not resolve user {args.user!r} — check the email / NTID / name.")
        return 1
    scope = ("nodes " + ", ".join(nodes)) if nodes else "ALL our reservations"
    print(f"add {res['user']!r} to reservations titled {res['title']!r} on {scope}: "
          f"{len(res['found'])} reservation(s) on {len(res['nodes'])} node(s)")
    for n in res["nodes"]:
        print(f"  {n['name'][:32]:32} {n['count']} resv")
    if not res["found"]:
        print("\nno matching reservations — nothing to do.")
        return 0
    if not args.commit:
        print("\n(dry-run) re-run with --commit to add the user to these.")
        return 0
    if not args.yes and input(
            f"\nAdd {res['user']!r} to these {len(res['found'])} reservation(s)? Type 'yes': "
            ).strip().lower() != "yes":
        print("aborted.")
        return 1
    out = add_user(cfg, user=args.user, node_names=nodes, commit=True,
                   progress=lambda m: print("·", m) if args.verbose else None)
    print(f"added user to {len(out['updated'])}/{len(out['found'])} reservation(s)")
    for x in out["failed"][:10]:
        print(f"  FAILED {x['id']}: {x['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
