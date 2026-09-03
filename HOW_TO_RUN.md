# How to Run — Hyperloom Auto-Reserve

Two audiences: **you** (already set up on this machine) and **a colleague** setting it up
fresh. Dry-run is always the default; nothing is created until you explicitly commit.

---

## A. Quick run (this machine — already configured)

```bash
cd ~/automatic-conductor-reserve

# 1) verify auth works and see your teams
python cli.py whoami

# 2) DRY-RUN — see exactly what would be reserved (no writes)
python cli.py plan          # add -v for live progress

# 3) COMMIT — actually create the reservations (asks you to type 'yes')
python cli.py run --commit
python cli.py run --commit --include-small-window   # ALSO reserve fragmented nodes (see below)

# past runs
python cli.py runs

# 4) CANCEL our current+future reservations on excluded (sub-min_gpus) nodes
python cli.py cancel-small-gpu            # dry-run: lists what would be cancelled
python cli.py cancel-small-gpu --commit   # actually cancel (asks you to type 'yes')

# 5) SYNC USERS — add config's default users to our existing (ongoing+upcoming) reservations
python cli.py sync-users                  # dry-run: lists reservations that would be updated
python cli.py sync-users --commit         # add the default users (mode 'add' — never removes)

# 6) STATUS — which nodes are free & healthy, and WHY the rest are not (read-only)
python cli.py status                      # table: gpu, docker, reserved, health/reason + an "UNHEALTHY" summary
python cli.py status --free               # only free & healthy nodes + copy-paste rocm-smi cmds
python cli.py status --unhealthy          # only unhealthy nodes, with the reason each one failed
python cli.py status --reserved           # only nodes YOU have reserved, with health + your window + title
python cli.py status --fast               # skip the reservation check (health only, quicker)
python cli.py status --no-probe           # skip the SSH probe (Conductor's scraped data only, instant)
python cli.py status --pool <id>          # one pool

# 7) CANCEL our reservations on nodes the probe proves are broken/unreachable
python cli.py cancel-unhealthy            # dry-run: unhealthy nodes + reasons + what would be cancelled
python cli.py cancel-unhealthy --commit   # actually cancel (asks you to type 'yes')
# (a can't-log-in / access failure is NOT cancelled here — judge those with step 8 while held)

# 7b) Release nodes whose hold has become too FRAGMENTED (the cancel-side window filter)
python cli.py cancel-small-window          # dry-run: fragmented nodes + what would be cancelled
python cli.py cancel-small-window --commit # actually cancel (asks you to type 'yes'); NOT denylisted

# 8) JUDGE the nodes you currently HOLD — the only place an SSH failure is a real verdict
python cli.py status --active_n_healthy           # dry-run: held nodes still unhealthy while we hold them
python cli.py status --active_n_healthy --commit  # denylist + release those (still-broken OR still-no-login)

# 9) DENYLIST — nodes confirmed bad are skipped by run/plan from then on
python cli.py denylist                    # show the denylisted nodes being skipped
python cli.py allow <node>                # re-enable a node (just remove it from the denylist)
python cli.py allow <node> --commit       # re-enable AND reserve it, so step 8 can re-test it live
```

**Reserve a single node from `status`:** pick a free & healthy node and run
`python cli.py run --commit --node <name>` (the exact line is printed under "Reserve one").
`--node` matches by short name or full hostname and works with `plan` (dry-run) too.

**Only worthwhile nodes are reserved (window filter, on by default).** A large team piles up
a lot of small, scattered reservations, so `plan`/`run` skip fragmented nodes: a node is kept
only if our **total hold** (existing reservations + the blocks this run would add) gives one
**continuous stretch over `policy.min_continuous_hours`** (default 12h), *or* leaves
**inter-block gaps all under `policy.max_gap_hours`** (default 12h) — between any two adjacent
blocks we hold, we're out of the node for under 12h. The wait before our first block isn't a
gap, and a single unbroken block always passes. Nodes that only offer short slivers split by
long gaps are dropped (the skip reason is printed). Pass `--include-small-window` (or the web
**include small-window nodes** checkbox) to reserve those too; an explicit `--node` and
`allow --commit` bypass the filter as well. Tune the two thresholds under `policy` in
`config.yaml`.

