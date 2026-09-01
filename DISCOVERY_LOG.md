# Conductor Auto-Reservation — Full Discovery & Build Log

A detailed, chronological account of how this tool was built: what we probed, every
error/dead-end we hit, how we resolved each, and what we learned. Written so a future
maintainer (or a fresh session) can reconstruct the reasoning without re-doing the work.

---

## 0. The goal

Automate reservation of AMD Conductor **standalone conductor nodes** across two shared
pools, greedily, until every eligible node's reservation window is exhausted:

- Pool A: `<POOL_ID_1>`
- Pool B: `<POOL_ID_2>`

For each eligible node, create a reservation with fixed defaults (title, project,
description, milestone, team, users, "disable batch jobs" checkbox), computing
start/end from existing bookings and the pool's max window; notify on success. Then wrap
it in a system that can be triggered manually.

---

## 0.1 What the user provided, and the decisions made

**Inputs the user supplied during the session:**
- The two pool URLs (Pool A / Pool B above).
- The fixed reservation defaults: Title `Hyperloom-E2E-expt`, Project `Hyperloom`,
  Description "This is a test reservation for the Hyperloom project.", Milestone
  `30-09-2026`, default users **<teammate1>** and **<teammate2>**, checkboxes (disable batch jobs, create-another, preserve-fields), submit.
- **Screenshot 1 — the reservation form.** Revealed fields not in the text spec: a required
  **"Team for Allocation"** dropdown (shown = `<your-team>`), a **"Next Available
  Reservation"** tab (the system computes free slots itself), an "Existing Reservations"
  table (Active/Upcoming), and the per-pool hint text *"reservations up to 2 days in the
  future"* / *"duration of up to 48 hrs"* — i.e. constraints are data, per pool.
- **The API key** `<redacted>` (stored only in `.env`, mode 600; never logged/committed).
- **Screenshot 2 — DevTools cookies** for conductor.amd.com, used to identify the auth
  cookie: `session` (HttpOnly, Secure, domain `.conductor.amd.com`). (In the end we didn't
  need the cookie — the API key path is durable — but it confirmed the cookie name.)
- **Two candidate emails**: `you@amd.com` **or** `your-ntid@amd.com`. We tested
  both live (see §4): the first authenticates (200); `your-ntid@amd.com` as an email → 401.
  (Note: the NTID works for **user lookup**, just not as the auth email.)
- Later: two follow-up documentation requests, and the "how do colleagues run it / API key?"
  question (→ `HOW_TO_RUN.md`).

