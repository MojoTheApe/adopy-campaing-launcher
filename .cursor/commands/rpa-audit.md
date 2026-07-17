---
description: Agent 10 RPA audit — debug latest Ads Manager failure using checklist, screenshots, ranks, and Hybrid C rules
---

Ты — шаг **`/rpa-audit`** для Agent 10 Facebook Ads Manager RPA.

## Read first

1. `.cursor/skills/agent10-rpa-variations/SKILL.md`
2. `docs/agent10-fb-ads-rpa.md`
3. `docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md`
4. `config/rpa_variations/checklist.md`
5. `config/rpa_variations/steps.yaml`
6. `config/rpa_variations/strategy_ranks.json`
7. Latest available job error / traceback and latest files in `output/rpa_screenshots/`
8. When needed: `src/fb_ads_rpa/ads_manager_flow.py`, `src/fb_ads_rpa/meta_dom.py`, `config/ads_manager_selectors.yaml`

## Audit checklist

- Keep the ground truth **Draft-only**. Never click **Publish**, never propose Publish as a ranked strategy, and never edit toward full publish.
- Prefer Multilogin Desktop / DirectCDP, or a refreshed automation token. Do not open Playwright Chromium directly against MLX Chrome 149+ profiles.
- Walk the left-tab recipe in order: Create / Manual / Leads / Continue -> **Campaign** -> **Ad set** -> **Ad** -> Draft.
- Include section panels inside the Campaign / Ad set / Ad levels when naming the failure.
- Diff latest screenshots and job errors against `config/rpa_variations/checklist.md`; report the **first broken step** before any fix.
- For that step, list known UI variants and strategies from `config/rpa_variations/steps.yaml`, `strategy_ranks.json`, selectors, recipes, and code.
- Apply Hybrid C:
  - safe steps can auto-promote only after DOM success and progress to the next major checklist step;
  - risky conversion steps (`conversion_location_website`, `conversion_event_lead`) go to `pending_approval` until teach-mode / explicit approval;
  - Multilogin/browser launch crashes do not update UI strategy learning.

## Response format

1. **First broken step:** step id, left tab, section, expected result, observed evidence.
2. **Evidence:** screenshot paths, job error lines, relevant selectors/recipes/ranks.
3. **Known variants/strategies:** from catalog + code.
4. **Minimal fix:** selector / strategy order / recipe / teach-mode path.
5. **Learning safety:** safe vs risky classification and whether ranks/catalog may change.

Before editing `config/rpa_variations/steps.yaml`, `config/rpa_variations/strategy_ranks.json`, or recipes, show the intended diff and wait for confirmation unless the user explicitly asked you to apply it now.
