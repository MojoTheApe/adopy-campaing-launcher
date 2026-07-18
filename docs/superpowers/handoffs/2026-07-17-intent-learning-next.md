# Handoff: stop patching — start Intent→Verify→Heal→Promote learning

**Date:** 2026-07-17 (evening)  
**Repo:** `C:\Users\EvgeniSedin\fb_agents`  
**Branch:** `feat/morning-web-ui`  
**Read first:** `docs/superpowers/specs/2026-07-17-rpa-intent-learning-methodology.md`

---

## User feeling / mission

User correctly observes: we keep hot-fixing Conversion/Website/banner but **do not learn**, and stability is worse — e.g. **campaign name not filled**.  
**Next window goal:** install a real learning loop for **where to click/fill**, starting at the **first broken verify**, not at Website.

---

## Current ground truth (last runs)

Campaign: `PE_AIT_All_Video_Day_35_17.07`

Typical step trail:

```
open_campaigns → create → manual fail/ok → Leads → Continue → wizard_tab_switch:ok
→ s03/s04 ok → s05 name:ok (sometimes) → s06 budget:fail×2
→ wizard_guard:ok (left to click-attribution after Next, then “recovered”)
→ s07 Next:ok (often false — landed on news)
→ s08/s09 adset fail → adset_level_restore:ok (often false positive on Ads list)
→ conversion_location:fail
```

**Smoking guns (screenshots):**

| Shot | Meaning |
|------|---------|
| `*_player_left_after_s07_campaign.png` | After Next → Meta news *Simplifying Ad Measurement…* (`/business/news/click-attribution`) |
| `*_player_guard_recover.png` | Back on **Ads manage list**, not edit wizard |
| `*_player_conversion_label_missing.png` | Account switcher open; no Conversion location |
| `*_player_campaign.png` | Measurement banner + About this update still in UI |

Root causes (ordered):

1. **CDP click miss** on Next → hits banner “About this update”.
2. **Recover** returns to manage list without verified wizard chrome.
3. **No verify** that Campaign name / Daily budget / Ad set name values stuck.
4. **Learning artifacts not wired** — ranks/catalog don’t drive recipe_player.
5. Scope too wide (Website) while earlier steps are red.

---

## Already in repo (do not reinvent)

| Piece | Path | Note |
|-------|------|------|
| Forbidden zones + recover | `config/rpa_forbidden_zones.yaml`, `wizard_guard.py` | Phase A; recover must require wizard |
| Recipe player | `recipe_player.py` | Partial footer-nav CDP work may be mid-edit — check git diff |
| Variations catalog | `config/rpa_variations/steps/*.yaml` | Underused |
| Strategy ranks | `config/rpa_variations/strategy_ranks.json` | Underused |
| Website AI PoC | `ai_website_recovery.py`, `scripts/rpa_ai_website_poc.py` | Vision heal — generalize |
| Click recorder | `click_recorder.py` | Prefer intent+verify over coords |
| Skills | `agent10-campaign-open`, `agent10-rpa-learn` | Follow |
| Multilogin keep-open | `.cursor/rules/agent10-multilogin-keep-open.mdc` | Hard rule |
| Serve defaults | `serve.py` → recipe player, stop `s12_conversion_location`, flow **v36** |

---

## Methodology decision (research)

**Adopt IVHP:** Intent → Verify → Heal → Promote on **Multilogin CDP**.

- **Do use:** semantic locators, ARIA-snapshot LLM heal, evidence packs, existing vision PoC as fallback, optional Stagehand later **on same CDP**.
- **Do not use as primary:** Browserbase/Anchor cloud browsers, pure CUA, more stale x,y in golden YAML.

Full write-up: `docs/superpowers/specs/2026-07-17-rpa-intent-learning-methodology.md`

---

## Progress (2026-07-18)

- [x] `src/fb_ads_rpa/intent_runner.py` + `config/rpa_intents/campaign_name.yaml`
- [x] Tests: `tests/test_intent_runner.py` (7 passed)
- [x] `recipe_player` `s05_campaign` → IVHP; hard-stop on verify fail; evidence under `output/rpa_evidence/`
- [ ] **3 green** live campaign-name verifies on Multilogin
- [ ] Then budget intent → Next → Website

## First work remaining

1. Restart `serve.py` (load new code). Recipe player ON.
2. Publish `PE_AIT_…` (or same PE). **Do not chase Website.**
3. If name verify fails: open newest `output/rpa_evidence/<run>/campaign_name/` (screenshot + aria).
4. Heal from evidence (one strategy) → re-run until name green ×3.
5. Only then add `daily_budget` intent the same way.

### Out of scope next window

- Full targeting/creatives/Publish.
- Switching away from Multilogin.
- Browserbase as runtime for FB accounts.

---

## Suggested first user message

```
Продолжаем по docs/superpowers/handoffs/2026-07-17-intent-learning-next.md
и docs/superpowers/specs/2026-07-17-rpa-intent-learning-methodology.md.
Не чиним Website. Строим Intent→Verify→Heal→Promote начиная с заполнения Campaign name.
Multilogin не закрывать.
```

---

## Checklist for next agent

- [ ] Methodology spec read  
- [ ] No Website work until name+budget+Next verify green  
- [ ] Evidence packs on fail  
- [ ] Multilogin window stays open  
- [ ] Handoff updated if scope changes  
