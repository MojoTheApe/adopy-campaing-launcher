# Handoff: Agent 10 RPA — continuing past Conversion Location → Website

Context dump for continuing this specific task in a fresh session. Everything
below reflects the state as of 2026-07-21, end of a long debugging session
that finally got a full, clean, live run from Create through Conversion
Location → **Website**.

## Where things are

- **Repo:** `C:\Users\EvgeniSedin\fb_agents` (NOT `C:\fb_agents` — that's a
  stale duplicate checkout on the same machine; it diverged and lacks
  several modules referenced here, e.g. `intent_runner.py`).
- **Safe rollback point:** git tag `agent10-website-reached-2026-07-21` on
  `master` — the exact commit where the flow was last verified working
  end-to-end. Visible on GitHub at
  `https://github.com/evgenis-ctrl/fb_agents/tags`. To recover if this
  session breaks something: `git checkout -b recovery-branch
  agent10-website-reached-2026-07-21` (does not touch anything else).
- **Working branch:** `feat/agent10-post-conversion-location` (branched from
  `master` at the tag above). Do all new work here; merge to `master` only
  once a new milestone is similarly verified live, the same way today's
  Website milestone was merged.
- **Dashboard:** `http://127.0.0.1:9090`, started via
  `C:\Users\EvgeniSedin\fb_agents\start_dashboard.bat`. Restart after any
  Python code change (recipes/YAML are read fresh, `.py` is not). Kill stray
  processes first — there is a recurring bug today where multiple `serve.py`
  processes pile up on the same port:
  `Get-CimInstance Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%serve.py%'" | Stop-Process -Force`.
- **`.env` already has `FB_RPA_ENGINE=recorded_flow` set** — this selects the
  new engine described below over the old recipe player. Leave it as-is.
- **Test account used today:** `FacebookUL921 CR Ait` (act_1337558731168884,
  country CR). Its Facebook Page ("Dough Joy") has **not accepted Meta's Lead
  Generation Terms of Service** — a real, account-level blocker unrelated to
  our code, shows as a red "Terms of Service Not Accepted" box once you reach
  the Ad set panel. Doesn't block filling fields, but does block actually
  publishing. Not our problem to fix; flag it to the user if it comes up
  again.

## What's CONFIRMED working today: the whole path to Website

`FB_RPA_ENGINE=recorded_flow` (set in `.env`, and also the CLI/dashboard
default now) runs `run_recorded_campaign_flow()` in
`src/fb_ads_rpa/meta_dom.py` — a **direct, linear replay** of a verified
human Playwright codegen recording, not the old label/breadcrumb/coordinate
cascade in `recipe_player.py` (that old system is still there and still used
when `FB_RPA_ENGINE` is empty, but stop trusting it as the default path —
today proved its assumptions were wrong in several places).

Confirmed live, repeatedly, both via CLI (`scripts/run_rpa_publish.py`) and
via the dashboard's own Publish button:

```
Create → (Manual campaign setup, if shown) → Leads → Continue
  → Campaign name (fill) → Budget (fill) → Next
  → Ad set name (fill)
  → Conversion location dropdown → Website (radio, exact) ✓ verified
```

Function signature: `run_recorded_campaign_flow(page, *, campaign_name,
budget, adset_name) -> str` — returns `""` on success, else a short slug
naming the step that failed (e.g. `"daily_budget"`, `"conversion_location_open"`).
Wired into `src/fb_ads_rpa/ads_manager_flow.py::publish_campaign` (checked
before the LLM agent and the recipe player), gated by
`src/fb_ads_rpa/config.py::get_use_recorded_flow()`.

## The next step: extend this same function past Website

Nothing after Website has been touched today. The remaining path (from the
2026-07-14 recording and general Ads Manager knowledge) is roughly:

