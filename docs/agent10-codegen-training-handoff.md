---
kind: handoff
name: agent10-codegen-training-handoff
created: 2026-07-23
status: draft
version: 1
tags:
  - rpa
  - agent10
  - handoff
  - codegen
related:
  - agent10-post-conversion-location-handoff.md
  - agent10-recorded-flow-runbook.md
  - agent10-codegen-via-multilogin.md
---

# Handoff: Agent 10 — codegen-training loop, ready to connect and test

Context dump for continuing in a fresh session. Picks up where
`agent10-post-conversion-location-handoff.md` left off (that one ends at
"Website selected" milestone, 2026-07-21). This one covers 2026-07-22/23.

## Where things are

- **Repo:** `C:\Users\EvgeniSedin\fb_agents` (same caveat as before — NOT
  `C:\fb_agents`, a stale duplicate).
- **Working branch:** `feat/agent10-post-conversion-location` (unchanged).
- **Nothing committed since `agent10-conversion-event-2026-07-21` tag** —
  everything below is uncommitted working-tree state. See "What to do
  first" below.
- **Dashboard:** now managed via `.claude/launch.json` → use
  `preview_start({name: "dashboard"})`, not raw `python serve.py` — this
  session hit repeated stray-process pileups on port 9090 from starting it
  manually; the managed preview avoids that. Config already created.
- **The user also runs their own separate dashboard instance on port 9091**
  (their own terminal/browser, not the Claude preview) — the two are
  independent processes reading the same files on disk (`Upload FB.xlsx`,
  `config/ad_account_map.csv`). Restarting the *preview* instance does not
  affect their 9091 instance and vice versa — if a code fix needs to reach
  their session, they need to restart their own process too.

## What changed this session (2026-07-22/23)

### 1. `run_recorded_campaign_flow()` extended end-to-end

`src/fb_ads_rpa/meta_dom.py` — the function now covers **Create through
draft hand-off** (was: Create through Website only, at the end of the last
session). New parameters: `interests`, `language`, `ad_name`,
`website_url`, `headline`, `primary_text`, `creative_filename`, all wired
from `AdSetDraft`/`AdDraft`/`CampaignDraft` fields in
`ads_manager_flow.py::publish_campaign`.

**Verification status — important, read before trusting any of this:**
- Steps 1–13 (Create → Website → Conversion event → Audience Locations
  country swap) — **live-verified repeatedly today**, works reliably.
- Steps 14–22 (Detailed targeting → Languages → Placements → Ad level →
  draft hand-off) — **written today from the full codegen recording
  (`output/codegen_conversion_location_sanitized.py`) but NEVER live-tested**.
  The account became unusable for testing today (a colleague was manually
  using the same browser window, plus general account slowdown after 60+
  automated runs). Full step-by-step detail, exact selectors, and known
  live gotchas for steps 1–13 are in `agent10-recorded-flow-runbook.md` —
  **read that before the next live test**, it tells you exactly what to
  expect to break and how the code currently reacts.

### 2. Real codegen-via-Multilogin, wired to the dashboard

New: `src/fb_ads_rpa/codegen_recorder.py` + routes `/rpa-codegen/start`,
`/stop`, `/status` in `src/web/app.py`. The dashboard's **"⏺ Record
(codegen)"** button (was "Record clicks") now opens a real Playwright
Inspector on the *already-open* Multilogin browser via `page.pause()` over
CDP — not a separate browser, no fresh-login risk. Full design rationale
in `agent10-codegen-via-multilogin.md`.

**This is the tool for training steps 14–22 next**, per the user's stated
goal: repeat codegen recordings often (safe — human-paced clicking looks
like normal usage, unlike the scripted automation loops that made the
account flaky) to cover more account/campaign variations, rather than
guessing selectors blind.

The old JSON action-logger (`click_recorder.py`, `/rpa-record/*` routes)
still exists and still works — just no longer wired to this button. Keep
it in mind as a fallback if `page.pause()` ever proves awkward for a given
session.

