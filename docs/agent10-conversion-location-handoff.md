# Handoff: Agent 10 RPA — remaining blocker is "Conversion location → Website"

Context dump for continuing this specific task in a fresh session. Everything
below reflects the state as of 2026-07-20, end of a long debugging session.

## What Agent 10 is

Playwright-based browser automation that fills out and drafts a real campaign
in **Facebook Ads Manager's UI** (not the Marketing API — a deliberate choice:
API calls would all originate from one server IP, risking Meta linking
otherwise-isolated ad accounts that are normally kept apart via per-account
Multilogin proxies). Entry point: the dashboard's Campaign Builder →
Preview/Publish buttons, or CLI `scripts/run_rpa_publish.py`.

**Real repo in use:** `C:\Users\EvgeniSedin\fb_agents` (NOT `C:\fb_agents` —
that's a stale duplicate checkout on the same machine, do not edit it; it
diverged and lacks several modules this doc references, e.g. `intent_runner.py`).

**Dashboard:** `http://127.0.0.1:9090`, started via
`C:\Users\EvgeniSedin\fb_agents\start_dashboard.bat` (same thing the desktop
shortcut "Adopy Campaigns Launcher" runs — `cd` into the repo + `python serve.py`).
Restart after any Python code change (recipes/YAML are read fresh, .py is not).
Kill stray processes first: `Get-CimInstance Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%serve.py%'" | Stop-Process -Force`.

**Test account:** `FacebookElf191 SA` → mapped to country **Peru** via the
`country` column in `config/ad_account_map.csv` (added because its real
account-name suffix "SA" was misleading — GSheet/CSV country resolution logic
lives in `src/analysis_agent/morning_service.py::_fallback_accounts_from_csv`).
Test path in dashboard: sidebar → Peru → Campaign Builder → FacebookElf191 SA
→ fill a slot → Preview → Publish. Requires the Multilogin profile for that
account open (or `MULTILOGIN_AUTOMATION_TOKEN_A` in `.env`, already set, lets
RPA start the profile itself via API).

## Two engines (same call shape, selectable via env)

- **Default (free): `src/fb_ads_rpa/recipe_player.py`** — replays
  `config/rpa_recipes/campaign_golden.yaml` (a recorded-then-hardened step
  list), with a hybrid "IVHP" verified-fill system for the fields that kept
  silently failing (see below). This is the one to keep iterating on — no API
  cost.
- **Opt-in (costs real Anthropic API $, `FB_RPA_ENGINE=llm_agent` in `.env`):
  `src/fb_ads_rpa/llm_agent.py`** — a live Claude-driven loop that reads the
  accessibility tree and decides each click itself instead of replaying a
  script. Built today, works better than the static recipe in some ways (got
  further on its own, gave a detailed stuck-reason on failure) but is
  unproven end-to-end and **the user is currently out of purchased API
  credits** — do not enable this without confirming a budget first.
  `FB_RPA_ENGINE=` (empty) in `.env` currently selects the free engine.

Engine selection lives in `src/fb_ads_rpa/ads_manager_flow.py::publish_campaign`
(checks `get_use_llm_agent()` then `get_use_recipe_player()`, both in
`src/fb_ads_rpa/config.py`).

## What's CONFIRMED working today (don't re-break these)

1. **Campaign name** fill — via IVHP (`config/rpa_intents/campaign_name.yaml`
   + `src/fb_ads_rpa/intent_runner.py::run_intent`). Fixed by disambiguating
   `s04_campaign`'s click match (added `label: Campaign name` — it was
   ambiguously matching some other combobox before).
2. **Daily budget** fill — same IVHP treatment, added today
   (`config/rpa_intents/daily_budget.yaml`) after live evidence showed the
   value silently reverting to Meta's $25 default (classic React-controlled-
   input problem: a JS-only `.value` set gets reverted by React unless a real
   keyboard event fired it — IVHP's `get_by_label_type` strategy does real
   `.type()` typing as a fallback, which survives this).
