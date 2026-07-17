---
name: agent10-rpa-learn
description: >-
  Agent 10 RPA learning loop: map failures to variation catalog, try AI recovery
  on Multilogin CDP, log/promote strategies under safe vs risky rules. Use for
  /rpa-learn, teach_mode_website_waiting, conversion Website fails, strategy
  ranks, or "system should learn" RPA requests.
---

# Agent 10 — RPA learn

Slash: `/rpa-learn`  
Repo root: `fb_agents`

## Learning rules (hybrid)

| Step risk | On success | On fail |
|-----------|------------|---------|
| **Safe** | Auto-promote if DOM verified **and** run reaches next major checklist step | Demote strategy |
| **Risky** | `pending_approval` + evidence only | Log + screenshot; teach-mode OK |
| Browser/MLX crash | **No** UI learning | — |

**Risky today:** Conversion location = Website, Conversion event = Lead, Publish.  
**Safe:** Create, Manual, Leads, Continue, names, budget, geo/age fills, sidebar nav.

Never change ground-truth checklist without explicit user OK.

**Multilogin window:** never close/stop the MLX profile after a run (success, fail, or cancel). Leave it open so the user can confirm Facebook is logged in. CDP disconnect only — see `.cursor/rules/agent10-multilogin-keep-open.mdc`.

## Artifacts

| Path | Role |
|------|------|
| `config/rpa_variations/steps/*.yaml` | Per-step UI variants + strategies |
| `config/rpa_variations/strategy_ranks.json` | Ordered strategies + wins/fails/pending |
| `output/rpa_screenshots/` | Evidence |
| `output/rpa_variations/` | Optional per-run logs |

## Agent procedure

1. Diff latest failure vs checklist in the variations design spec (first broken step).
2. Open matching `config/rpa_variations/steps/<step_id>.yaml` + ranks entry.
3. Propose **one** next strategy (or AI recovery), show plan before applying.
4. If step is Conversion Website and MLX CDP is up:
   ```powershell
   Set-Location "C:\Users\EvgeniSedin\fb_agents"
   python scripts/rpa_ai_website_poc.py --cdp-port <PORT>
   ```
   Or `--discover-cdp` to find Multilogin `--remote-debugging-port`.
5. On success:
   - risky → append `pending_approval` in `strategy_ranks.json` (do not reorder defaults until user says approve);
   - safe → promote per hybrid gate.
6. Summarize: step, strategy tried, ok/fail, rank diff, screenshot paths.

## Do not

- Open Playwright Chromium on MLX profile folders.
- Auto-Publish.
- Keep clicking after wizard lost to Ads list — re-bind `/edit/` or Edit first.
- Expand scope past Campaign-open gate until that skill is green ×3 (unless user overrides).

## Related

- `.cursor/skills/agent10-campaign-open/SKILL.md`
- `docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md`
- `src/fb_ads_rpa/ai_website_recovery.py`
- `scripts/rpa_ai_website_poc.py`