### 3. Fixed misleading "Network error" on dashboard Save buttons

`src/web/app.py` — `/account-link-patch` and `/account-map-patch` had
unhandled exceptions on file-save (most commonly `PermissionError` when
`Upload FB.xlsx` / `ad_account_map.csv` is open in Excel elsewhere).
Unhandled → FastAPI's default 500 came back as plain text, not JSON → the
dashboard's `fetch().then(r => r.json())` threw → UI showed "Network
error" regardless of the real cause. Both routes now catch `PermissionError`
specifically ("file is open elsewhere — close it and retry") and other
exceptions generically, always returning valid JSON. Verified live (hit
the endpoint directly, got a proper JSON error body back).

### 4. Cleanup

Deleted ~413MB / 1417 files in `output/rpa_screenshots/` and 62
`output/manual_test_run*.log` files accumulated from today's live-test
loop (confirmed gitignored/untracked first). `output/rpa_screenshots/`
will refill fast once live testing resumes — that's expected and fine.

### 5. Documentation package (also uncommitted)

A parallel thread this session: the user wants to take this from
"works on my laptop" to a shared, multi-user, domain-hosted setup. Wrote:

- `global-rollout-overview.md` — business context, current vs target
  architecture, phased rollout, explicitly **unresolved** open questions
  (hosting, dashboard auth, rollout scale) flagged as stakeholder
  decisions, not guessed at.
- `hub-agent-architecture.md` (+ `-en.md`) — hub+local-agent technical
  spec: data model, API contract, security, job lifecycle. Explicit
  section on what's reused as-is (the whole RPA engine, unchanged).
- `agent-update-strategy.md` (+ `-en.md`) — how to keep the local agent's
  code current on every employee's machine: git-based auto-update
  (recommended first) vs Intune-managed deployment (gated on confirming
  with IT that Intune is actually used for internal app deployment today).
- This is a **separate initiative** from `service-architecture.md`
  (pre-existing, per-customer SaaS spec for the analysis side) — don't
  conflate the two when picking this back up.

## What to do first in the next session

1. **Decide on committing.** Nothing from this session is committed yet.
   Recommend: one commit for the code (`meta_dom.py`, `ads_manager_flow.py`,
   `app.py`, `report.html`, `codegen_recorder.py`) once steps 14–22 get at
   least one live confirmation: separately, the six new `docs/*.md` files
   can be committed any time (pure documentation, no risk). Do NOT commit
   `output/dry_run_slots_cr_navtest.json` changes or `config/ad_account_map.csv`
   without checking what changed there first — the CSV was modified during
   this session via dashboard Save buttons (real account config), verify
   it's not test noise before deciding whether that belongs in this branch
   or should be committed separately/immediately for safety.

2. **Connect to the platform.** Confirm: Multilogin profile for the test
   account open and logged in; dashboard reachable (`preview_start({name:
   "dashboard"})`, or ask the user whether their own 9091 instance should
   be used instead for this round).

3. **Use "Record (codegen)" to cover steps 14–22.** For each remaining
   segment (Detailed targeting, Languages, Placements, Ad level, draft
   hand-off), click Record, walk through it manually once, copy the
   generated code out of the Inspector, compare it against what's already
   in `run_recorded_campaign_flow()` (lines are already written from the
   *original* recording — the new recording will tell you where today's
   live UI actually diverges). Update `agent10-recorded-flow-runbook.md`'s
   verification-status table as each segment gets confirmed.

4. **Vary account/campaign each time**, per the user's stated goal —
   the whole point of building the codegen-via-Multilogin path was to make
   it cheap to repeat across different accounts/campaign shapes instead of
   hand-tuning selectors against one single test account.

5. Only after 2+ clean full live runs (Create → draft hand-off, no fail
   step) — tag and fast-forward-merge into `master`, the same process used
   for the Website and Conversion-event milestones earlier.

#rpa #agent10 #handoff #codegen