**Decisions the user made (via explicit multiple-choice prompts):**
1. **Auth** → *API key* (durable, headless) — chosen over browser cookie / headless SSO.
2. **Booking policy** → **Fully greedy — fill the horizon** (chain back-to-back max-length
   reservations on every eligible node until each node's future window is exhausted).
3. **Notifications** → **web-UI run summary + local JSONL file** (no email/Teams webhook).
4. **Form values** → **ask once, then reuse** (seed a config file with the stated defaults;
   fill exact Team/Project before the first real run).

**Caution raised (and accepted):** greedily booking every node for its max window is exactly
what the per-pool caps ("2 days ahead", "48 hrs") exist to bound. Flagged the shared-pool
blast radius and recommended a dry-run-first default; the user chose full greedy. We
therefore made **dry-run the default everywhere** and put real writes behind an explicit
commit, plus optional `policy.max_nodes` / `max_reservations_per_node` safety rails. It also
turned out the 48h/48h caps make "fill the horizon" ≈ **one 48h reservation per free node** —
a naturally small blast radius.

---

## 1. Environment probe — what's on this machine

- Python 3.12 (base conda at `~/miniconda3`), Node 20, curl. **No** Playwright/Selenium.
- `conductor.amd.com` reachable (HTTP 200, ~1s).
- **Learning:** browser automation wasn't installed, so we first checked whether a REST
  API existed before committing to a headless-browser approach.

## 2. Is it a browser-only UI, or is there an API?

- The site is an **Angular SPA** served as a static shell (`main-*.js`, `chunk-*.js`).
- Downloaded all JS bundles (~5.3 MB) to `/tmp/condjs/` and grepped them.
- **Found the API base:** `api_ip:"https://conductor.amd.com/api/v1"` and
  `api_ip_v2:".../api/v2"`. So there **is** a clean REST API — no browser needed. ✅

### Error encountered: `ugrep` complexity limits
- The system `grep` is actually **ugrep**, which errors on wide bounded regex
  quantifiers: `error: ... exceeds complexity limits`.
- **Fix:** switched all bundle analysis to Python (`re` + fixed-string `in` checks).
  Simple `grep -F`/`grep -c` still fine.
- **Learning:** don't rely on complex regexes here; use Python for text mining.

## 3. Reverse-engineering the API surface (parallel workflow)

- Ran a multi-agent **workflow** (6 extractors → adversarial verifiers → synthesis) over
  the bundles to map endpoints from **verbatim string literals only** (no guessing).
- Confirmed, e.g.:
  - Create: `POST /api/v2/calendar/reservation` (custom header `conductor-client-source: WEB_APP`).
  - Availability: `GET /api/v2/calendar/reservation/filtered_openings`.
  - Enumeration via AMDQL (`POST /api/v1/amdql`) with a `Table.column__OP` filter grammar.
  - Dates are ISO-8601 UTC; reservation *status* (Active/Upcoming) is derived client-side
    from start/end, not a server field.
- **Discovered policy constants** in the bundle env: `max_reservation_duration_days:1`,
  `max_reservation_distance_future_days:14` — but these are **defined-but-unused** defaults;
  the real limits are **per-pool** and server-enforced. We did **not** hardcode them.

## 4. The authentication saga (the hard part)

This took the most iterations. Chronology:

1. Unauth API calls returned a helpful oracle:
   - `401 {"message":"value is missing from Cookie header"}` = endpoint **exists**.
   - `404` = absent. `405` = exists, wrong method. → We could verify paths without creds.
2. Found `/api/v1/api_key` (GET/POST/PUT) → self-service API keys exist. The user provided
   an API key: `<redacted>` (stored only in `.env`, mode 600).
3. **Dead-end:** tried the key in ~40 placements — `Authorization: Bearer <k>`,
   `x-api-key`, `api-key`, `X-Auth-Token`, and as a cookie under `session`, `token`,
   `access_token`, `sessionid`, `conductor_session`, … **All** returned the identical
   `401 "value is missing from Cookie header"`.
4. **Dead-end:** even replaying a *real* fresh `session` cookie from
   `GET /api/v1/auth/azure_oidc` gave the same error → that `session` cookie is only an
   OAuth **state** cookie; the real auth cookie is **HttpOnly and server-set** (name not
   in the JS). Login is **Azure AD OIDC + PKCE** (interactive Microsoft SSO).
5. **Key realization from a verify agent:** across all 5.3 MB the frontend has **zero**
   `Authorization`/`Bearer`/`x-api-key` request headers — the SPA authenticates purely by
   the cookie session and **never sends the API key**. How the key is presented is defined
   **server-side / by a separate client**, invisible in the bundle.
6. **Breakthrough via the docs:** `https://conductor.amd.com/docs/reservations/` is a real
   server-rendered docs site (not the SPA shell). Its nav revealed an official
   **Conductor Python SDK**, a **CLI (`conduct`)**, and an **MCP server**.
7. The SDK **Getting Started / Configuration** pages gave the auth scheme:
   env vars `AMD_EMAIL` + `ATS_SECRET` (= the API key), base URL `ATS_URL`
   (default `https://conductor.amd.com`).
8. Installed the SDK and read its source. The decisive line in
   `at_scale_python_api/endpoints/endpoint.py`:
   ```python
   headers["Authorization"] = (get_email() + ":" + get_secret()) if get_email() else get_secret()
   ```
   → **Auth is `Authorization: <email>:<api_key>`** (email and key joined by a colon).
   That's the one format never tried. The server also accepts `Cookie: session=…`.
9. **Confirmed live:** `Authorization: test@amd.com:<key>` changed the error to
   `401 "Provided Authorization header is invalid"` (mechanism correct, wrong owner). With
   the real owner email `you@amd.com` → **HTTP 200**, full identity returned. ✅

**Learning:** black-box header/cookie guessing is a poor substitute for finding the
official client. The docs site + installed SDK source resolved in minutes what dozens of
probes couldn't. The API key is real and durable (no browser, no cookie expiry) — it just
needs the `email:key` framing the SDK does for us.

## 5. Installing the SDK

### Error: pip SSL + dependency resolution
- `pip install conductor_sdk` against the internal Artifactory failed with
  `SSL: CERTIFICATE_VERIFY_FAILED` (corporate MITM cert) **and** `ResolutionImpossible`.
- Root cause: the internal index `hw-orc3pypi-prod-local` is a **local-only** repo — it
  does **not** proxy PyPI, so third-party deps (`requests`, …) weren't found there.
- **Fix (into base conda per request):**
  ```
  pip install conductor_sdk \
    --index-url https://mkmartifactory.amd.com/artifactory/api/pypi/hw-orc3pypi-prod-local/simple \
    --extra-index-url https://pypi.org/simple \
    --trusted-host mkmartifactory.amd.com
  ```
  → installed `conductor_sdk 0.5.0`, `at_scale_python_api 6.0.0`, `amd-ats-models`, etc.
- **Learning:** internal index for AMD packages, PyPI for the rest, trust the internal host.

## 6. Validating the whole flow live (read-only)

Every call below was run against production with **zero writes**:

- **Identity:** `MyResources().user` → email/id/teams. Teams include **<your-team>**
  (the "Team for Allocation" default) and **Hyperloom** (the project).
- **Pools:** `PoolQuerier().find_pool_by_id()` (returns a **list** — normalize to `[0]`).
  Pool A = *Pool-A-Shared* (7 systems), Pool B = *Pool-B-Shared*
  (46 systems). Both `reservation_strategy=calendar`, `block_api_access=False`,
  `group_restrictions_met=True`.
  - **Constraints are in SECONDS:** `reservation_duration_limit=172800` (48h),
    `furthest_future_reservation=172800` (48h). So greedy fill is bounded to a ~48h
    horizon per node — much smaller blast radius than "weeks".
- **Systems:** `SystemQuerier().find_system_advanced(pool=[name])`. The display name is at
  **`system_datas.name`** (top-level `name` came back `None`).
- **Eligibility:** `EntityStatus`/`status` is documented as **purely cosmetic — must not
  gate reservations.** Real eligibility = strategy==calendar ∧ not archived ∧ not
  `block_api_access` ∧ `group_restrictions_met`. `reservation_only` nodes are reservable.
- **Users:** `UserQuerier().lookup_advanced(user=[…])`.
  - **Error/learning:** partial surname (a last name alone) → **0 matches**. Names are stored
    **"Last, First"**; `user=["<teammate1>"]` and exact lowercase **email** both
    resolve. NTID also resolves. Resolved all three:
    - <you> `e8d59de6-…` (creator, auto-included)
    - <teammate1> `d6db6c19-…` (`teammate1@amd.com`)
    - <teammate2> `e3d783a6-…` (`teammate2@amd.com`)
- **Create schema (`ats_models…reservations/actions/create.CreateReservation`):**
  `title(≥3)`, `description?`, `project(≥3)`, `milestone(datetime)`, `target_id(UUID)`,
  `team_id(UUID)`, `user_ids[UUID]?(≤50)`, `date_start?(def now)`, `date_end`,
  `batch_opt_out?(=disable-batch-jobs checkbox)`. Rules: `date_end>date_start`,
  `date_start≥now−6h`, **`milestone≥date_end`**. Times round to **10-minute** marks.
  `create()` takes a **list** and returns `{reservations:[…created…], notes:{failures}}`.
- **Availability:** `ReservationController().get_filtered_openings(...)` returns free slots
  with entity name + start/end. And **`check_conflicts(...)`** (read-only) returns existing
  reservations overlapping a window — we use this for server-authoritative busy detection.

### Error: AMDQL leading-wildcard search hung
- A `Users.full_name__LIKE %term%` query **timed out (120s)**.
- **Fix/learning:** avoid leading-wildcard LIKE on AMDQL (slow full scan). Resolve users
  via the SDK `UserQuerier` with exact email / "Last, First" / NTID instead.

## 7. What we built

A deterministic Python package + two front-ends. **Dry-run is the default everywhere;**
writes happen only on an explicit commit (CLI `--commit` with a typed "yes", or the web
"Reserve for real" checkbox).

```
automatic-conductor-reserve/
  .env                     # AMD_EMAIL, ATS_SECRET (mode 600, gitignored) — never logged/committed
  config.yaml              # reservation defaults + pools + greedy policy knobs
  conductor_reserve/
    creds.py               # load .env; require AMD_EMAIL + ATS_SECRET
    config.py              # load/validate config.yaml
    conductor.py           # thin SDK wrapper (identity, pools, systems, users, conflicts, create)
    scheduler.py           # greedy, conflict-aware plan builder (10-min rounding, pool limits)
    engine.py              # enumerate -> plan -> (commit) -> per-item outcome; writes run log
    notify.py              # JSONL run log + console/web summaries
    models.py              # dataclasses: PoolInfo, NodeInfo, PlanItem, RunResult
  cli.py                   # plan / run --commit / whoami / runs
  app.py + templates/      # Flask control app (manual trigger, live plan/commit tables, history)
  runs/                    # per-run JSONL logs
```

### First live dry-run result
Enumerated all **53 nodes**, checked conflicts on each, planned **4** reservations (~47.5h
each). Spot-checked the non-planned nodes: they are genuinely booked solid (existing 48h
reservations cover the whole window), confirming the scheduler skips busy nodes correctly.

## 8. Key learnings, distilled

1. **Find the official client before reverse-engineering auth.** The docs site + SDK source
   answered in minutes what black-box probing couldn't.
2. **The 401 message is an oracle** ("missing from Cookie header" = exists; "invalid" =
   right mechanism, wrong creds) — useful for safe, unauthenticated discovery.
3. **Auth = `Authorization: <email>:<api_key>`**, done for us by the SDK via
   `AMD_EMAIL`/`ATS_SECRET`. Durable; no browser, no cookie expiry.
4. **Pool limits are per-pool, in seconds, server-enforced.** Read them live; never
   hardcode. Both current pools = 48h/48h, so "fill the horizon" ≈ one 48h reservation/node.
5. **Status is cosmetic**; eligibility is strategy/archived/block_api/group_restrictions.
6. **Users resolve by email / NTID / "Last, First" / UUID** — not by partial name.
7. **Let the server be the source of truth**: `check_conflicts` for busy time, and
   `create()`'s `notes` for per-node failure reasons (conflict / access) — no fragile
   client-side free/busy math, no double-booking.
8. **Deterministic > agentic** for this task (see README "Do we need an LLM agent?").

## 9. Build details, bugs fixed, and tests

- **Reverse-engineering workflow stats:** 13 agents, ~1.05 M tokens, ~16 min. It recovered
  110 distinct API path literals and verified the reservation subset adversarially (a first
  agent's `filtered_openings` evidence snippet was caught as non-verbatim and corrected).
- **Bugs fixed during the build:**
  - `PlanItem` didn't carry `description`, and `engine._commit` had a stray attempt to read
    it off the wrong object plus dead `created_by_start` code → added a `description` field
    to `PlanItem`, set it in the scheduler, and passed it cleanly into the create payload.
- **Tests run (all against production):**
  - `cli.py whoami` → correct identity + 4 teams (ml-framework, MI325-AMD-FWCollab,
    <your-team>, Hyperloom).
  - Full **dry-run** `cli.py plan` → 53/53 nodes enumerated, **4** reservations planned
    (node-a1, node-a2, node-a3, node-b1),
    each ~47.5h. **Zero writes.**
  - Spot-check of non-planned Pool-A nodes via `check_conflicts` → each already has a 48h
    reservation covering the window (correctly skipped, not a bug).
  - Flask app boots and renders `GET /` (HTTP 200); JSONL run log written to `runs/`.
- **Not yet done:** no real commit has been run — awaiting the user's go-ahead and the two
  confirmations below.

## 10. Post-build additions & open items

- **Docs added after the core build:** this `DISCOVERY_LOG.md`, `README.md`, and
  **`HOW_TO_RUN.md`** (run steps + how to hand the tool to colleagues).
- **Colleagues need their OWN API key** — the key *is* the person's identity (reservations
  are created as the key owner; access is per-user). Each colleague: generate their own key,
  set their own `AMD_EMAIL`/`ATS_SECRET` in a private `.env`, set `team_name` to one of
  *their* teams. Never share `.env`. (Full steps in `HOW_TO_RUN.md`.)
- **A memory note** was saved to the assistant's project memory capturing the auth mechanism,
  SDK usage, pool/user IDs, and eligibility rules, so a future session starts warm.
- **Open confirmations before a real commit:**
  1. Is `Hyperloom` the exact **Project** string the team expects? (API accepts any ≥3-char
     string, so a typo would still "succeed".)
  2. For the first real booking, cap the greedy run (e.g. `policy.max_nodes: 1`) or full send?

## Appendix A — Verified API facts (reference)

From the bundle reverse-engineering + live validation. Auth for all: `Authorization:
<email>:<api_key>` (SDK handles it); base `…/api/v1` and `…/api/v2`.

- **Create:** `POST /api/v2/calendar/reservation`, body = list of `CreateReservation`
  (see §6), extra header `conductor-client-source: WEB_APP`. Returns
  `{reservations:[CreateReservationRecord], notes:{failure reasons}}`.
- **Read reservations:** `GET /api/v2/calendar/reservation` (filter via `arg_list` of
  `{field:{comparator,value}}`, fields like `dates.date_start`, `users.user.id`).
- **Update / Delete:** `PATCH` / `DELETE /api/v2/calendar/reservation` (DELETE carries a
  body `[id]`, not a query param).
- **Availability:** `GET /api/v2/calendar/reservation/filtered_openings`
  (`target_ids` XOR `pool_id`, `open_length_size_minutes`, search window, `day_start/day_end`
  need a tz offset). Also `/openings` (simple) and `/conflict`, `/extension`.
- **Enumeration:** `POST /api/v1/amdql` — filter grammar `Table.column__OP`
  (`__EQ __NE __GT __GTE __LTE __IN __LIKE __REGEX __IS_NULL __IS_NOT_NULL`);
  pagination `limit` + `pagination_offset`; total-count in response header `Totalitems`.
  **Leading-wildcard LIKE is very slow — avoid.**
- **Identity:** `GET /api/v1/identity/ats` (used by app) / `/identity/me`; `/api_key`
  (GET/POST/PUT — self-service keys, shown once); `/auth/azure_oidc`, `/auth/logout`.
- **Errors:** server returns the message in an **`error-message` response header** (falls
  back to JSON body on HTTP 422). 5xx→error, 4xx→warning, 401→session-expired in the UI.
- **Pool fields that matter:** `reservation_strategy` (need `calendar`),
  `reservation_duration_limit` & `furthest_future_reservation` (seconds),
  `block_api_access`, `group_restrictions[/_met]`, `archived`.
- **Permission `reservation_admin_bypass`** lets an admin bypass constraints / view others'
  reservations — not used here.