**`run --commit` also tears down what's still fragmented afterward.** Once it has booked, it
re-runs the same window test over each node's final hold (existing + newly created) and cancels
our reservations on any node still fragmented — so freshly-de-fragmented nodes are spared, and
leftover slivers are cleaned up in the same step. `plan` / dry-run `run` only *preview* this.
Opt out with `--no-cancel-fragmented` (or the web **don't cancel still-fragmented nodes after**
checkbox); a targeted `run --node N` never sweeps other nodes. It's the same logic as the
standalone `cancel-small-window` command, just folded into the reserve step.

### What "healthy" means

`status`, `plan` and `run` all SSH into each candidate node and check three things. A node is
healthy only if **all** hold:

1. **key-only login succeeds** — logging in with `ssh_user` + `health_probe.key` without a
   password. A node that prompts for a password, drops the session, or times out is unhealthy.
2. **`rocm-smi` is installed and reports ≥ `policy.min_gpus` GPUs** — so a box where the GPUs
   have fallen off the bus (`0 GPUs`) or ROCm was never installed is unhealthy.
3. **`docker` is installed and `docker ps` succeeds** — daemon down or no access is unhealthy.

The reason a node failed is printed next to it in `status` (and in the Flask **Node health**
card). The probe is **read-only** — three shell lookups, nothing on the node is changed —
runs nodes in parallel (`health_probe.workers`) and retries once, so a transient blip
doesn't condemn a good node. Conductor's own scraped data (reachability, driver, `disabled`,
24h utilization) is still read and shown, but the live probe is what decides.

#### Not every failure is the node's fault

A failure is classified, and only some classes disqualify a node:

| Class | Means | Reserved? | `cancel-unhealthy` releases it? |
|---|---|---|---|
| `broken` | We **logged in** and the box is unusable — 0/too few GPUs, no `rocm-smi`, dead Docker. | no | yes |
| `unreachable` | Nothing answered — timeout, connection closed/refused, no route. | no | yes |
| `access` | We **could not log in** (key rejected / password wanted). Usually just means *we don't hold the node right now*. | **yes** | never — but if it fails **while we hold it**, `status --active_n_healthy` denylists it (see below) |

#### SSH access is largely gated on holding an active reservation

This is the single most important thing to understand about the probe. On much of this
fleet you can only log into a node **while one of your reservations on it is active**.

Measured directly: two nodes (one from each of two pools) both logged in fine
while we held them (revealing `0 GPUs` and `no rocm-smi` respectively). We released those
reservations, re-probed minutes later, and both returned `permission denied`. Same key, same
machine, same network — the only thing that changed was that we no longer held them.

It is not an absolute rule: a few pool-C nodes probe fine with no reservation, and two nodes
stayed denied *while* held. So holding a reservation is neither strictly necessary nor
sufficient — but it is the dominant factor.

Two consequences the tool is built around:

- **A probe verdict is only meaningful for nodes you currently hold.** For everything else,
  `access` means *unknown*, not *bad*. That is why it is never reported as a fault.
- **`access` must not block reserving.** If it did, one failed login would stop the node
  being reserved, which would remove the access needed to ever check it again — the node
  would be locked out permanently on the strength of a single ambiguous result.

So the workflow is a self-correcting loop: **reserve broadly → probe while the reservation
is active → release only what the probe proves is broken** (`cancel-unhealthy`). To assess
one specific node, reserve it first (`run --commit --node <name>`), wait for the reservation
to become active, then run `status`. Change which classes disqualify a node with
`health_probe.block_classes` in `config.yaml`.

#### `status --active_n_healthy` — judge the nodes you actually hold

