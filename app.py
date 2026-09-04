#!/usr/bin/env python
"""Flask control app for Conductor auto-reservation.

Manual-trigger only. Two actions:
  - "Dry-run plan"    -> shows exactly what WOULD be reserved (no writes; the default)
  - "Reserve for real"-> creates the reservations (requires the confirm checkbox)

Run:  python app.py    then open http://127.0.0.1:5057
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from flask import Flask, redirect, render_template, request, url_for

from conductor_reserve import denylist, notify
from conductor_reserve.config import load_config
from conductor_reserve.engine import (CANCELLABLE_CLASSES, add_user, cancel_ids, cancel_small_gpu,
                                      cancel_small_window, cancel_unhealthy, format_durations, run,
                                      status_report, sync_users, verify_held)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = Flask(__name__)

# Simple in-memory state for the last run + a lock so two clicks don't overlap.
_lock = threading.Lock()
_last = {"result": None, "log": [], "running": False, "cancel": None, "sync": None,
         "reserved": None, "health": None, "unhealthy": None, "active_n_healthy": None,
         "denylist": None, "allow": None, "small_window": None, "add_user": None}


def _do_run(commit: bool, filter_windows: bool = True, cancel_fragmented: bool = True):
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        result = run(cfg, commit=commit, filter_windows=filter_windows,
                     cancel_fragmented=cancel_fragmented,
                     progress=lambda m: _last["log"].append(m))
        _last["result"] = result
    finally:
        _last["running"] = False


def _do_cancel(commit: bool):
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        _last["cancel"] = cancel_small_gpu(cfg, commit=commit,
                                           progress=lambda m: _last["log"].append(m))
    finally:
        _last["running"] = False


def _do_sync(commit: bool):
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        _last["sync"] = sync_users(cfg, commit=commit,
                                   progress=lambda m: _last["log"].append(m))
    finally:
        _last["running"] = False


def _do_add_user(user: str, nodes: list, commit: bool, future: bool = False):
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        _last["add_user"] = add_user(cfg, user=user, node_names=nodes or None, future=future,
                                     commit=commit, progress=lambda m: _last["log"].append(m))
    finally:
        _last["running"] = False


def _do_reserved():
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        window = max(48, int(cfg.get("policy", {}).get("default_horizon_days", 14)) * 24 + 48)
        rows = status_report(cfg, window_hours=window,
                             progress=lambda m: _last["log"].append(m))
        now = datetime.now(timezone.utc)
        reserved = [r for r in rows if r["reserved_by_me"]]
        for r in reserved:
            r["durations"] = format_durations(r["mine"], now)
        _last["reserved"] = reserved
    finally:
        _last["running"] = False


def _do_health():
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        _last["health"] = status_report(cfg, progress=lambda m: _last["log"].append(m))
    finally:
        _last["running"] = False


def _do_cancel_unhealthy(commit: bool):
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    # Only genuine machine faults (broken/unreachable). A can't-log-in (access) failure is not
    # decisive for a node we don't actively hold — judge those with the active-&-healthy card.
    try:
        _last["unhealthy"] = cancel_unhealthy(cfg, commit=commit, classes=CANCELLABLE_CLASSES,
                                              progress=lambda m: _last["log"].append(m))
    finally:
        _last["running"] = False


def _do_cancel_small_window(commit: bool):
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        _last["small_window"] = cancel_small_window(cfg, commit=commit,
                                                    progress=lambda m: _last["log"].append(m))
    finally:
        _last["running"] = False


def _do_active_n_healthy(commit: bool):
    """`status --active_n_healthy`: probe the nodes we hold ACTIVE now; with commit, denylist +
    release the bad ones.

    Report-only find first, then act on exactly that list (cancel_ids + denylist.add) so a
    second live probe can't shift the verdict between what was shown and what is written.
    """
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        res = verify_held(cfg, progress=lambda m: _last["log"].append(m))
        cancelled, denylisted = [], []
        if commit and res["bad_nodes"]:
            cancelled = cancel_ids([r["id"] for r in res["to_cancel"]])
            denylisted = denylist.add(
                [{"name": n["name"], "hostname": n.get("hostname"), "reason": n["reason"],
                  "cls": n["cls"]} for n in res["bad_nodes"]])
        res.update(committed=bool(commit), cancelled=cancelled, denylisted=denylisted)
        _last["active_n_healthy"] = res
        _last["denylist"] = denylist.load()  # refresh the viewer after a change
    finally:
        _last["running"] = False


def _do_denylist():
    _last["log"] = []
    _last["denylist"] = denylist.load()


def _do_allow(nodes: list, commit: bool):
    """Remove node(s) from the denylist, and (with commit) reserve them to re-test while active."""
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        removed = [n for n in nodes if denylist.remove(n)]
        _last["denylist"] = denylist.load()
        result = None
        if commit:
            # No window filter: we're re-booking specific nodes to re-test them.
            result = run(cfg, commit=True, node_names=nodes, probe_health=False,
                         filter_windows=False, progress=lambda m: _last["log"].append(m))
        _last["allow"] = {"nodes": nodes, "removed": removed, "committed": bool(commit),
                          "result": result}
    finally:
        _last["running"] = False


@app.route("/")
def index():
    return render_template(
        "index.html",
        result=_last["result"],
        cancel=_last["cancel"],
        sync=_last["sync"],
        reserved=_last["reserved"],
        health=_last["health"],
        unhealthy=_last["unhealthy"],
        active_n_healthy=_last["active_n_healthy"],
        denylist=_last["denylist"],
        allow=_last["allow"],
        small_window=_last["small_window"],
        add_user=_last["add_user"],
        log=_last["log"],
        running=_last["running"],
        runs=notify.list_runs()[:10],
        config=_safe_config(),
    )


@app.route("/plan", methods=["POST"])
def plan():
    filter_windows = request.form.get("include_small_window") != "on"
    if not _last["running"]:
        with _lock:
            _do_run(commit=False, filter_windows=filter_windows)
    return redirect(url_for("index"))


@app.route("/commit", methods=["POST"])
def commit():
    if request.form.get("confirm") != "on":
        _last["log"] = ["Refused: you must tick the confirm box to create real reservations."]
        return redirect(url_for("index"))
    filter_windows = request.form.get("include_small_window") != "on"
    cancel_fragmented = request.form.get("no_cancel_fragmented") != "on"
    if not _last["running"]:
        with _lock:
            _do_run(commit=True, filter_windows=filter_windows,
                    cancel_fragmented=cancel_fragmented)
    return redirect(url_for("index"))


@app.route("/cancel", methods=["POST"])
def cancel():
    """Find our reservations on sub-min_gpus nodes (dry-run), or cancel them if confirmed."""
    do_commit = request.form.get("confirm") == "on"
    if not _last["running"]:
        with _lock:
            _do_cancel(commit=do_commit)
    return redirect(url_for("index"))


@app.route("/sync-users", methods=["POST"])
def sync_users_route():
    """List our reservations that would get the default users (dry-run), or update if confirmed."""
    do_commit = request.form.get("confirm") == "on"
    if not _last["running"]:
        with _lock:
            _do_sync(commit=do_commit)
    return redirect(url_for("index"))


@app.route("/add-user", methods=["POST"])
def add_user_route():
    """Add one person to our reservations (dry-run), scoped by node(s); write if confirmed."""
    user = (request.form.get("user") or "").strip()
    nodes = (request.form.get("nodes") or "").replace(",", " ").split()
    do_commit = request.form.get("confirm") == "on"
    future = request.form.get("future") == "on"
    # --future is per-node: ignore it (with a note) if no node was named.
    if future and not nodes:
        _last["log"] = ["'future' needs at least one node — ignored (nothing to persist for ALL)."]
        future = False
    if user and not _last["running"]:
        with _lock:
            _do_add_user(user, nodes, commit=do_commit, future=future)
    return redirect(url_for("index"))


@app.route("/status", methods=["POST"])
def status_route():
    """Node health report: which nodes are free & healthy, and why the rest failed."""
    if not _last["running"]:
        with _lock:
            _do_health()
    return redirect(url_for("index"))


@app.route("/cancel-unhealthy", methods=["POST"])
def cancel_unhealthy_route():
    """List our reservations on probe-failing nodes (dry-run), or cancel them if confirmed."""
    do_commit = request.form.get("confirm") == "on"
    if not _last["running"]:
        with _lock:
            _do_cancel_unhealthy(commit=do_commit)
    return redirect(url_for("index"))


@app.route("/cancel-small-window", methods=["POST"])
def cancel_small_window_route():
    """List our reservations on fragmented nodes (dry-run), or cancel them if confirmed."""
    do_commit = request.form.get("confirm") == "on"
    if not _last["running"]:
        with _lock:
            _do_cancel_small_window(commit=do_commit)
    return redirect(url_for("index"))


@app.route("/reserved", methods=["POST"])
def reserved_route():
    """Show the nodes reserved by me (ongoing + upcoming). Read-only."""
    if not _last["running"]:
        with _lock:
            _do_reserved()
    return redirect(url_for("index"))


@app.route("/active-n-healthy", methods=["POST"])
def active_n_healthy_route():
    """Probe nodes we hold active now (dry-run), or denylist+release the bad ones if confirmed."""
    do_commit = request.form.get("confirm") == "on"
    if not _last["running"]:
        with _lock:
            _do_active_n_healthy(commit=do_commit)
    return redirect(url_for("index"))


@app.route("/denylist", methods=["POST"])
def denylist_route():
    """Show the persistent denylist that run/plan skip unconditionally. Read-only."""
    with _lock:
        _do_denylist()
    return redirect(url_for("index"))


@app.route("/allow", methods=["POST"])
def allow_route():
    """Remove node(s) from the denylist, and (with confirm) reserve them to re-test."""
    nodes = (request.form.get("nodes") or "").replace(",", " ").split()
    do_commit = request.form.get("confirm") == "on"
    if nodes and not _last["running"]:
        with _lock:
            _do_allow(nodes, commit=do_commit)
    return redirect(url_for("index"))


def _safe_config():
    try:
        cfg = load_config()
        r = cfg["reservation"]
        return {
            "title": r["title"], "project": r["project"], "milestone": r["milestone"],
            "team_name": r["team_name"], "users": r.get("users", []),
            "batch_opt_out": r.get("batch_opt_out", True),
            "pools": [p["id"] for p in cfg["pools"]], "policy": cfg.get("policy", {}),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False)
