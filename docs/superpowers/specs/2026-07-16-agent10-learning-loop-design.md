# Agent 10 — Learning loop (skills + AI Website PoC)

**Date:** 2026-07-16  
**Status:** Implemented scaffold (skills + catalog + CDP PoC)  
**Extends:** `2026-07-15-agent10-rpa-variations-design.md`

## Delivered

1. **Skills**
   - `.cursor/skills/agent10-campaign-open/SKILL.md` + `/rpa-campaign-open`
   - `.cursor/skills/agent10-rpa-learn/SKILL.md` + `/rpa-learn`
2. **Variation catalog**
   - `config/rpa_variations/steps/*.yaml`
   - `config/rpa_variations/strategy_ranks.json`
3. **Website AI PoC (Multilogin CDP, not Browserbase)**
   - `src/fb_ads_rpa/ai_website_recovery.py`
   - `src/fb_ads_rpa/variation_store.py`
   - `scripts/rpa_ai_website_poc.py`

## Runtime contract

```
Dashboard/RPA opens MLX with --remote-debugging-port
  → deterministic meta_dom Website select
  → if fail + API key: LLM vision loop (click_text / Space / scroll)
  → log risky success as pending_approval in strategy_ranks.json
```

## Next (not in this scaffold)

- Hook PoC into `ads_manager_flow._select_conversion_location_website` teach-mode path
- Stagehand Node sidecar optional cache
- Approve CLI: `python scripts/rpa_approve_strategy.py --step conversion_location_website`