`cancel-unhealthy` probes every node and only ever acts on `broken`/`unreachable`, because
for a node you *don't* hold, an `access` failure is just "we don't have it right now" — you
can't SSH in precisely *because* the reservation isn't active, so it's not a fault.
`status --active_n_healthy` is the complement: it looks **only at nodes you hold an active
reservation on right now**, and there an `access` failure is decisive — the reservation-gating
excuse is gone, so a node that *still* won't let us log in (or logs in and is unusable) is
genuinely bad.

```bash
python cli.py status --active_n_healthy          # dry-run: which held nodes are still unhealthy, and why
python cli.py status --active_n_healthy --commit # denylist those nodes AND release all our reservations on them
```

A held node is judged bad for **any** failure class — `broken` (logged in, unusable),
`access` (still no login while we hold it), or `unreachable` (dead while we hold it) — and on
`--commit` it is denylisted and every reservation of ours on it (active + upcoming) is
cancelled in one go. Nodes you hold that probe healthy are left exactly as they are.

#### Confirmed-bad nodes are denylisted for good

Both `cancel-unhealthy --commit` and `status --active_n_healthy --commit` write the nodes they
release to a persistent **denylist** (`denylist.yaml`, gitignored — it holds hostnames). From then on
`run` and `plan` skip those nodes **unconditionally** — before the probe even runs — so a
repeatedly-bad box is never reserved again, even under `--no-probe`.

What gets recorded is deliberately narrow, so we never permanently ban a node on an ambiguous
result:

- **`cancel-unhealthy`** records only the **`broken`** class (we logged in and the machine was
  unusable). It never records `access` — for an unheld node that's just "no login from here".
- **`status --active_n_healthy`** records whatever failed **while we held the node** — including
  `access`, because a login refused *during our own active reservation* is a real fault, not an access quirk.

