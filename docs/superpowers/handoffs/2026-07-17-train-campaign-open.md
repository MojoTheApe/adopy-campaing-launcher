# Handoff: train bot to open Meta campaign correctly

**Date:** 2026-07-17  
**Repo:** `C:\Users\EvgeniSedin\fb_agents`  
**Branch:** `feat/morning-web-ui`  
**Superseded for learning approach:** use  
`docs/superpowers/handoffs/2026-07-17-intent-learning-next.md`  
+ `docs/superpowers/specs/2026-07-17-rpa-intent-learning-methodology.md`  
(Intent→Verify→Heal→Promote; stop patch-only Website loop).

**Main goal (legacy):** обучать / стабилизировать RPA так, чтобы бот **правильно открывал кампанию** до gate ниже (не полный publish).

---

## Mission (next window)

Stabilize this path end-to-end (green runs, then promote strategies):

```
Ads Manager (correct act=)
  → Create
  → Manual campaign setup (if AI vs Manual shown)
  → Objective: Leads
  → Continue
  → Wizard open (Campaign name visible /edit or side panel)
  → Campaign name + Daily budget
  → Next → Ad set name
  → Conversion location dropdown
  → Single
  → Website   ← EXACT one word only
```

**Never select:** `Website and instant forms` / `Website & Instant forms`.  
**Do not:** Conversion event, targeting, creatives, Meta Publish.  
**Multilogin window:** never close after run (user verifies FB login). See `.cursor/rules/agent10-multilogin-keep-open.mdc`.

---

## How to start the next chat

1. Open this file or paste its path.
2. Run slash **`/rpa-campaign-open`** (skill) and/or **`/rpa-learn`** when a step fails.
3. Dashboard: `python serve.py` → http://127.0.0.1:9090/  
   - Recipe player ON  
   - `FB_RPA_RECIPE_STOP_AFTER=s12_conversion_location` (default in `serve.py`)  
   - Flow version should print **v36+**

---

## Skills / commands (read first)

| Path | Use |
|------|-----|
| `.cursor/skills/agent10-campaign-open/SKILL.md` | Gate checklist + procedure |
| `.cursor/skills/agent10-rpa-learn/SKILL.md` | Variation catalog, ranks, teach-mode, AI recovery |
| `.cursor/commands/rpa-campaign-open.md` | Slash entry |
| `.cursor/commands/rpa-learn.md` | Slash entry |
| `.cursor/rules/agent10-multilogin-keep-open.mdc` | Never stop_profile / kill Mimic |

---

## Key code / config

| Path | Role |
|------|------|
| `config/rpa_recipes/campaign_golden.yaml` | Golden steps; stop at `s12_conversion_location`; includes optional `s00b_manual` |
| `src/fb_ads_rpa/recipe_player.py` | Recipe runner; wizard detect/recover after Continue; ad set restore after rename |
| `src/fb_ads_rpa/meta_dom.py` | Conversion location → Single → Website (exact) |
| `src/fb_ads_rpa/ads_manager_flow.py` | Fallback flow; `RPA_FLOW_VERSION` |
| `config/rpa_variations/steps/*.yaml` | UI variants + strategies |
| `config/rpa_variations/strategy_ranks.json` | Wins/fails/pending_approval |
| `config/ad_account_map.csv` | `ad_account_id` + `profile_id` (required for Publish) |
| `output/rpa_screenshots/` | Evidence after each run |

---

## Current status (as of handoff)

### Working / done recently
- Dashboard Refresh fast (reuse Excel DF; ~7s GSheet refresh).
- Multilogin stays open after Publish/cancel (CDP disconnect only).
- Gate expanded past name+budget to **Ad set name + Website exact**.
- v36: after Continue — longer wait, Manual→Leads→Continue recover, reopen draft from list; better wizard DOM detection (not only `/edit/standalone`).

### Last known failure (fix first in next session)
- **Campaign:** `PE_AIT_All_Video_Day_35_17.07`
- **Steps:** `s00_create:ok → s01_objective:ok → s02_objective_continue:ok → wizard_tab_switch:fail`
- **Error:** Campaign wizard did not open after Continue  
- **Checklist row:** #4 (wizard open) — do **not** jump to Website until this is green.
- v36 recover was added **after** that fail; next run must verify whether recover works. If still fail: inspect newest `output/rpa_screenshots/*wizard*` and whether Manual was required.

### Learning rules (hybrid)
- **Safe** (Create, Manual, Leads, Continue, names, budget, nav): auto-promote only if DOM verified **and** next major checklist step reached.
- **Risky** (Conversion Website, later Event/Publish): `pending_approval` + evidence; no silent reorder until user approves.
- Browser/MLX crash → no UI learning.

---

## Agent procedure (next window)

1. Confirm `serve.py` up, logs show Recipe player + current `RPA_FLOW_VERSION`.
2. User Publish → read `steps_completed` + newest screenshots.
3. Name **first** broken checklist row; smallest fix only.
4. Map fail → `config/rpa_variations/steps/<step>.yaml` + update `strategy_ranks.json`.
5. Re-run same campaign until gate green; aim **3 consecutive green** before expanding past Website.
6. Never Playwright Chromium against MLX profile folders; Multilogin CDP only.
7. Never close Multilogin window as cleanup.

---

## Out of scope (for this training phase)

- Full Ad set targeting / placements / creatives / Publish in Meta.
- Switching to `playwright-stealth` / vanilla Chromium (worse than Multilogin).
- “Human click” polish (hover/jitter) — optional later; does **not** teach where to click.

---

## Suggested first user message in next chat

```
Продолжаем обучение RPA: открытие кампании до Ad set name + Conversion location → Single → Website.
Читай docs/superpowers/handoffs/2026-07-17-train-campaign-open.md и .cursor/skills/agent10-campaign-open/SKILL.md.
Последний фейл был wizard_tab_switch после Continue на PE_AIT_All_Video_Day_35_17.07 — проверь v36 recover и доведи gate до зелёного.
```
