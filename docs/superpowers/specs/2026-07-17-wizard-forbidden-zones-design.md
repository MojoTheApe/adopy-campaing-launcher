# Agent 10 — Wizard forbidden zones + recover (phase A)

**Date:** 2026-07-17  
**Status:** Implemented (recipe player)  
**Related:** handoff `docs/superpowers/handoffs/2026-07-17-train-campaign-open.md`

## Problem

Recipe player sometimes leaves campaign create (e.g. clicks **About this update** on the measurement banner) and lands on `facebook.com/business/news/click-attribution`. Then `Ad set wizard not found after Next`.

## Decision

- **Phase A (now):** deny-list + URL watchdog + recover inside `recipe_player` only.
- **Phase B (later):** same guard for `ads_manager_flow` / AI recovery.
- **Recovery:** `history.back` / close news tab / reopen draft — not hard-stop-only.
- **Catalog:** `config/rpa_forbidden_zones.yaml` + short cheat sheet in `agent10-campaign-open` skill. User screenshots → append zones.

## Components

| Path | Role |
|------|------|
| `config/rpa_forbidden_zones.yaml` | bad URLs + forbidden click texts |
| `src/fb_ads_rpa/wizard_guard.py` | match/URL helpers, banner dismiss (X only), recover |
| `src/fb_ads_rpa/recipe_player.py` | pre-click block, post-click URL recover, after Next |
| `.cursor/skills/agent10-campaign-open/SKILL.md` | human/agent cheat sheet |

## Runtime

1. `_guards`: dismiss measurement banner via **X** (never About this update); if bad URL → recover.
2. `_generic_click`: block forbidden `match` / locator text; after click if bad URL → recover (max 2).
3. After campaign **Next**: recover then require ad set wizard.

## Out of scope (phase A)

- Full allow-list of every control
- Global click interceptor for non-recipe paths
- Human-like mouse jitter
