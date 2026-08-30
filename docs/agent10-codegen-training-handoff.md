---
kind: handoff
name: agent10-codegen-training-handoff
created: 2026-07-24
status: draft
version: 2
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

# Handoff: Agent 10 — post codegen-via-Multilogin experiment, account paused

Context dump for continuing in a fresh session. Supersedes v1 of this
document (written earlier on 2026-07-23, before the codegen-Inspector
experiment below ran and failed). Picks up where
`agent10-post-conversion-location-handoff.md` left off.

## Where things are

- **Repo:** `C:\Users\EvgeniSedin\fb_agents` (NOT `C:\fb_agents`, a stale
  duplicate — confirmed again this session: `preview_start` with a
  `.claude/launch.json` resolved to the WRONG checkout at one point because
  that stale duplicate has its own `.claude/launch.json` higher up the
  session's default directory tree. If the dashboard ever shows missing
  accounts / stale `RPA_FLOW_VERSION` in its logs, that's the symptom —
  just run `python serve.py` directly via Bash from
  `C:\Users\EvgeniSedin\fb_agents`, don't rely on `preview_start({name:...})`
  for this project).
- **Working branch:** `feat/agent10-post-conversion-location`.
- **Account state right now: paused, user reported it "hung" (подвис).**
  Do not run more live tests until the user confirms the Multilogin profile
  is responsive again. The last live test before the freeze failed early at
  the `create` step (likely transient, unrelated to any code change) —
  don't read anything into that specific failure.
