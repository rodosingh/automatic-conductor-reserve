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
python cli.py status                 # which nodes are free & healthy + why the rest are not
python cli.py status --free          # only free & healthy + copy-paste rocm-smi / reserve cmds
python cli.py status --unhealthy     # only unhealthy nodes, with the reason each one failed
python cli.py status --reserved      # only nodes YOU have reserved (ongoing + upcoming)
python cli.py plan                   # DRY-RUN: show exactly what would be reserved (no writes)
python cli.py run --commit           # create the reservations (asks for a typed 'yes')
python cli.py run --commit --node N  # reserve a single node picked from `status`
python cli.py cancel-unhealthy       # release nodes the probe proves are broken/unreachable
python cli.py cancel-small-gpu       # cancel our reservations on excluded (sub-min_gpus) nodes
python cli.py verify                 # probe nodes you HOLD now; denylist+release any still unhealthy
python cli.py denylist               # show confirmed-bad nodes that run/plan now skip
python cli.py allow N                # re-enable a denylisted node (add --commit to also re-reserve it)
python cli.py sync-users             # add config's default users to existing reservations
python cli.py runs                   # list past run summaries
```

Shared flags on `plan` / `run` (and where noted): `--pool <id>` (repeatable, restrict pools),
`--node <name>` (repeatable, restrict to specific nodes — short name or full hostname),
`--no-probe` (skip the SSH health probe), `--include-small-window` (also reserve fragmented
nodes — see below), `--yes` (skip the confirm prompt), `-v` (live progress).
`run`/`cancel-unhealthy`/`cancel-small-gpu`/`sync-users` are **dry-run** until you add
`--commit`.

### Skip fragmented nodes (window filter)

With a large team you accumulate a lot of small, scattered reservations. By default `plan`
and `run` **keep only nodes worth holding**: a node is reserved only if our **total hold**
(existing reservations + the blocks this run would add) gives either

- one **continuous stretch longer than `policy.min_continuous_hours`** (default 24h), **or**
- **inter-block gaps all shorter than `policy.max_gap_hours`** (default 12h) — i.e. between
  any two adjacent blocks we hold, we're only out of the node for under 12h.

A node that only yields short slivers separated by long gaps is skipped. The lead time before
our first block doesn't count as a gap, and a single unbroken block always passes. Naming
nodes with `--node`, or passing `--include-small-window`, bypasses the filter and reserves the
fragmented nodes too (`allow --commit` also bypasses it, since its job is to re-book a node to
re-test it). Tune the thresholds under `policy` in `config.yaml`.

### Node health

A node is **healthy** only if, over your own SSH key, all of these hold:

1. it accepts a **key-only login** — a node that prompts for a password counts as unhealthy,
2. **`rocm-smi`** is installed and reports at least `policy.min_gpus` GPUs,
3. **`docker`** is installed and **`docker ps`** succeeds.

The probe is read-only (three shell lookups, nothing is changed on the node), runs in
parallel, and retries once so a transient blip doesn't condemn a good node. `status` prints
the failure reason per node. Configure it under `health_probe`; disable with `--no-probe`.

**A failure is classified, because not every failure is the node's fault:**

| Class | Means | Reserved? | Released by `cancel-unhealthy`? |
|---|---|---|---|
| `broken` | We logged in and the box is unusable (0/too few GPUs, no `rocm-smi`, dead Docker) | no | yes |
| `unreachable` | Nothing answered — timeout, closed, refused | no | yes |
| `access` | We could not log in — usually just means *we don't hold the node right now* | **yes** | only with `--include-access`; but if it fails *while we hold it*, `verify` denylists it |

**SSH access is largely gated on holding an active reservation.** On much of this fleet you
can only log into a node while one of your reservations on it is active. Measured directly:
two nodes logged in fine while held (revealing `0 GPUs` and `no rocm-smi`); minutes after we
released them, the same key on the same machines returned `permission denied`. It isn't
absolute — a few nodes probe fine unreserved, and two stayed denied while held — but it is
the dominant factor.

So a probe verdict is only meaningful for nodes you currently hold; elsewhere `access` means
*unknown*, not *bad*. And it must never block reserving: one failed login would stop the node
being booked, which removes the access needed to check it again — locking the node out
permanently on one ambiguous result.

Hence the self-correcting loop: **reserve broadly → probe while the reservation is active →
release only what the probe proves is broken.** To assess one node, reserve it, wait for the
reservation to go active, then run `status`. Tune with `health_probe.block_classes`.

**Verify the nodes you hold.** `cancel-unhealthy` only ever acts on `broken`/`unreachable`,
because for a node you don't hold an `access` failure is just "we don't have it right now".
`cli.py verify` is the complement: it probes **only nodes you hold an active reservation on**,
where an `access` failure IS decisive — a login refused during our own reservation is a real
fault. `verify --commit` denylists such a node (any failure class) and releases every
reservation of ours on it.

**Confirmed-bad nodes are denylisted for good.** A node released by `cancel-unhealthy
--commit` or `verify --commit` is written to a persistent `denylist.yaml` (gitignored); from
then on `run`/`plan` skip it unconditionally — before the probe, and even under `--no-probe`
— so a repeatedly-bad box is never re-reserved. `cancel-unhealthy` records only the `broken`
class (`access` isn't a fault for an unheld node, an `unreachable` timeout may be transient);
`verify` records whatever failed *while we held the node*, `access` included. Inspect with
`cli.py denylist`; re-enable a repaired node with `cli.py allow <node>` (add `--commit` to
also reserve it so `verify` can re-test it live).

### Web control app
```bash
python app.py                 # http://127.0.0.1:5057
```
- **Dry-run plan** / **Reserve for real** — plan table, then create (confirm box) with a live
  per-node created/failed table + run history.
- **Node health card** — runs the SSH probe and lists every **unhealthy node with its reason**,
  plus a full table of all nodes. Mirrors `cli.py status`.
- **Cancel unhealthy card** — find/cancel our reservations on nodes the probe blames on the
  machine; nodes we merely can't log into are listed separately and kept (tick *include
  no-access* to release those too).
- **Cancel card** — find/cancel our reservations on sub-`min_gpus` nodes.
- **Sync-users card** — add the config's default users to existing reservations.
- **My reservations card** — nodes you have reserved (ongoing + upcoming), with each one's
  health, mirrors `status --reserved`.
- Node-eligibility table shows each node's GPU count + include/exclude reason (probe failures
  included).

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
| `policy.min_continuous_hours` | window filter: keep a node if our total hold gives a continuous stretch longer than this (default 24) |
| `policy.max_gap_hours` | window filter: else keep it if every gap between adjacent blocks we hold is under this (default 12) |
| `ssh_user` | username the health probe logs in as (also used in printed `ssh` commands) |
| `health_probe.enabled` | run the SSH health probe at all (`false` = Conductor's scraped data only) |
| `health_probe.user` / `key` | login user (defaults to `ssh_user`) and private key to authenticate with |
| `health_probe.timeout_s` / `workers` | per-node time budget, and how many nodes to probe in parallel |
| `health_probe.block_classes` | which probe failures disqualify a node (default `[broken, unreachable]` — `access` is not blocking) |
| `policy.default_duration_hours` / `default_horizon_days` | fallback limits for pools that set no `reservation_duration_limit` / `furthest_future_reservation` (keeps greedy fill bounded) |

## How eligibility & scheduling work

- **Eligible pool:** `reservation_strategy == "calendar"`, not archived, `block_api_access`
  false, group restrictions met.
- **Eligible node:** in an eligible pool, not archived, with at least `policy.min_gpus`
  GPUs, **not on the persistent `denylist.yaml`** (nodes previously confirmed bad and
  released by `cancel-unhealthy`/`verify`), and **not shown by the live SSH probe to be
  `broken` or `unreachable`** (a node
  we merely can't log into is still reserved — see the health table above).
  (Cosmetic `status` is ignored — the SDK states it must not affect reservations.
  `reservation_only` nodes are included.)
- **Greedy plan (per node):** tile the node's free time — read from the **actual reservation
  list** — with back-to-back reservations of up to `pool.reservation_duration_limit`, each
  **ending by `now + pool.furthest_future`** (a hard server cap on `date_end`), rounded to
  10-minute marks. Pools that cap at 48h yield ~one block ending at the 48h mark; pools with
  no limit chain out to `policy.default_horizon_days`.
- **Window filter (default on):** after planning a node, drop it unless our total hold
  (existing + planned) clears `min_continuous_hours` continuous **or** keeps inter-block gaps
  under `max_gap_hours`. Bypass with `--include-small-window` or an explicit `--node`.
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
