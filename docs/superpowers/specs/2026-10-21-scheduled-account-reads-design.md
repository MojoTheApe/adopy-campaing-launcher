# Campaigns always at hand, dates free: account reads decoupled from the period

## Description

The owner's goal: **always have access to accounts and campaigns, and jump freely between dates.**
Typical flow (owner's words): look at yesterday's result of a campaign → switch the dashboard to last week / month to
decide → immediately close the campaign or open a new one, from the same screen.

Today this needs two manual buttons per account — **Read spend** (account map row) and **Read campaigns** (campaign
table) — and a date change makes both look lost (TEST9095x):
- A date change is a full page reload (`report.html` `amapApply` ~:9731).
- Spend readings are stored per period (`account_spend.json`, key `account|from|to`) → a new period has no reading.
- The campaign snapshot is one per account (`campaign_inventory.json`) but its Meta Insights cells belong to the
  snapshot's own period.
- Both reads open the same browser profile and both read the campaign tree.

Decision (owner): **numbers come from BI; the campaign list is independent of dates.** BI numbers are enough to decide
(spend, leads, FTDs, Net per account / ad set / ad; mapped to campaigns by the existing `exact_name_join`).
Campaigns are sometimes edited by hand in Ads Manager, so the list must refresh — but not from date changes and not
on a fixed short timer.

Hard constraint: a walk over live accounts on a timer once preceded a Meta phone checkpoint. Every profile open is
30–100 s and visible activity; the official Meta app route (webhooks, server-side API) is closed to us. Profile opens
must stay rare and purposeful.

## Specifications

### 1. Dates never open a profile
- Changing the period re-renders numbers from the stats source (BI / archive / workbook — owned by the ACL-103
  session) and nothing else. No profile read is started by a period change.
- The campaign table always shows the last campaign structure (campaigns, ad sets, ads, statuses, budgets) for the
  account, whatever the period, with BI columns for the page period.
- `Maximum` (span=all) works for viewing (BI numbers + structure) — reads no longer depend on the period at all.

### 2. Scheduled refresh — three windows a day (option B)
- Windows: **morning from 08:00, midday from 13:00, evening from 18:00** (local time of the dashboard machine).
- The first time the dashboard page is open inside a window, the server refreshes **all accounts on the dashboard**
  (the account map list — 12 today) once, one profile at a time. A window that passes with no page open is skipped, not caught
  up later.
- Accounts read less than **1 hour** ago (by any trigger) are skipped in the sweep.
- The scheduled sweep is behind a setting (`ACL_AUTO_REFRESH`, on by default on working installs, off by default
  on test/dev instances). The owner's TEST9095 has it **on** (owner, 2026-10-21).
- If several dashboard processes run on one machine (9090 + 9095), only one sweeps (file lock in the config dir).
- Window state is persisted, so a restart or a reload inside the same window does not start a second sweep.

### 3. Refresh on attention (option C)
- When the owner expands an account (or a campaign of it) in the campaign table and its list is older than
  **1 hour**, the current list is shown immediately and the account is refreshed in the background.
- Attention reads go to the **front** of the queue, ahead of the scheduled sweep.

### 4. Check at the moment of action (option D)
- Commands (pause / activate / budget / bid) already re-read the live object before writing
  (`fb_api/commands.py` preview + execute, stale → nothing written). Keep that.
- New: when a command finds the object changed since the list was read (stale / differs from the snapshot), it says
  so and queues an immediate refresh of that account (front of the queue).
- New: after any successful command, refresh that account's list in the same profile session (no second open).
  Campaign creation from the builder likewise refreshes the account after publishing.

### 5. One read, one profile, one queue
- One read per account opens the profile once and reads **structure only**: campaigns, ad sets, ads with status,
  effective status, budgets, bid strategy. **No Meta spend, no Insights, no country split** (owner, 2026-10-21:
  BI is the decision basis; drop Meta spend while it makes reads heavier). The Meta account-spend column and the
  Meta Insights cells are removed from the screen; BI columns stay.
- If the profile is already warm (ACL-1812 `warm.py`), the read uses the warm tab (seconds). Otherwise it opens
  the profile, reads, and releases it as today's reads do. Profiles are **not** kept open all day by this feature
  (31 GB RAM, ~5 GB free, 12 accounts).
- Server-side queue: at most **one** profile read at a time across the dashboard. Priority: action-triggered >
  attention > scheduled sweep. Duplicate requests for an account already queued/running are merged.
- Honour existing stops: cooldowns, `GraphStop` / checkpoint, call ceiling, MLX batch refusal
  (`_mlx_batch_refusal`), publishing writer gate (`browser_operation.reading()`), hand-opened profile.
  Per-account problems (proxy error, logged out, no profile) mark that row; the queue moves on. Global stops halt
  the queue and show one banner above the table; nothing bypasses an active cooldown.

### 6. What the owner sees
- No **Read spend**, **Read campaigns**, **Read campaigns in <country>**, **Read all**, **Read again** buttons.
- Each account row: `list 14:20` (time of the last successful structure read), `updating…` while queued/running,
  or the failure reason + one **Retry** (forces a read of that account, front of the queue).
- Banner for a global stop; a small line while a scheduled sweep runs (`Refreshing accounts 3/12`).
- Server endpoints `/account-spend/read` and `/campaign-inventory/read` may stay for Retry, tests and scripts.

### 7. Telemetry
- Each read records its trigger (`schedule:<window>`, `attention`, `action`, `retry`) in the existing telemetry
  (`rpa_telemetry.jsonl` step `api_read`, `fb_api_pilot.jsonl` event `fb_api_read`); skips record
  `skipped_fresh`. Goal: prove that date jumps open nothing and count opens per day.

### Settled details (coordinator's calls)
- "Page open in a window" = the dashboard page loaded (page-load request) inside the window.
- After a cooldown / global stop ends, the sweep resumes for the rest of the same window.
- "Changed by hand" for commands = status or budget differs from the snapshot; the existing stale check stays.
- Global failures (MLX agent down `WinError 10061` / `launcher.mlx.yt` unreachable, checkpoint, call ceiling)
  show ONE banner and halt the queue instead of a failure on every row; per-account failures are one short line.

## Non-goals
- Fixed short timers / continuous polling; webhooks or any official-app route.
- Meta's own metrics (spend, impressions, clicks, frequency) — BI numbers are the decision basis.
- Changing how commands validate and write.
- The stats source itself (BI, workbook merge, accumulating BI archive) — ACL-103 session.

## Anti-goals (failure even if it "works")
- A date change that opens a profile.
- Two profiles open at once because of auto-refresh.
- More profile opens per day than today's manual use for the same work.
- Numbers from one period shown under another period's label.
- A command written over a hand edit without noticing.

## Acceptance
- Jump through 10 periods → numbers change instantly, telemetry shows zero reads.
- First page open after 08:00 → all accounts refreshed one by one, once; reload in the same window → no new sweep.
- Expand an account whose list is 2 h old → old list visible at once, `updating…`, then `list <now>`.
- Pause a campaign that was edited by hand → nothing written, message shown, account refreshed.
- Proxy failure on one account → reason + Retry on that row; the sweep continues.

## Implementation notes (2026-10-24)

- Queue state: machine-local `account_refresh.json`; short atomic transactions are independent of the long dispatcher/profile read leases. The latter share the existing machine profile-lease root across ports. Failed/corrupt state is not reset to forget a completed window.
- Routes: `POST /account-refresh/page-open`, `POST /account-refresh/request` (attention/retry only), `GET /account-refresh/state`. The server takes accounts from the current map. No period is passed to the structure reader.
- Workbook navigation uses a one-time `view_only` page transition. It is consumed without a page-open admission; cached inventory/BI GETs still work. Polling while work is pending only reads local queue state.
- Automatic startup may resume current-window persisted work, but never catches up an expired window. Attention/action/retry requests are retained. Same-account requests join running reads.
- Read results from confirmed commands/publishing settle the queue using the existing open session without opening again or resetting its Graph call budget. Unknown/failed/cancelled/dry publishing does not invoke a successful-publish refresh. Existing write reconciliation remains unchanged.
- The working packaged application defaults on. Source port9090 retains its working fallback; other source ports and `--reload` default off. `ACL_AUTO_REFRESH=1` explicitly enables TEST9095. Actual launch environment and real profile observations are deployment facts, separate from offline automated checks.
