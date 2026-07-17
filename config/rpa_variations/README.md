# RPA variation catalog + ranked strategies

Learning store for Agent 10 Ads Manager RPA (see design spec
`docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md`).

## Layout

```
config/rpa_variations/
  README.md
  strategy_ranks.json          # ordered strategies + wins/fails/pending_approval
  steps/
    open_campaigns.yaml
    create_leads_continue.yaml
    campaign_name_budget.yaml
    conversion_location_website.yaml
```

## Risk rules (short)

- **Safe** success → may auto-promote if DOM ok and next major step reached.
- **Risky** success → `pending_approval` only until human / teach-mode approve.
- Never learn from Multilogin/browser launch crashes.

## Tools

- Skill: `.cursor/skills/agent10-rpa-learn/SKILL.md` (`/rpa-learn`)
- Narrow open gate: `.cursor/skills/agent10-campaign-open/SKILL.md` (`/rpa-campaign-open`)
- Website AI PoC: `python scripts/rpa_ai_website_poc.py --discover-cdp`
