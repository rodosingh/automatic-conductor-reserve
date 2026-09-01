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

from flask import Flask, redirect, render_template, request, url_for

from conductor_reserve import notify
from conductor_reserve.config import load_config
from conductor_reserve.engine import run, cancel_small_gpu

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = Flask(__name__)

# Simple in-memory state for the last run + a lock so two clicks don't overlap.
_lock = threading.Lock()
_last = {"result": None, "log": [], "running": False, "cancel": None}


def _do_run(commit: bool):
    cfg = load_config()
    _last["log"] = []
    _last["running"] = True
    try:
        result = run(cfg, commit=commit, progress=lambda m: _last["log"].append(m))
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


@app.route("/")
def index():
    return render_template(
        "index.html",
        result=_last["result"],
        cancel=_last["cancel"],
        log=_last["log"],
        running=_last["running"],
        runs=notify.list_runs()[:10],
        config=_safe_config(),
    )


@app.route("/plan", methods=["POST"])
def plan():
    if not _last["running"]:
        with _lock:
            _do_run(commit=False)
    return redirect(url_for("index"))


@app.route("/commit", methods=["POST"])
def commit():
    if request.form.get("confirm") != "on":
        _last["log"] = ["Refused: you must tick the confirm box to create real reservations."]
        return redirect(url_for("index"))
    if not _last["running"]:
        with _lock:
            _do_run(commit=True)
    return redirect(url_for("index"))


@app.route("/cancel", methods=["POST"])
def cancel():
    """Find our reservations on sub-min_gpus nodes (dry-run), or cancel them if confirmed."""
    do_commit = request.form.get("confirm") == "on"
    if not _last["running"]:
        with _lock:
            _do_cancel(commit=do_commit)
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
