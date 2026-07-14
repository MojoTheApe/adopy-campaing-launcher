# Agent 10 — RPA Variations Learning Machine — Design Spec

**Date:** 2026-07-15  
**Status:** Draft (awaiting review)  
**Owner:** Evgeni Sedin  
**Approach:** Catalog + ranked strategy store (hybrid learning)  
**Related:** `docs/agent10-fb-ads-rpa.md`, `src/fb_ads_rpa/` (flow v29)

---

## Goal

Build a **ground-truth campaign recipe**, a **UI variation catalog** for Meta Ads Manager left-tab navigation, a **learning machine** that ranks click/fill strategies, an **Agent 10 Cursor skill** (auto + slash), and a **reusable task prompt** for future RPA debug sessions.

Target recipe (hand-off / draft, not Publish):

```
Create → Manual setup → Objective Leads → Continue
  → Campaign (name, budget)
  → Ad set: Conversion location Website (not Instant Forms) → Conversion event Lead
  → Audience / targeting / placements
  → Ad creatives + copy
  → Leave as Draft (hand-off)
```

**Reference UI:** left tabs = Campaign / Ad set / Ad **and** section panels inside those levels. Correctly built **Upload FB** campaigns are the eventual live golden reference (phase later). Phase 1 builds the checklist from Agent 10 code first.

---

## Context (existing)

| Piece | Role today |
|-------|------------|
| `ads_manager_flow.py` (v29) | Wizard orchestration |
| `meta_dom.py` | Conversion / targeting / sidebar helpers |
| `config/ads_manager_selectors.yaml` | Alternate EN/ES selectors |
| `config/rpa_recipes/`, teach-mode, website click lab | Human/recipe adaptation for Website |
| `output/rpa_screenshots/` | Failure evidence |
| Multilogin providers in `browser.py` | Browser session (API → DirectCDP → PlaywrightProfile) |

Jenya / Agent 1 analytics are **out of scope**. Agent 9 Marketing API is **out of scope** for this learner (RPA path only).

---

## Architecture

```
Dashboard slots → draft/enrich (Upload FB)
  → RpaPublisher + Multilogin
  → AdsManagerFlow
       reads strategy order from ranked store
       runs step strategies
       logs step outcomes + screenshots
  → Learner
       safe: auto-promote when hybrid rules pass
       risky: pending_approval until teach-mode / human approve
  → Agent 10 skill /slash uses catalog + ranks + screenshots to debug
```

Four deliverables:

1. **Ground-truth checklist** — ordered steps, left tab, section, expected DOM, screenshot label  
2. **Variation catalog** — per-step UI variants + strategy list (`config/rpa_variations/`)  
3. **Ranked strategy store** — wins/fails/order + pending_approval  
4. **Skill + slash + task prompt** — how the agent runs audits and updates learning artifacts  

---

## Ground-truth checklist (phase 1)

| # | Left tab | Section / action | Must result | Shot label (existing style) |
|---|----------|------------------|-------------|------------------------------|
| 1 | — | Open campaigns `act=` | Ads Manager ready, Create visible | `01_campaigns` |
| 2 | — | Create → Manual (not AI-guided) | Manual / objectives chooser | `02_*` |
| 3 | — | Objective Leads → Continue | Campaign name field | `03_wizard_started` |
| 4 | Campaign | Name + daily budget | Fields filled | `04_campaign_level` |
| 5 | Ad set | Navigate to Ad set | Ad set level visible | `04b_*` |
| 6 | Ad set | Conversion location = Website | Website selected (not Instant Forms / hybrid) | `04d_*` |
| 7 | Ad set | Conversion event = Lead | Lead selected | `04e_*` |
| 8 | Ad set | Name, geo, age, targeting, placements | Audience filled; targeting skipped only if conversion incomplete | `05_*` |
| 9 | Ad | Creative + copy | Ads created | (ad shots) |
| 10 | — | Finish | Draft / hand-off; Publish **not** clicked unless explicitly approved | `99_draft` |

