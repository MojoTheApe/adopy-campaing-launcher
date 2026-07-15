---
name: agent10-rpa-variations
description: Use when debugging Agent 10 RPA publish, Ads Manager selectors, teach-mode, Multilogin, conversion Website, rpa_screenshots, variation catalog, strategy ranks, or /rpa-audit.
---

# Agent 10 RPA Variations

## Core rule

Agent 10 RPA creates Meta Ads Manager campaigns for human hand-off only. The flow is **Draft-only**: never click **Publish**, never rank Publish selectors, and never treat Publish as a learnable strategy.

Ground-truth recipe:

```text
Create -> Manual setup -> Objective Leads -> Continue
  -> Campaign: name + daily budget
  -> Ad set: Conversion location Website, Conversion event Lead
  -> Audience / targeting / placements
  -> Ad: creatives + copy
  -> Leave as Draft (hand-off)
```

## Required navigation model

Ads Manager has left tabs **Campaign / Ad set / Ad** plus section panels inside those levels. Audit failures against this order:

| # | Left tab | Required result | Evidence label |
|---|----------|-----------------|----------------|
| 1 | - | Campaigns `act=` opened; Create visible | `01_campaigns` |
| 2 | - | Manual setup selected, not AI-guided | `02_*` |
| 3 | - | Leads objective continued to campaign form | `03_wizard_started` |
| 4 | Campaign | Campaign name and budget filled | `04_campaign_level` |
| 5 | Ad set | Ad set tab/level visible | `04b_*` |
| 6 | Ad set | Conversion location = Website, not Instant Forms/hybrid | `04d_*` |
| 7 | Ad set | Conversion event = Lead | `04e_*` |
| 8 | Ad set | Audience, targeting, placements filled | `05_*` |
| 9 | Ad | Creative and copy filled | `ad_*` |
| 10 | - | Left as Draft / hand-off; Publish not clicked | `99_draft` |

## Hybrid C learning rules

- Always log per-step strategy outcomes with strategy id, ok/fail, timestamp, and screenshot path.
- Safe success: auto-promote only when the DOM verifies the step **and** the run reaches the next major left-tab/checklist step.
- Safe fail: increment fails and demote.
- Risky success: write `pending_approval` with evidence; do not change default order until teach-mode or explicit human approval.
- Risky fail: log and keep the teach-mode path available.
- Browser or Multilogin launch crash: no UI strategy learning.
- Out of scope: **Publish**. Leave Draft-only.

Initial risky list is conversion only:

- `conversion_location_website`
- `conversion_event_lead`

All other checklist steps are safe unless the variation catalog says otherwise.

## Paths to read first

- Ground-truth checklist: `config/rpa_variations/checklist.md`
- Variation catalog: `config/rpa_variations/steps.yaml`
- Ranked store: `config/rpa_variations/strategy_ranks.json`
- Failure evidence: latest files in `output/rpa_screenshots/`
- Design spec: `docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md`
- RPA docs: `docs/agent10-fb-ads-rpa.md`
- Flow/selectors: `src/fb_ads_rpa/ads_manager_flow.py`, `src/fb_ads_rpa/meta_dom.py`, `config/ads_manager_selectors.yaml`

## Browser rule

Use Multilogin first for MLX profiles: prefer Multilogin Desktop / DirectCDP, or a refreshed automation token. Do not open Playwright Chromium directly against MLX Chrome 149+ profiles.

## Audit workflow

1. Read this skill and the paths above.
2. Diff the latest job error and `output/rpa_screenshots/` against the checklist.
3. Report the first broken step before proposing fixes.
4. Map that step to known `ui_variants`, strategies, recipes, code selectors, and rank entries.
5. Propose the smallest fix: selector, strategy order, recipe, or teach-mode approval.
6. Before editing `config/rpa_variations/steps.yaml` or `strategy_ranks.json`, show the intended diff and apply Hybrid C rules.
7. Keep the campaign Draft-only.

## Reusable task prompt (EN)

```text
You are debugging Agent 10 Facebook Ads Manager RPA in fb_agents.

Ground truth (Draft / hand-off, do NOT Publish unless I say so):
Create -> Manual -> Leads -> Continue -> Campaign name/budget ->
left tab Ad set -> Conversion location Website (NOT Instant Forms) ->
Conversion event Lead -> audience/targeting/placements ->
left tab Ad -> creatives/copy -> leave Draft.

UI navigation: left tabs = Campaign / Ad set / Ad AND section panels inside.
Upload FB-created campaigns are the live reference when available; until then use the checklist in docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md and ads_manager_flow v29.

Learning rules (hybrid C):
- Always log per-step strategy outcomes.
- Auto-promote SAFE strategies only if the step succeeded in DOM AND the run did not collapse before the next major left-tab / checklist step.
- RISKY steps (conversion location, conversion event): pending_approval only; promote after teach-mode / my explicit approve.
- Always leave campaign as Draft / hand-off - never click Publish.
- Never learn from Multilogin/browser launch crashes.

Tasks:
1) Diff the latest RPA failure (job error + output/rpa_screenshots) against the ground-truth checklist; name the first broken step.
2) List UI variants and strategies already known for that step (config/rpa_variations + code + recipes).
3) Propose the smallest fix (selector / strategy order / recipe / teach-mode).
4) If updating ranks or catalog, follow safe vs risky rules and show the diff before applying.
5) Do not open Playwright Chromium against MLX Chrome 149+ profiles; use MLX Desktop + DirectCDP or a refreshed automation token.

Read: docs/agent10-fb-ads-rpa.md, this design spec, src/fb_ads_rpa/ads_manager_flow.py, meta_dom.py, config/ads_manager_selectors.yaml.
```

## Reusable task prompt (RU short)

```text
Задебай Agent 10 RPA. Рецепт: Manual -> Leads -> Website -> Lead -> Draft (hand-off).
Левые табы: Campaign / Ad set / Ad + секции. Обучение: hybrid C (safe auto с гейтом; risky только teach-mode).
Найди первый сломанный шаг по скринам/ошибке, предложи минимальный фикс, не трогай ground truth без моего ОК.
Не используй Playwright Chromium на MLX-профиле 149+ - только MLX Desktop / DirectCDP / свежий token.
```