- **Nothing new committed this session** (last commit: `e7e664a`,
  "extend recorded flow past Website, live-fix onboarding modal + Locations
  panel access, add codegen recorder" — already pushed to GitHub). Since
  then: further uncommitted edits, see below.

## The big finding this session: Playwright's Inspector doesn't work on Multilogin's Mimic browser

This is the most important thing to know before touching the "Record"
button again. Full story in `agent10-codegen-via-multilogin.md` (rewritten
to v2) — summary:

- Built `src/fb_ads_rpa/codegen_recorder.py`: connects over CDP to the
  already-open Multilogin browser and calls `page.pause()` to open the real
  Playwright Inspector (the same UI `playwright codegen` uses) on that same
  window — no second browser, no fresh login.
- Wired it to the dashboard's "Record" button, tested it live against
  `FacebookDQ204 CR`.
- **It does not work.** Confirmed twice: the Inspector genuinely connects
  (window flashes when toggling its own Record button, CDP handshake
  succeeds), but neither "Pick locator" (hover highlighting) nor "Record"
  (click capture) produce any effect at all — no highlight on hover, no new
  code lines on click, even with the Inspector's own Record toggle visibly
  turned on (red).
- Ruled out user error: at the same moment, the **old** `click_recorder.py`
  (a plain `document.addEventListener('click', ..., true)` injected via
  `page.evaluate()`, not Playwright's built-in Inspector machinery) was
  running against the exact same CDP connection and **did** capture the
  click correctly, own_text and all. So it's specifically Playwright
  Inspector's own mechanism (likely the CDP `Overlay` domain) that Mimic's
  antidetect modifications break — not the CDP connection itself, and not
  a general "no JS works here" problem. Regular scripted automation
  (`Input.dispatchMouseEvent` etc., what `run_recorded_campaign_flow` uses
  all day) is unaffected.
- **Reverted the dashboard button back to `click_recorder.py`** (now
  labeled "⏺ Record clicks" again). `codegen_recorder.py` is left in the
  repo (its account-based CDP-port lookup fix is independently useful, see
  below) but the dashboard no longer calls it.
- **Do not try `page.pause()`/Inspector again on this browser** without new
  information (e.g. Multilogin explicitly announcing DevTools
  Overlay/Inspector compatibility). It's a dead end here.

### Real, incidental win from this experiment

While debugging the above, found and fixed a **real, separate bug**: both
`click_recorder.py` and `codegen_recorder.py`'s CDP-port discovery
(`_find_cdp_url()`) uses a fixed list of common ports and missed the port
an active test session's browser was actually running on. Added
`_find_cdp_url_for_account(account_label)` to both modules — looks up the
account's `profile_id` in `ad_account_map.csv`, finds its MLX profile
directory, and reads the real port from the profile's own
`DevToolsActivePort` file (same mechanism `browser.py`'s
`MultiloginDirectCDPProvider` already used for actual RPA runs). Confirmed
live: found the correct port (`56413`) on the first try where the old
scan found nothing. The dashboard's "Record clicks" button now passes
`account_label` through to use this.

### What the working recorder captured (useful ground truth, not yet fully applied)

Used the now-confirmed-working `click_recorder.py` to capture the exact
click sequence for the "Further limit the reach of your ads" panel-reopen
step that was blocking progress past Audience Locations on
`FacebookDQ204 CR`. Captured JSON (`output/rpa_recipes/probe_test_*.json`):

```
click "Further limit the reach of your ads" — own_text exact match,
  role="" on the element itself (role inherited from a div[role=button]
  ancestor a few levels up — same pattern as the "Next" footer button
  elsewhere in this file)
click "Continue" — immediately follows, same own_text/role="" pattern
```

Applied this as a fix in `run_recorded_campaign_flow()`
(`src/fb_ads_rpa/meta_dom.py`): replaced the unreliable
`get_by_text(...).first.click()` / `get_by_role("button", name=...)`
attempts for this specific reopen step with the same own-text-node +
`_cdp_mouse_click()` pattern already proven for Website/Lead selection
(`get_by_role` wasn't resolving reliably through this much DOM nesting in
practice, even though the role-inheritance theory is sound).

**Not yet live-verified** — the one test run attempted right after this
fix failed early at `create` (looked transient/unrelated), and then the
account reportedly froze before a clean retest could happen. **This is
the first thing to verify once the account is responsive again.**

## Current uncommitted changes (git status)

```
 M src/fb_ads_rpa/click_recorder.py       — added _find_cdp_url_for_account, account_label param
 M src/fb_ads_rpa/codegen_recorder.py     — same fix (kept for reference, not wired to dashboard)
 M src/fb_ads_rpa/meta_dom.py             — own-text+CDP-click fix for "Further limit reach" reopen + Continue
 M src/web/app.py                         — /rpa-record/start and /rpa-codegen/start both accept account_label
 M src/web/templates/report.html          — Record button reverted to click_recorder wiring, passes account_label
?? output/manual_test_run63.log..71.log   — test noise, don't commit
?? creatives/                             — unrelated, don't commit
 M output/unknown_targets.log             — pre-existing, unrelated to this session, don't commit
```

`docs/agent10-codegen-via-multilogin.md` was also rewritten (v2) to
document the Inspector-doesn't-work finding — should be committed
regardless of whether the meta_dom.py fix above is verified yet, since
it's accurate regardless.

## What to do first in the next session

1. **Check the account/Multilogin profile is responsive** before running
   anything. The user reported it hung after the last test attempt — don't
   assume it's fine just because some time has passed.
2. **Live-verify the "Further limit the reach" reopen fix** in
   `meta_dom.py` (see above) — this is the immediate next milestone, one
   step past where testing stalled all of 2026-07-22/23. Test against
   `FacebookDQ204 CR` (act_1234567890123456, profile_id
   `550e8400-e29b-41d4-a716-446655440000`, already filled into
   `config/ad_account_map.csv` this session) or `FacebookUL921 CR Ait`
   (the original, much more heavily-used test account).
3. **If it works**, use the dashboard's "Record clicks" button (now fixed)
   to capture the *next* unverified segments the same way — this is the
   real, working version of the "train faster with recordings" loop the
   user wanted, just via JSON action capture instead of live codegen.
   Manually translate each captured `own_text`/`role`/`css_path` into an
   own-text + `_cdp_mouse_click()` step in `meta_dom.py`, same pattern as
   today's fix.
4. **Commit in stages, not all at once**: commit the
   `agent10-codegen-via-multilogin.md` doc update immediately (safe, no
   live-test dependency). Commit the code changes once the reopen fix is
   verified live. Review `output/unknown_targets.log`'s diff before ever
   including it in a commit — it's unrelated to this work.
5. Only after 2+ clean full live runs (Create → draft hand-off, no fail
   step) — tag and fast-forward-merge into `master`, same process as the
   Website and Conversion-event milestones.

#rpa #agent10 #handoff #codegen
