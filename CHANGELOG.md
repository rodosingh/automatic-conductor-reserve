# Changelog

Rationale for notable changes — **why**, not just what. Commit messages cover the mechanics;
this file explains the reasoning so future readers (and teammates) know the intent.

## Change process

- New work is developed on a **branch**, not committed straight to `main`.
- Each change records its **reason** here (and/or in the README / HOW_TO_RUN docs it touches).
- Branches are merged via a **pull request** so there's a review point and a paper trail.

---

## Cron auto-booking (`book_team.py` + `install_cron.sh` + `run_book_team_cron.sh`)

### 2026-10-16 — One combined run log per tick
**Why:** every per-credential worker wrote its own `runs/run-*.jsonl`, so a single cron tick
dropped one file per teammate (6+). With a 15-minute cadence that was hundreds of tiny files a
day. Now all workers in a tick append to a single `runs/run-<ts>-team-<mode>.jsonl` (one record
set per assignment, with an aggregate team summary prepended so the web history reads it as one
entry). Same total content, far fewer files.
_Touches:_ `conductor_reserve/notify.py` (`BOOK_TEAM_RUN_LOG` append + `combine_team_run_log`),
`book_team.py`.

### 2026-10-16 — Prune old run artifacts each tick; retention set to 1 day
**Why:** `runs/` grew without bound (at least one artifact every tick, forever). The cron tick
now deletes `runs/*.jsonl` older than `BOOK_TEAM_RUNS_RETENTION_DAYS` before booking. The prune
lives in the runner (same cron job, no second crontab entry) and is guarded so a cleanup error
can never block a booking. Retention is baked into the crontab and validated at install time;
set to **1 day** on this host after the combined-log change.
_Touches:_ `run_book_team_cron.sh`, `install_cron.sh`.

### 2026-09-21 — Configurable cron interval
**Why:** the 15-minute cadence was hardcoded as `*/15` in `install_cron.sh`. Exposed
`BOOK_TEAM_INTERVAL_MIN` (every N minutes) and `BOOK_TEAM_SCHEDULE` (full 5-field cron) so the
schedule can change without editing the script, with validation before the crontab is touched.
Note: `*/N` where N doesn't divide 60 restarts at the top of each hour (e.g. `*/55` fires at
`:55` and `:00`), which is why clean divisors like 15 are preferred.
_Touches:_ `install_cron.sh`, README + HOW_TO_RUN override tables.

### 2026-09-18 — Cron installer pinned to `conductor-venv`
**Why:** cron runs with a bare environment — no activated venv, minimal `PATH` — so a plain
`python` resolves to `/usr/bin/python3`, which lacks `conductor_sdk`. The installer bakes the
venv interpreter into the crontab (`CONDUCTOR_PYTHON`/`CONDUCTOR_VENV`) and `run_book_team_cron.sh`
pins `VIRTUAL_ENV` / `PATH` and unsets `PYTHONHOME`/`PYTHONPATH` before exec'ing it. Install
refuses any interpreter that can't `import conductor_sdk`. Overlapping ticks `flock` a
user-owned lock (under `~/.cache/`, not shared `/tmp`) and log a skip instead of stacking.
_Touches:_ `install_cron.sh`, `run_book_team_cron.sh`, README + HOW_TO_RUN.

### 2026-09-18 — Documented a safe stop one-liner
**Why:** `crontab -e` is error-prone and `crontab -r` wipes **every** job. Documented a filter
that removes only the book_team job (the same match the installer uses), plus how to pause with
`#` and the fact that stopping never cancels reservations already booked.
_Touches:_ README, HOW_TO_RUN.

---

## Housekeeping

### 2026-09-21 — Stripped AI co-author trailers from history
**Why:** commits had carried `Co-authored-by: Cursor` / `Co-Authored-By: Claude` trailers. These
were removed from every commit message (content unchanged) so the history reflects the human
author only. The trailer is injected by the Cursor IDE's commit integration, not a git
hook/template — disable it in Cursor settings to stop it recurring.