3. **Ad set name** fill — same IVHP treatment (`config/rpa_intents/adset_name.yaml`).
4. **Discard-drafts dialog** — `md.dismiss_discard_drafts_dialog()`, wired into
   `_guards()` and `_reopen_wizard_from_list()`. A stray click used to leave
   this confirmation open and unhandled, blocking everything after it. Always
   clicks Cancel, never Discard.
5. **Level navigation (Campaign→Ad set→Ad)** — was relying on clicking the
   footer "Next" button, which proved unreliable across ~6 different fix
   attempts today (label matching, wizard-open false-positive removal, settle
   delays, universal after-click verification, sidebar-tree rescue — see git
   history / this file's "already tried" section below). **Final approach,
   suggested by the user and just implemented, untested yet**: click the
   **top breadcrumb** ("Campaign" / "Ad set" / "Ad" segments) directly instead
   of the Next button. `md.click_wizard_breadcrumb_index(page, index)` (0/1/2)
   is now tried FIRST for `s07_campaign` and `s49_adset`, falling back to
   `md.click_sidebar_tree_index` then the old Next-button cascade only if the
   breadcrumb click doesn't work. Code: `recipe_player.py::_generic_click`,
   look for `level_target = {"campaign": 1, "adset": 2}.get(...)`.
   **This is the most recent change and has not been live-tested — verify
   this actually resolved level navigation before spending more effort on
   Conversion Location, since a broken navigation upstream will masquerade as
   a Conversion Location failure downstream (see "false trail" note below).**

## THE remaining blocker: Conversion location → Website

Goal: in the Ad Set panel, "Conversion location" must end up as exactly
**"Website"**, not the default **"Website and instant forms"**. Sequence:
open the dropdown → menu shows "Multiple" and "Single" sections → click
"Single" to expand it → click the "Website" option within (not the "Website
and instant forms" default value — these are different, visually similar
elements, easy to confuse programmatically).

### Code involved

- `recipe_player.py::_select_conversion_location_panel` (delegated section
  handler, called once per run when `section == "conversion_location"`).
- `meta_dom.py::select_conversion_location_website` — the actual driver, up to
  3 retry attempts, each: `click_conversion_location_dropdown` →
  `wait_conversion_menu_open` → guard against opening the wrong (Conversion
  Event) menu → `_pick_website_from_open_menu`.
- `meta_dom.py::_pick_website_from_open_menu` — expands "Single"
  (`_expand_conversion_location_single`, itself tries: role=heading match,
  subtitle-text match, exact-text match, each with a scroll-into-view + mouse
  click) then tries 7 (!) different strategies to actually select "Website":
  keyboard focus+Space (the human-recorded golden path —
  `_click_website_via_keyboard_focus`), raw CDP mouse click, the recorded
  JSON recipe (`config/rpa_recipes/conversion_location_website.json`),
  Playwright mouse/force/JS click, arrow-key nav, aria-controls fallback.
- Recorded reference: `config/rpa_recipes/conversion_location_website.json` —
  captured from a real human doing this exact flow on 2026-07-14. Notes in
  that file: "Website selected by Space keydown on SPAN after Single
  expanded. Not mouse click." — i.e. Space-on-focused-element is the strategy
  known to have worked at least once for a human-recorded session.

### What's been tried and the honest state

This step already has **more defensive fallback logic than any other single
step in the codebase** (7 strategies just for the final click). It was not
possible to get clean, isolated evidence for *why* it fails today, because
every live test so far got derailed upstream first — either the Next-button
navigation problem (fixed today, untested) or the adset_name revert problem
(fixed today). **We have never actually seen this step fail on a clean run
where everything before it definitely worked.** That's the single biggest
unknown here — don't assume Conversion Location itself is broken until a run
reaches it cleanly and it *still* fails.

The live LLM agent (`llm_agent.py`), on its one deep attempt, also struggled
here specifically — it opened the dropdown, got confused about "Single" vs
directly filling text into a non-fillable dropdown, accidentally landed on
unrelated fields (Locations, an unrelated toggle), and eventually gave up.
That run is informative as "this is a genuinely fiddly UI interaction even
for a live adaptive agent," but its actions were ad-hoc/exploratory, not a
clean test of the existing `meta_dom.py` logic.

### Evidence/screenshots already on disk (from earlier runs, may be stale)

- `output/rpa_screenshots/20260720_042346_player_fail_conversion_location.png`
- `output/rpa_screenshots/20260720_042223_player_before_conversion_location.png`
- `output/rpa_screenshots/20260720_042224_player_conversion_label_missing.png`
- (older, 07-17/07-18, same filenames pattern — likely stale relative to
  today's Meta UI, re-capture before trusting them)

### Recommended next step (not yet done)

The user suggested, and this is a good idea given repeated blind-guess fixes
haven't panned out for the Next button either: **record a fresh, live
`playwright codegen` session of a human actually doing Conversion Location →
Single → Website on the current Meta UI**, then diff the captured
selectors/flow against `conversion_location_website.json` (2026-07-14) and
`meta_dom.py`'s hardcoded text patterns. Six days is enough time for Meta to
have changed something. Command:
```
playwright codegen https://adsmanager.facebook.com
```
Do this only once level navigation is confirmed fixed (see above) — otherwise
you'll be recording/debugging against a session that never legitimately
reaches this panel.

### Debugging workflow that worked well today (reuse it)

1. Run a live Publish attempt (free engine, `FB_RPA_ENGINE` empty).
2. Read the returned error + step list (shows `recipe:<step_id>:ok|fail` for
   every step — very informative on its own for spotting where things
   diverge).
3. Check `output/rpa_evidence/<timestamp>/<intent_id>/` for IVHP-covered
   fields — `detail.txt` (expected vs current value), `strategies_tried.json`
   (which of the fill strategies fired and whether they verified),
   `screenshot.png`, `aria_snapshot.md`, `url.txt`.
4. For non-IVHP failures, check `output/rpa_screenshots/` for
   `player_before_<step>_click.png` / `player_after_<step>_click.png` pairs
   (added today) plus the various `player_fail_*`/`player_*_not_found.png`
   labels sprinkled through `recipe_player.py`.
5. Cross-reference the screenshot's visible URL/breadcrumb against what the
   step *should* show — this caught the Next-button false-"ok" bug (the
   screenshot kept showing the Campaign panel with $25 default budget despite
   `s06_campaign:ok`/`s07_campaign:ok` in the log).

### One structural gotcha to remember

`current_step=N` in the Ads Manager URL query string is **not reliable** —
observed staying at `current_step=0` even after other signals suggested
navigation succeeded. Don't use it as a diagnostic signal; trust
`get_by_text("Campaign name"/"Ad set name"/etc., exact=True).is_visible()`
checks and screenshots instead.

## Files touched today (for `git diff` / review)

`src/fb_ads_rpa/recipe_player.py`, `src/fb_ads_rpa/meta_dom.py`,
`src/fb_ads_rpa/config.py`, `src/fb_ads_rpa/ads_manager_flow.py`,
`src/fb_ads_rpa/llm_agent.py` (new), `src/fb_ads_rpa/intent_runner.py`
(pre-existing, reused), `config/rpa_intents/adset_name.yaml` (new),
`config/rpa_intents/daily_budget.yaml` (new), `config/rpa_forbidden_zones.yaml`
(added "Publish" pattern), `config/ad_account_map.csv` (added `country`
column), `src/analysis_agent/morning_service.py` (GSheet+CSV account merge
fix, unrelated to this specific blocker), `.env` (added `ANTHROPIC_API_KEY`,
`FB_RPA_ENGINE`, `MULTILOGIN_AUTOMATION_TOKEN_A` was already present).