```
Website ✓ (already working)
  → Conversion event (pick "Lead" or similar)
  → Audience (Locations, Detailed targeting, Languages, Age)
  → Placements (Advantage+ / Manual)
  → Next → Ad level
  → Ad name, Website URL, creative upload, primary text/headline
  → (Publish is explicitly out of scope — draft hand-off only, per
     Agent 10's standing constraint — never click Publish automatically)
```

**Recommended method — this is exactly what got today's breakthrough, reuse
it, don't go back to guessing:**

1. Record a fresh **Playwright codegen** session picking up right where
   automation currently stops (Conversion Location → Website already set),
   continuing through Conversion event → Audience → Placements → Ad. Command:
   ```
   python -m playwright codegen --target python -o output/codegen_next_segment.py https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=<test_account_id>
   ```
   **CRITICAL — security incident from earlier today:** this recording will
   capture the Facebook login (email/password/2FA) in plain text if you log
   in fresh during the recording. Last time this happened twice and both
   files had to be deleted immediately. If at all possible, avoid a fresh
   login (reuse an already-open session if codegen supports attaching to
   one), or if not, **strip the login block out of the file immediately
   after recording** and never let it touch git. Warn the user their
   Facebook password may need rotating if this happens again.
2. Read the recorded file, extract the real selectors it used
   (`get_by_placeholder`, `get_by_role(..., name="...")`, exact text) — these
   are ground truth, not guesses.
3. Extend `run_recorded_campaign_flow()` (or add a new function chained
   after it) using the **same pattern already in that function**:
   - `_click()` / `_fill()` helpers with retry + `_wait_loading()` before
     acting (Meta shows a transient "Loading..." overlay after several
     transitions, confirmed today — a plain click without waiting for this
     to hide can silently miss).
   - `_shot()` after every single step — this is what let today's session
     diagnose each new failure from one screenshot, one fix at a time,
     instead of guessing.
   - Prefer `get_by_placeholder` / `get_by_role(name=...)` over
     `get_by_label` — today's live evidence showed Meta's wizard inputs
     mostly have **no real ARIA label** despite looking labeled visually;
     `get_by_label` silently matches zero elements.
4. Test one segment at a time via a live run (CLI:
   `python scripts/run_rpa_publish.py --account "FacebookUL921 CR Ait"
   --country CR --slots-json <path> --dry-run`, or the dashboard). Fix
   exactly what the screenshot shows broke, retest, repeat.
5. Once a new milestone is reached reliably (ideally 2+ consecutive clean
   runs, not just one), commit, tag it the same way
   (`agent10-<milestone>-<date>`), and fast-forward-merge into `master`.

## Gotchas already found today (don't rediscover these)

- **Meta's footer "Next" button has no `role` attribute on its own text
  node** — role is inherited from an ancestor. `page.get_by_role("button",
  name="Next")` still resolves it correctly; the breadcrumb/sidebar-tree
  click-index workarounds elsewhere in `meta_dom.py` were solving the wrong
  problem.
- **Campaign name / Ad set name / Budget inputs have no real ARIA label** —
  match by placeholder (`"Enter your campaign name here"`, `"Enter your ad
  set name here..."`) or, for Budget, by role+name="Budget" with a
  `get_by_placeholder("Please enter amount")` fallback (both were seen live).
- **`read_input_near_label` (IVHP verify, in the OLD recipe-player path) had
  a bug**: it didn't exclude radio/checkbox inputs the way
  `fill_input_near_label` (fill) already did — a budget verify could read a
  nearby radio's `true`/`false` value instead of the amount. Fixed, but if
  you touch IVHP-based fills again, check this class of bug first.
- **Transient "Loading..." overlay** appears after Create, after Continue,
  after Next — and seemingly also on a periodic list-refresh, not just after
  navigation. A single wait-then-click can still race a fresh overlay;
  `_click`/`_fill` in `run_recorded_campaign_flow` retry up to 3× with a
  fresh `_wait_loading()` each time.
