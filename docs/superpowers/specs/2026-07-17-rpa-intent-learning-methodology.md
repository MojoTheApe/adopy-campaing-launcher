# Agent 10 — Intent learning methodology (where to click)

**Date:** 2026-07-17  
**Status:** Proposed (research-backed) — start next window  
**Replaces reactive patch loop** for campaign-open training

---

## Why we feel stuck

Recent runs show the same pattern:

1. Hotfix one symptom (wizard tab, banner, recover).
2. Next run fails earlier or elsewhere (campaign name empty, Next → news, Conversion without wizard).
3. `strategy_ranks.json` / variation YAMLs exist but **do not drive** most recipe clicks.
4. Golden recipe still replays brittle `own_text` + stale coords; Multilogin CDP often **misses** the target and hits the measurement banner (`About this update` → `business/news/click-attribution`).
5. Learning is not closed: fail → screenshot → human chat patch → no promoted intent.

**Verdict:** more one-off fixes without a verify/promote loop will not stabilize Meta Ads Manager.

---

## Research summary (2026 landscape)

| Approach | What it is | Fit for us (Multilogin + FB login) |
|----------|------------|-------------------------------------|
| **Hardcoded Playwright selectors** | CSS/`#js_*` / recorded x,y | Poor alone — Meta DOM churn + CDP mouse drop (#41286) |
| **Semantic locators** (`getByRole` / `getByLabel`) | Playwright best practice | **Primary** for safe steps |
| **Self-heal via ARIA snapshot + LLM** | On fail: `ariaSnapshot` → LLM proposes new role/name locator → retry → pending_approval | **Best learning layer** on our CDP |
| **Stagehand (act/observe)** | NL → element at runtime; optional Browserbase | **Useful as sidecar on MLX CDP**; Browserbase cloud **not** for FB account cookies |
| **browser-use / CUA (Computer Use)** | Vision agent clicks pixels | Fallback only — cost/noise; OK on MLX CDP if DOM heal fails |
| **Pure Browserbase / Anchor cloud** | Managed browsers | **Reject for Publish path** — wrong profile, login, Multilogin rule |
| **Human teach / click recorder** | Watch human, save recipe | Keep — but save **intent + verify**, not raw x,y |

Industry consensus: **DOM/ARIA-first + promote verified strategies**; vision/CUA only when DOM fails. Session must stay on **already-logged Multilogin** via CDP.

---

## Recommended methodology: IVHP

**Intent → Verify → Heal → Promote**

```
For each checklist step:
  1. INTENT   — natural language + semantic matchers (role/label/exact text)
  2. ACT      — try ranked strategies (deterministic first)
  3. VERIFY   — DOM assertion (field value / label present / URL still Ads Manager)
  4. on fail  — HEAL: aria snapshot (+ optional screenshot) → LLM suggests strategy
              — retry once; log evidence pack
  5. PROMOTE  — safe: auto if verify OK AND next major step reached
              — risky: pending_approval only
```

### Hard rules (unchanged)

- Multilogin CDP only; never close MLX window.
- Never Publish in Meta.
- Forbidden zones YAML (`About this update`, help links, wizard Close).
- Gate stop: `s12_conversion_location` until campaign-open is green ×3.

### What we stop doing

- Patching random symptoms without a verify predicate.
- Promoting strategies that only “clicked something”.
- Relying on recorded `x,y` for Next / Continue / Create.
- Expanding to Website while Campaign name fill is red.

---

## Architecture to build (next)

```
config/rpa_intents/<step_id>.yaml
  intent: "Fill Campaign name with {{campaign_name}}"
  verify:
    type: input_value_near_label
    label: Campaign name
    equals: "{{campaign_name}}"
  strategies: [get_by_label, fill_near_label, aria_heal, vision_heal]

src/fb_ads_rpa/intent_runner.py
  run_step(page, intent) -> Outcome(ok, strategy, evidence)

Evidence pack per fail (output/rpa_evidence/<run>/<step>/):
  screenshot.png
  aria_snapshot.md
  url.txt
  strategies_tried.json

Heal backends (order):
  1. Deterministic semantic (existing meta_dom / recipe match)
  2. ARIA heal (LLM + page.accessibility / aria snapshot)  ← NEW core
  3. Existing ai_website_recovery vision loop (generalize beyond Website)
  4. Optional Stagehand observe/act over same CDP (Node sidecar later)
  5. Human teach-mode / click recorder → new strategy draft
```

Wire **recipe_player** to call `intent_runner` for safe steps first:  
`s05_campaign` (name), `s06_campaign` (budget), `s07` Next, then ad set name, then Website.

---

## Learning session protocol (human + agent)

1. One campaign only (`PE_AIT_…` or similar).
2. Dashboard Publish with recipe player ON.
3. **Stop at first red verify** — do not continue to Conversion.
4. Agent writes evidence pack + one heal candidate.
5. Re-run **same step** until verify green ×3, then unlock next checklist row.
6. User may send screenshots → append forbidden zones / intents (not free-form code thrash).

Optional: dashboard **Teach** — human performs one step; recorder stores intent + post-state verify, not coords.

---

## Third-party / servers — decisions

| Tool | Decision |
|------|----------|
| Browserbase | No for Agent 10 Publish (cookies/MLX) |
| Stagehand | Optional later, attach to **MLX CDP** |
| OpenAI/Anthropic vision API | Yes for heal layer (already PoC for Website) |
| Anchor / cloud CUA | No as primary |
| browser-use | Optional experiment on MLX CDP after ARIA heal |

---

## Success metrics

- Campaign name verify green **3/3** consecutive runs.
- No navigation to `/business/news/` after Next (or recover → wizard verified).
- Each fail produces an evidence pack (not only a chat message).
- `strategy_ranks.json` updates from real verifies, not from “click returned True”.

---

## Related

- Handoff: `docs/superpowers/handoffs/2026-07-17-intent-learning-next.md`
- Prior: `2026-07-15-agent10-rpa-variations-design.md`, `2026-07-16-agent10-learning-loop-design.md`
- Forbidden zones: `2026-07-17-wizard-forbidden-zones-design.md`