`unreachable` timeouts from `cancel-unhealthy` stay re-probed every run rather than banned
(they're often transient). Manage the list with:

```bash
python cli.py denylist              # what's currently skipped, with the reason and date
python cli.py allow <node>          # remove a node from the denylist (e.g. after it's repaired)
python cli.py allow <node> --commit # remove it AND reserve it, so active_n_healthy can re-test it live
```

`allow --commit` reserves the node with the probe skipped on purpose: the whole point is to
regain the access needed to test it, so it must be booked regardless of what a probe says
while we don't yet hold it. The reservation goes active after `policy.start_lead_minutes`
(~20 min); once it's active, `status --active_n_healthy --commit` re-judges it and re-denylists
it if it's still bad.

For each free & healthy node `status` also prints an `ssh <user>@<host> 'watch -n 0.2 rocm-smi'`
line (user from `--ssh-user` or `ssh_user`) so you can eyeball the GPUs live yourself.

### Flags for `run`, `cancel-unhealthy` and `cancel-small-gpu`

| Flag | Effect |
|---|---|
| `--commit` | actually write (create / cancel). Without it, everything is a dry-run. |
| `--pool <id>` | restrict the run to one pool id (repeatable). Default = all pools in `config.yaml`. |
| `--node <name>` | restrict to specific node name(s)/hostname(s) (repeatable) — reserve a single node picked from `status`. |
| `--no-probe` | skip the SSH health probe. Faster, but reserves without verifying login / rocm-smi / docker. |
| `--include-small-window` | (`plan`/`run`) also reserve fragmented nodes the window filter would skip. |
| `--yes` | skip the interactive "type yes" prompt (for scripts / non-interactive shells). |
| `-v` | live progress lines (includes per-node probe results). |

**Per-pool commits** — this is how the pools were actually booked (pool C greedy first, then A/B),
because the two have different limits and it's safer to verify one before the next:

```bash
# pool C only (Pool-C-Models — its 4 named nodes, greedy to the ~14-day horizon)
python cli.py run --commit --yes --pool <POOL_ID_3>

# pools A + B (48h blocks — see the cap note below)
python cli.py run --commit --yes \
  --pool <POOL_ID_1> \
  --pool <POOL_ID_2>
```

> A full `python cli.py run --commit` (no `--pool`) commits **all** configured pools in one go
> and produces the same result — the scheduler already applies each pool's own limits.

Or the **web app**:
```bash
python app.py               # open http://127.0.0.1:5057
```
- **Dry-run plan** button → full plan table, no writes.
- Tick **“I understand — reserve for real”** → **Reserve for real** → creates them, shows a
  per-node created/failed table. History + JSONL logs under `runs/`.
- **Node health card** → **Check node health** runs the SSH probe and shows counts plus a table
  of every **unhealthy node with its reason** (and a fold-out table of all nodes). Mirrors `status`.
- **Cancel unhealthy card** → **Find (no delete)** lists the unhealthy nodes with reasons and our
  reservations on them; tick the box and **Cancel** to release them (mirrors `cancel-unhealthy`).
  Only `broken`/`unreachable` are in scope — no-access nodes are listed but kept.
- **Cancel card** → **Find (no delete)** lists our reservations on excluded (sub-`min_gpus`)
  nodes; tick the box and **Cancel** to delete them (mirrors `cancel-small-gpu`).
- **Cancel small-window card** → **Find (no delete)** lists our reservations on fragmented nodes;
  tick the box and **Cancel** to release them (mirrors `cancel-small-window`; not denylisted).
- **Active & healthy card** → **Check held (no writes)** probes only the nodes you hold active
  now; tick the box and **Denylist & release** to act on the bad ones (mirrors `status --active_n_healthy`).
- **Sync-users card** → **Find (no update)** lists our ongoing+upcoming reservations; tick
  the box and **Add default users** to add the config's default users (mirrors `sync-users`).
- **My reservations card** → **Show my reservations** lists the nodes YOU have reserved
  (ongoing + upcoming) with your window, block count, active/upcoming, and each node's health
  (mirrors `status --reserved`).
- The node-eligibility table shows each node's **GPU count** and why it was included/excluded.

> The web app commits **all** configured pools together (no per-pool button). For per-pool
> commits, use the CLI `--pool` flag shown above.

> `python` here = the base conda Python (`~/miniconda3/bin/python`) where the SDK is
> installed. If `python` isn’t that by default, use `~/miniconda3/bin/python cli.py …`.

### What gets reserved (behavior notes)

- **GPU filter:** nodes with fewer than `policy.min_gpus` (default **2**) are skipped — this
  drops the single-GPU dev boxes. There are no 2–4 GPU nodes in these pools, so in practice
  it just excludes the 1-GPU machines and keeps the 8-GPU ones. `cancel-small-gpu` cancels
  *our* reservations on those excluded nodes.
- **Health filter:** every remaining node is SSH-probed and only the healthy ones are
  reserved (see *What "healthy" means* above). `cancel-unhealthy` releases reservations we
  already hold on nodes that now fail. It only touches reservations carrying our configured
  title, so a colleague's reservation on the same node is never cancelled.
- **How far ahead / how long — set per pool by the server:**
  - Pools **A & B** are hard-capped at **48h**: a reservation's *end* must be within `now+48h`,
    so each node gets **one block ending at the 48h mark** — a full 48h if free now, a shorter
    tail if it's busy until near the horizon. You **cannot** book a month there; the server
    rejects it (`exceeds furthest future reservation limit`).
  - Pool **C** (Pool-C-Models) sets no limit, so the tool chains **24h blocks out to
    ~14 days** (`policy.default_horizon_days` / `default_duration_hours`).
- **Never double-books:** existing reservations (yours or others') are read live and skipped;
  a node already booked by other teams for the whole window is simply left alone (re-run later
  as it frees up).

---

## B. Giving it to a colleague

### Does a colleague need an API key? **Yes — their own.**

The API key **is** the person’s identity. Every reservation is created **as the key’s
owner** (that person is the auto-included creator), and what they’re allowed to reserve
depends on **their** team/pool access. So:

- **Do NOT share your API key.** A colleague using your key would be acting as *you*
  (reservations under your name, your access, your notifications).
- **Each colleague generates their own key** and uses their own email.

### How a colleague gets an API key
1. Log in to <https://conductor.amd.com> (Azure SSO).
2. Open their **profile / account menu → API Key** (the “Manage API Key” dialog).
3. **Generate / Create** → copy the key **immediately** — it’s shown **once**. (Regenerate
   makes a new one and invalidates the old.)

### Colleague setup (one time)

```bash
# 1) Get the code (copy the folder, or clone it if you put it in git — WITHOUT .env)
cd ~
#   e.g. scp -r you@host:~/automatic-conductor-reserve ~/automatic-conductor-reserve   (or a git clone)
cd automatic-conductor-reserve

# 2) Install the SDK + web deps into their Python (base conda recommended)
pip install conductor_sdk flask pyyaml \
  --index-url https://mkmartifactory.amd.com/artifactory/api/pypi/hw-orc3pypi-prod-local/simple \
  --extra-index-url https://pypi.org/simple \
  --trusted-host mkmartifactory.amd.com

# 3) Create THEIR OWN .env (mode 600 — never commit or share this file)
cat > .env <<'EOF'
AMD_EMAIL=firstname.lastname@amd.com
ATS_SECRET=<their-own-conductor-api-key>
VERIFY_CERTS=false
EOF
chmod 600 .env

# 3b) Create THEIR config from the template
cp config.example.yaml config.yaml   # then edit team_name / users / pool ids

# 4) Verify
python cli.py whoami        # should print THEIR email + teams
```

### Adjust config for the colleague
Edit `config.yaml`:
- **`reservation.team_name`** must be one of **their** teams (see `whoami` output).
  `<your-team>` is yours — theirs may differ.
- `reservation.users`, `title`, `project`, `milestone` as desired (they’re auto-included,
  so they don’t need to add themselves).
- `pools[]` — they can only successfully reserve pools their teams can access; the server
  will report “lack of access” in the results for pools they can’t.

Then they run exactly as in **Section A** (`plan` first, then `run --commit`).

> **Reproducibility note:** everything is portable except the two secrets in `.env`.
> Never put `.env` in git or share it — it’s already in `.gitignore`. Ship the code; each
> person supplies their own `AMD_EMAIL` + `ATS_SECRET`.

---

## C. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Missing credentials…` | `.env` missing or `AMD_EMAIL`/`ATS_SECRET` unset. |
| `whoami` → 401 / “Authorization header is invalid” | Wrong email for the key, or key was regenerated. Get a fresh key; email must be the key owner’s. |
| `Team 'X' not found…` | `reservation.team_name` isn’t one of your teams — pick from `whoami`. |
| `could not resolve users: …` | Use **email or NTID** (a plain “First Last” name won’t resolve — DB stores “Last, First”). |
| Pool shows **INELIGIBLE** | Not calendar-strategy, archived, `block_api_access`, or group restrictions unmet — expected; it’s skipped. |
| A node fails on commit with “already reserved (overlap)” | The window is already booked (by you or others). Not a bug — expected on busy shared nodes. |
| A node fails with “exceeds furthest future reservation limit” | You tried to book past the pool's horizon (A/B = 48h). The scheduler caps to it automatically; seeing this means a stale plan — re-run. |
| A node fails on commit with “access” | Your teams don’t have reservation access to that pool. |
| Lots of nodes report `ssh: permission denied` | Expected on a mixed fleet: your key isn't authorized on those hosts. They are classed `access`, still reserved, and never auto-released. Check one by hand with `ssh -i <key> <user>@<host>`. |
| A node you can reach shows `ssh: connection timed out` | Not on the internal network/VPN, or the node needs a jump host the probe doesn't use. Raise `health_probe.timeout_s`, or exclude it. |
| `rocm-smi shows 0 GPUs` | Real fault — the GPUs have fallen off the bus on that node. Correctly treated as unhealthy. |
| `docker ps failed` | Docker daemon is down, or your user isn't in the `docker` group on that node. |
| `ModuleNotFoundError: conductor_sdk` | SDK not installed in the Python you’re running — use `~/miniconda3/bin/python`, or run from the project dir / set `PYTHONPATH=~/automatic-conductor-reserve`. |
