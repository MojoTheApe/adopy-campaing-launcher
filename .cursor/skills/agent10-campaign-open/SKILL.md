---
name: agent10-campaign-open
description: >-
  Narrow Agent 10 gate: open Meta Ads Manager campaign wizard through Campaign
  name + daily budget only. Use for /rpa-campaign-open, "open campaign wizard",
  Create→Leads→Continue failures, Lost campaign wizard, or when stabilizing RPA
  before Conversion location work.
---

# Agent 10 — Campaign open (narrow gate)

Slash: `/rpa-campaign-open`  
Repo root: `fb_agents`

## Goal (stop here)

```
Campaigns list → Create → Manual (if shown) → Objective Leads → Continue
  → wizard open with Campaign name visible
  → fill Campaign name + Daily budget
```

**Do not** touch Conversion location, Ad set targeting, creatives, or Publish in this skill.
Success metric: **3 consecutive green runs** before expanding scope.

## Ground rules

1. Multilogin only (Desktop API / DirectCDP). Never Playwright Chromium against MLX Chrome 149+ profiles.
2. **Never close the Multilogin/Mimic window** after launch, Publish, cancel, or failure. Disconnect CDP only — leave the profile open so the user can verify Facebook is logged into the right account. Never `stop_profile` / kill Mimic as cleanup. (See `.cursor/rules/agent10-multilogin-keep-open.mdc`.)
3. Never click `button.layerConfirm` after Continue — it closes the wizard.
4. Wait for `Verifying your edits…` / All edits saved before fills.
5. Prefer `FB_RPA_RECIPE_PLAYER=1` and `FB_RPA_RECIPE_STOP_AFTER=s06_campaign` (serve.py defaults) — golden steps `s00`–`s06` only.
6. Log outcomes into `config/rpa_variations/strategy_ranks.json` (safe steps may auto-promote).
7. Never let `dismiss_publish_draft_dialog` click wizard Close (matches "In draft" chrome) — that yields `player_lost_wizard_campaign`.

## Checklist

| # | Must result | Evidence |
|---|-------------|----------|
| 1 | Ads Manager campaigns for correct `act=` | shot `01_campaigns` |
| 2 | Create clicked; objective chooser or Manual | `02_*` |
| 3 | Leads selected; Continue | modal advances |
| 4 | Wizard open (`Campaign name` / `/edit/`) | `player_wizard_ready` or `03_wizard_started` |
| 5 | Name + budget filled | `04_campaign_level` / player campaign shots |

First failing row = only thing to fix.

## Agent procedure

1. Confirm dashboard `serve.py` up with Recipe Player on (`serve_err` / env).
2. Inspect latest job `steps_completed` + newest `output/rpa_screenshots/*`.
3. Name first broken checklist row; do **not** jump to Website.
4. Smallest fix only (skip harmful step, wizard tab bind, dismiss Review draft via Cancel).
5. Re-run until Campaign name+budget OK ×3, or stop with a one-page failure note.

## Related

- Spec: `docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md`
- Learn skill: `.cursor/skills/agent10-rpa-learn/SKILL.md`
- Variations: `config/rpa_variations/`
- PoC Website (out of scope here): `scripts/rpa_ai_website_poc.py`
