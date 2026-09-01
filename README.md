# Hyperloom Auto-Reserve

Automatically reserves eligible **standalone Conductor nodes** across the configured pools,
greedily filling each node's free time up to the pool's future horizon. Pure REST via the
official **Conductor Python SDK** — no browser, no cookies, nothing that expires.

> **Dry-run is the default.** Nothing is ever created unless you explicitly commit
> (CLI `--commit` + typed `yes`, or the web **Reserve for real** checkbox).

- **[HOW_TO_RUN.md](HOW_TO_RUN.md)** — step-by-step run guide + how to hand it to colleagues.
- **[DISCOVERY_LOG.md](DISCOVERY_LOG.md)** — the full build story (every dead-end, fix, learning).

## Setup

Already done on this machine, but for reference:

```bash
# SDK (into base conda):
pip install conductor_sdk flask pyyaml \
  --index-url https://mkmartifactory.amd.com/artifactory/api/pypi/hw-orc3pypi-prod-local/simple \
  --extra-index-url https://pypi.org/simple \
  --trusted-host mkmartifactory.amd.com
```

Credentials live in **`.env`** (mode 600, gitignored):

```
AMD_EMAIL=you@amd.com
ATS_SECRET=<your Conductor API key>     # Conductor UI > profile > API key
VERIFY_CERTS=false
```

Config lives in **`config.yaml`** (gitignored — keep your real values local):

```bash
cp config.example.yaml config.yaml     # then edit: team_name, users, pool ids, only_nodes
```

## Use

### Command line
```bash
python cli.py whoami                 # verify auth + list your teams
python cli.py status                 # which nodes are free & healthy (read-only report)
python cli.py status --free          # only free & healthy + copy-paste rocm-smi / reserve cmds
python cli.py plan                   # DRY-RUN: show exactly what would be reserved (no writes)
python cli.py run --commit           # create the reservations (asks for a typed 'yes')
python cli.py run --commit --node N  # reserve a single node picked from `status`
python cli.py cancel-small-gpu       # cancel our reservations on excluded (sub-min_gpus) nodes
python cli.py sync-users             # add config's default users to existing reservations
python cli.py runs                   # list past run summaries
```

Shared flags on `plan` / `run` (and where noted): `--pool <id>` (repeatable, restrict pools),
`--node <name>` (repeatable, restrict to specific nodes — short name or full hostname),
`--yes` (skip the confirm prompt), `-v` (live progress). `run`/`cancel-small-gpu`/`sync-users`
are **dry-run** until you add `--commit`.

### Web control app
```bash
python app.py                 # http://127.0.0.1:5057
```
- **Dry-run plan** / **Reserve for real** — plan table, then create (confirm box) with a live
  per-node created/failed table + run history.
- **Cancel card** — find/cancel our reservations on sub-`min_gpus` nodes.
- **Sync-users card** — add the config's default users to existing reservations.
- Node-eligibility table shows each node's GPU count + include/exclude reason.

Every run also writes a JSONL log to `runs/run-<timestamp>-<mode>.jsonl`.

## Configuration (`config.yaml`)

| Field | Meaning |
|---|---|
| `reservation.title` | "Reservation Title" (free text, ≥3 chars) |
| `reservation.project` | "Project" (free text, ≥3 chars — not validated against a list) |
| `reservation.description` | "Reservation Description" (optional) |
| `reservation.milestone` | "Milestone / Deadline" `YYYY-MM-DD` (must be ≥ every reservation end) |
| `reservation.team_name` | "Team for Allocation" (must be one of your teams) |
| `reservation.batch_opt_out` | the "Disable batch jobs…" checkbox |
| `reservation.users` | extra users — **email, NTID, "Last, First" name, or UUID** (you're auto-included) |
| `pools[].id` | pool UUIDs to sweep |
| `pools[].only_nodes` | *(optional)* restrict this pool to just these nodes (short name or full hostname; domain ignored). Omit = all eligible nodes in the pool. |
| `policy.max_nodes` | cap nodes touched per run (`null` = all) |
| `policy.max_reservations_per_node` | cap chained reservations per node (`null` = fill horizon) |
| `policy.min_reservation_minutes` | skip free gaps shorter than this |
| `policy.start_lead_minutes` | never start earlier than now + this |
| `policy.min_gpus` | exclude nodes with fewer than this many GPUs |
| `policy.default_duration_hours` / `default_horizon_days` | fallback limits for pools that set no `reservation_duration_limit` / `furthest_future_reservation` (keeps greedy fill bounded) |

## How eligibility & scheduling work

- **Eligible pool:** `reservation_strategy == "calendar"`, not archived, `block_api_access`
  false, group restrictions met.
- **Eligible node:** in an eligible pool, not archived, and with at least `policy.min_gpus`
  GPUs. (Cosmetic `status` is ignored — the SDK states it must not affect reservations.
  `reservation_only` nodes are included.)
- **Greedy plan (per node):** tile the node's free time — read from the **actual reservation
  list** — with back-to-back reservations of up to `pool.reservation_duration_limit`, each
  **ending by `now + pool.furthest_future`** (a hard server cap on `date_end`), rounded to
  10-minute marks. Pools that cap at 48h yield ~one block ending at the 48h mark; pools with
  no limit chain out to `policy.default_horizon_days`.
- **Commit:** creates each reservation individually via the SDK; a `422 overlaps` means the
  slot is already taken (never double-booked), any other error is reported per node.

## Do we need an LLM "agent"?

**No.** The task is fully deterministic — enumerate, check eligibility, compute slots from
hard rules, create. A plain Python service is more **reliable** (no hallucinated bookings),
**cheaper**, **auditable**, and **safe** than an LLM agent, and it's what runs here. An LLM
agent would only earn its place if you later want *natural-language* control
("reserve all idle MI300 nodes for Priya next week") — in which case wrap these same
functions as tools behind an LLM, or use Conductor's own **MCP server**. The reservation
logic stays exactly this code either way.