Later: open a live Upload FB–created campaign in MLX and diff this table against left tabs / section panels; record gaps in the catalog (do not silently change ground truth without human OK).

---

## Variation catalog

**Location:** `config/rpa_variations/steps/<step_id>.yaml` (or one YAML with `steps:` map).

Per step:

```yaml
step_id: conversion_location_website
left_tab: ad_set
section: conversion
risk: risky   # safe | risky
must:
  - conversion_location == Website
ui_variants:
  - id: hybrid_default
    notes: "Website and instant forms selected by default"
  - id: en_vs_es
    notes: "Website / Sitio web labels"
strategies:
  - id: recipe_space
  - id: keyboard_space_enter
  - id: cdp_mouse
  - id: playwright_click
evidence:
  - output/rpa_screenshots/...
```

**Phase 1:** thin coverage for **all** checklist steps (seed from current code strategies).  
**Phase 2:** deepen conversion location / event strategies and evidence.

---

## Learning machine (ranked store)

**Store:** e.g. `config/rpa_variations/strategy_ranks.json`  
Optional later: per-account overrides under `output/rpa_variations/`.

### Hybrid learning rule (approved)

| Event | Behavior |
|-------|----------|
| Any step outcome | Always **log** (strategy id, ok/fail, timestamp, screenshot path) |
| **Safe** step success | **Auto-promote** only if hybrid gate passes: step verified in DOM **and** run does not collapse before the next left-tab transition (or within N following micro-steps — N configurable, default: reach next checklist major step) |
| **Safe** step fail | Increment fails; demote in order |
| **Risky** step success | Write `pending_approval` + evidence; **do not** change default order until teach-mode / human approve |
| **Risky** step fail | Log + screenshot; keep teach-mode path |
| Full campaign stuck after a logged success | Safe promote still allowed only if hybrid gate passed; otherwise log-only |
| Browser/Multilogin crash | **No** UI strategy learning |

### Risk classification (initial)

- **Safe:** Create click, Manual dismiss, Leads select, Continue, campaign/ad set name fill, budget fill, geo/age, copy fields, sidebar/breadcrumb navigation clicks  
- **Risky:** Conversion location Website, conversion event Lead, Publish  

### Integration with existing tools

- Recipes = strategies with id `recipe_*`  
- Teach-mode = approval UI for risky promote  
- Website click lab = exploration; winners enter ranks only after approve if risky  
- Selectors YAML remains alternate locators; ranks order **which strategy family** to try first  

### Non-goals (phase 1)

- Vision / screenshot embedding compare  
- Auto-Publish  
- Learning from PlaywrightProfile downgrade / browser launch failures  

---

## Agent 10 skill + slash

**Skill path:** `.cursor/skills/agent10-rpa-variations/SKILL.md`  
**Slash:** `.cursor/commands/rpa-audit.md` (name TBD: `/rpa-audit`)

**Invocation:**

- **Auto** when the chat concerns RPA publish, Ads Manager selectors, teach-mode, Multilogin browser open for publish, conversion Website, `rpa_screenshots`, variation catalog  
- **Explicit** `/rpa-audit` for a full debug pass against checklist + latest job/screenshots  

**Skill must instruct the agent to:**

1. Prefer Multilogin Desktop / DirectCDP over Playwright Chromium on MLX profiles  
2. Walk the ground-truth checklist; find first failing step  
3. Map failure to `ui_variants` + strategy ranks  
4. Propose fix (selector, strategy order, recipe, teach-mode)  
5. Update catalog/ranks only within safe/risky rules; never change ground truth without user confirmation  

---

## Reusable task prompt

Copy-paste for a new Cursor session (also embed in the skill):