- **Reusing the same campaign/ad set name across many test runs makes Meta
  show a name-history autocomplete dropdown** that can cover the next
  control (Conversion location, in today's case). Dismissed via a neutral
  click (clicking the "Ad set name" heading), **never `page.keyboard.press
  ("Escape")`** — there's a standing note elsewhere in this file
  (`dismiss_open_popovers`) that Escape closes the *whole wizard*, not just a
  popover.
- **Conversion location menu shows "Website" directly** with "Single"
  already expanded — no separate "click Single to expand" step needed. Don't
  rely on `wait_conversion_menu_open()` (checks `[role=listbox]`/`[role=menu]`
  — this custom dropdown doesn't use those roles); wait for the "Website"
  radio to become visible directly instead.
- **Verifying the Website selection**: the menu auto-collapses right after
  clicking Website, so the original radio locator goes stale — don't try to
  re-check `is_checked()` on it. Also avoid re-deriving state via a
  label-walk from "Conversion location" (a green "Instant forms" suggestion
  banner sits between the label and the real control and can be picked up by
  mistake). Simplest reliable check: scan for a visible element whose own
  (direct text-node) content is exactly `"Website"` — see the JS in
  `run_recorded_campaign_flow`'s final check for the exact pattern.
- **Multilogin ground rules (already violated once today — don't repeat):**
  never close/restart a profile the user opened manually without explicit,
  fresh permission for that specific instance. Restarting to enable a CDP
  debug port does **not** log the user out (cookies persist on disk), but the
  restart mechanism itself (`MLX stop_profile` API) can fail (404 seen
  today, traced to a missing `folder_id` in `ad_account_map.csv` for one
  account — check that CSV has `multilogin_instance` + `folder_id` filled
  for whichever account you test next). If in doubt, ask before touching an
  already-open profile.
- **`ad_account_map.csv`** needs `multilogin_instance` (usually `A`) and
  `folder_id` (usually `3ef7f42d-a910-441f-b935-845a11fff08e` for this
  workspace) filled in for the MLX stop/restart API to work — one account
  (`FacebookUL921 CR Ait`) was missing both today and that's what caused the
  404.
- **Never commit/leave on disk any Playwright codegen recording that still
  has a plaintext Facebook login in it** — happened twice today, both
  deleted (login block only — see next point, don't overcorrect by deleting
  the whole recording).
- **`output/codegen_conversion_location_sanitized.py` now has the FULL
  recorded session** (95 lines) — not just through Website. It continues
  through Conversion event → Audience (Locations, Detailed targeting,
  Languages) → Platforms/Placements → Ad level (ad name, Website URL,
  creative upload, Headline). This is exactly the ground truth needed for
  the "next step" work above — read it before recording a fresh session,
  it may already answer most of what you'd otherwise re-record. (First pass
  today accidentally kept only the Website-reaching prefix when stripping
  credentials — restored from conversation context after the user pointed
  out the rest was needed too. Lesson: when sanitizing a credential leak,
  remove only the credential-bearing lines, not the whole file.)

## Files touched today (for `git diff` / review against the tag)

`src/fb_ads_rpa/meta_dom.py` (new `run_recorded_campaign_flow`, fixed
`read_input_near_label`, rewrote `click_sidebar_tree_index` to use real CDP
clicks), `src/fb_ads_rpa/ads_manager_flow.py` (new engine dispatch, added
`import logging`), `src/fb_ads_rpa/config.py` (new `get_use_recorded_flow`),
`src/fb_ads_rpa/recipe_player.py` (Next/sidebar-tree cascade rework — now
superseded by the recorded flow, kept as the non-`recorded_flow` fallback
path), `scripts/run_rpa_publish.py` (added missing `load_dotenv`),
`config/ad_account_map.csv` (filled `folder_id`/`country` for one account),
`config/rpa_recipes/campaign_golden.yaml` (skip s04/s08 pre-focus clicks),
`config/rpa_intents/daily_budget.yaml` (added "Budget" label).
