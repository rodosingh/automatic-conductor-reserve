# How to Run — Hyperloom Auto-Reserve

Two audiences: **you** (already set up on this machine) and **a colleague** setting it up
fresh. Dry-run is always the default; nothing is created until you explicitly commit.

---

## A. Quick run (this machine — already configured)

```bash
cd ~/hyperloom-reserve

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
```

### Flags for `run` and `cancel-small-gpu`

| Flag | Effect |
|---|---|
| `--commit` | actually write (create / cancel). Without it, everything is a dry-run. |
| `--pool <id>` | restrict the run to one pool id (repeatable). Default = all pools in `config.yaml`. |
| `--yes` | skip the interactive "type yes" prompt (for scripts / non-interactive shells). |
| `-v` | live progress lines. |

**Per-pool commits** — this is how the pools were actually booked (pool C greedy first, then A/B),
because the two have different limits and it's safer to verify one before the next:

```bash
# pool C only (MI350X-AIG-SW-Models — its 4 named nodes, greedy to the ~14-day horizon)
python cli.py run --commit --yes --pool 46c35d80-1e4a-4970-aaa1-d7269f3e67f7

# pools A + B (48h blocks — see the cap note below)
python cli.py run --commit --yes \
  --pool a58e25ad-9aaf-4f6c-87ad-138d08f56510 \
  --pool d708f0af-1b34-4f07-8b73-ebbc1f584fd6
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
- **Cancel card** → **Find (no delete)** lists our reservations on excluded (sub-`min_gpus`)
  nodes; tick the box and **Cancel** to delete them (mirrors `cancel-small-gpu`).
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
- **How far ahead / how long — set per pool by the server:**
  - Pools **A & B** are hard-capped at **48h**: a reservation's *end* must be within `now+48h`,
    so each node gets **one block ending at the 48h mark** — a full 48h if free now, a shorter
    tail if it's busy until near the horizon. You **cannot** book a month there; the server
    rejects it (`exceeds furthest future reservation limit`).
  - Pool **C** (MI350X-AIG-SW-Models) sets no limit, so the tool chains **24h blocks out to
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
#   e.g. scp -r you@host:~/hyperloom-reserve ~/hyperloom-reserve   (or a git clone)
cd hyperloom-reserve

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

# 4) Verify
python cli.py whoami        # should print THEIR email + teams
```

### Adjust config for the colleague
Edit `config.yaml`:
- **`reservation.team_name`** must be one of **their** teams (see `whoami` output).
  `AIG-Training` is yours — theirs may differ.
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
| `ModuleNotFoundError: conductor_sdk` | SDK not installed in the Python you’re running — use `~/miniconda3/bin/python`, or run from the project dir / set `PYTHONPATH=~/hyperloom-reserve`. |