```text
You are debugging Agent 10 Facebook Ads Manager RPA in fb_agents.

Ground truth (Draft / hand-off, do NOT Publish unless I say so):
Create → Manual → Leads → Continue → Campaign name/budget →
left tab Ad set → Conversion location Website (NOT Instant Forms) →
Conversion event Lead → audience/targeting/placements →
left tab Ad → creatives/copy → leave Draft.

UI navigation: left tabs = Campaign / Ad set / Ad AND section panels inside.
Upload FB–created campaigns are the live reference when available; until then use the checklist in docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md and ads_manager_flow v29.

Learning rules (hybrid C):
- Always log per-step strategy outcomes.
- Auto-promote SAFE strategies only if the step succeeded in DOM AND the run did not collapse before the next major left-tab / checklist step.
- RISKY steps (conversion location, conversion event, Publish): pending_approval only; promote after teach-mode / my explicit approve.
- Never learn from Multilogin/browser launch crashes.

Tasks:
1) Diff the latest RPA failure (job error + output/rpa_screenshots) against the ground-truth checklist; name the first broken step.
2) List UI variants and strategies already known for that step (config/rpa_variations + code + recipes).
3) Propose the smallest fix (selector / strategy order / recipe / teach-mode).
4) If updating ranks or catalog, follow safe vs risky rules and show the diff before applying.
5) Do not open Playwright Chromium against MLX Chrome 149+ profiles; use MLX Desktop + DirectCDP or a refreshed automation token.

Read: docs/agent10-fb-ads-rpa.md, this design spec, src/fb_ads_rpa/ads_manager_flow.py, meta_dom.py, config/ads_manager_selectors.yaml.
```

Russian short version for chat kickoff:

```text
Задебай Agent 10 RPA. Рецепт: Manual → Leads → Website → Lead → Draft (hand-off).
Левые табы: Campaign / Ad set / Ad + секции. Обучение: hybrid C (safe auto с гейтом; risky только teach-mode).
Найди первый сломанный шаг по скринам/ошибке, предложи минимальный фикс, не трогай ground truth без моего ОК.
Не используй Playwright Chromium на MLX-профиле 149+ — только MLX Desktop / DirectCDP / свежий token.
```

---

## Rollout plan

1. Write checklist + thin variation catalog seeded from v29 code  
2. Add Agent 10 skill + `/rpa-audit` + embed task prompt  
3. Implement strategy_ranks store + hybrid promote hooks in flow  
4. Unit tests: parse ranks, safe promote gate, risky pending_approval  
5. Smoke: dry-run / hand-off RPA on one account  
6. Later: live Upload FB golden campaign in MLX → catalog gaps; phase 2 deepen conversion  

---

## Testing

| Test | Expectation |
|------|-------------|
| Unit: hybrid gate | Success without next-step progress → log only, no safe promote |
| Unit: risky success | Lands in `pending_approval`, order unchanged |
| Unit: teach approve | Risky strategy moves to top |
| Manual smoke | Hand-off draft path; ranks file updates only per rules |
| Regression | Existing selector/recipe paths still run if ranks missing (fallback to code order) |

---

## Open points (resolved in brainstorm)

| Topic | Decision |
|-------|----------|
| Scope | Full stack (catalog + learner + skill + prompt) |
| Recipe | Leads → Website → Lead → Draft |
| Tabs | Left tabs; both level + section panels |
| Golden | Code checklist first; live Upload FB later |
| Learning | Hybrid C |
| Skill invoke | Auto on RPA topics + slash full audit |
| Catalog phase 1 | Full path thin; deepen conversion later |
| Approach | #2 ranked strategy store |

---

## Spec self-review

- No TBD placeholders for core behavior; N for “micro-steps after success” left as configurable with default = next major checklist step  
- Consistent with hybrid C and Draft-not-Publish  
- Scope phase 1 vs 2 explicit  
- Ambiguity on “next left-tab” resolved as: next major checklist step that changes left-tab or finishes Draft  
