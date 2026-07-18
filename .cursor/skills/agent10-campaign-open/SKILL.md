---
name: agent10-campaign-open
description: >-
  Agent 10 gate: open Meta Ads Manager campaign wizard through Ad set name and
  Conversion location → Single → Website (exact). Use for /rpa-campaign-open,
  "open campaign wizard", Create→Leads→Continue, Ad set name, or Website
  (not Instant forms) failures.
---

# Agent 10 — Campaign open → Ad set Website gate

Slash: `/rpa-campaign-open`  
Repo root: `fb_agents`

## Goal (stop here)

```
Campaigns list → Create → Manual (if shown) → Objective Leads → Continue
  → fill Campaign name + Daily budget
  → Next → Ad set name
  → Conversion location dropdown → Single → Website (exact one word)
```

**Never** select `Website and instant forms` / `Website & Instant forms`.  
**Do not** touch Conversion event, targeting, creatives, or Publish in this skill.

## Ground rules

1. Multilogin only (Desktop API / DirectCDP). Never Playwright Chromium against MLX Chrome 149+ profiles.
2. **Never close the Multilogin/Mimic window** after launch, Publish, cancel, or failure. Disconnect CDP only — leave the profile open so the user can verify Facebook is logged into the right account. Never `stop_profile` / kill Mimic as cleanup. (See `.cursor/rules/agent10-multilogin-keep-open.mdc`.)
3. Never click `button.layerConfirm` after Continue — it closes the wizard.
4. Wait for `Verifying your edits…` / All edits saved before fills.
5. Prefer `FB_RPA_RECIPE_PLAYER=1` and `FB_RPA_RECIPE_STOP_AFTER=s12_conversion_location` (serve.py default).
6. After Ad set name fill, Meta may jump to Ad level — restore Ad set before Conversion location.
7. Log outcomes into `config/rpa_variations/strategy_ranks.json` (safe steps may auto-promote; Website is risky → pending_approval).
8. Never let `dismiss_publish_draft_dialog` click wizard Close (matches "In draft" chrome) — that yields `player_lost_wizard_campaign`.
9. **Forbidden zones** (`config/rpa_forbidden_zones.yaml` + `wizard_guard.py`): never click exit/help chrome; if URL leaves Ads Manager, recover (`history.back` / reopen draft).
10. **IVHP** (`intent_runner` + `config/rpa_intents/`): click/fill ≠ success. Campaign name must **verify** in the field; fail → `output/rpa_evidence/` + stop. Do not chase Website while name/budget/Next are red.

## Forbidden zones (cheat sheet)

**Never click**
- `About this update` (blue measurement banner → `business/news/click-attribution`)
- `About daily budget` / `About campaign budget` / `About campaign objectives`
- `Learn more` (help/news)
- Wizard footer/header `Close` / `Cerrar` (draft dialog **Cancel** is OK)
- `Discard drafts` / `Review and publish…`
- Far-left global Ads Manager nav (Home / Billing / etc.)

**If URL contains** `/business/news/` or `/business/help/` → do not keep clicking; recover (back / close news tab / reopen draft). Evidence: `player_guard_left_*`, `player_guard_recover`.

**Extend zones:** user sends screenshot → append pattern + `evidence` in YAML + one line here.

## Checklist

| # | Must result | Evidence |
|---|-------------|----------|
| 1 | Ads Manager campaigns for correct `act=` | shot `01_campaigns` |
| 2 | Create clicked; objective chooser or Manual | `02_*` |
| 3 | Leads selected; Continue | modal advances |
| 4 | Wizard open (`Campaign name` / `/edit/`) | `player_wizard_ready` or `03_wizard_started` |
| 5 | Name + budget filled | `04_campaign_level` / player campaign shots |
| 6 | Ad set name filled | `s09_adset` / `player_adset_after_rename` |
| 7 | Conversion location = **Website** only (not Instant forms) | `gate_adset_website` / `player_gate_adset_website` |

First failing row = only thing to fix.

## Related

- Spec: `docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md`
- Forbidden zones: `docs/superpowers/specs/2026-07-17-wizard-forbidden-zones-design.md`
- Learn skill: `.cursor/skills/agent10-rpa-learn/SKILL.md`
- Variations: `config/rpa_variations/`
- PoC Website (out of scope here): `scripts/rpa_ai_website_poc.py`
