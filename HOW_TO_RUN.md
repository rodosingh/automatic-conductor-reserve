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
python cli.py cancel-unhealthy --include-access --commit   # ALSO release nodes we can't log into
```

**Reserve a single node from `status`:** pick a free & healthy node and run
`python cli.py run --commit --node <name>` (the exact line is printed under "Reserve one").
`--node` matches by short name or full hostname and works with `plan` (dry-run) too.

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
| `access` | We **could not log in** (key rejected / password wanted). | **yes** | only with `--include-access` |

`access` is deliberately not disqualifying. It is a fact about *our credentials*, not about
the machine — and blocking on it is a trap: the tool would stop reserving the node, so we
would never regain the access needed to check it. Observed on this fleet: nodes have been
reachable **without** holding a reservation, and denied **while** holding one, so login
success is not a proxy for health and must not gate booking.

That gives a self-correcting loop: **reserve broadly → probe while the reservation is
active → release what the probe proves is broken** (`cancel-unhealthy`). Change which
classes disqualify a node with `health_probe.block_classes` in `config.yaml`.

For each free & healthy node `status` also prints an `ssh <user>@<host> 'watch -n 0.2 rocm-smi'`
line (user from `--ssh-user` or `ssh_user`) so you can eyeball the GPUs live yourself.

### Flags for `run`, `cancel-unhealthy` and `cancel-small-gpu`

| Flag | Effect |
|---|---|
| `--commit` | actually write (create / cancel). Without it, everything is a dry-run. |
| `--pool <id>` | restrict the run to one pool id (repeatable). Default = all pools in `config.yaml`. |
| `--node <name>` | restrict to specific node name(s)/hostname(s) (repeatable) — reserve a single node picked from `status`. |
| `--no-probe` | skip the SSH health probe. Faster, but reserves without verifying login / rocm-smi / docker. |
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
- **Cancel card** → **Find (no delete)** lists our reservations on excluded (sub-`min_gpus`)
  nodes; tick the box and **Cancel** to delete them (mirrors `cancel-small-gpu`).
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
