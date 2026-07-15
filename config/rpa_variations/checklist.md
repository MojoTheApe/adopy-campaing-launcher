# RPA Variations — Ground-truth checklist (Draft-only)

**Recipe:** Create → Manual setup → Objective Leads → Continue → Campaign (name, budget) → Ad set: Conversion location Website → Conversion event Lead → Audience / targeting / placements → Ad creatives + copy → Leave as Draft (hand-off).

**Note:** Publish is out of scope. RPA must always leave the campaign as Draft; do not click Publish or rank Publish strategies.

| # | Step ID | Left tab | Section / action | Must result | Shot label |
|---|---------|----------|------------------|-------------|------------|
| 1 | `open_campaigns` | — | Open campaigns `act=` | Ads Manager ready, Create visible | `01_campaigns` |
| 2 | `create_manual` | — | Create → Manual (not AI-guided) | Manual / objectives chooser | `02_*` |
| 3 | `objective_leads` | — | Objective Leads → Continue | Campaign name field | `03_wizard_started` |
| 4 | `campaign_name_budget` | Campaign | Name + daily budget | Fields filled | `04_campaign_level` |
| 5 | `nav_ad_set` | Ad set | Navigate to Ad set | Ad set level visible | `04b_*` |
| 6 | `conversion_location_website` | Ad set | Conversion location = Website | Website selected (not Instant Forms / hybrid) | `04d_*` |
| 7 | `conversion_event_lead` | Ad set | Conversion event = Lead | Lead selected | `04e_*` |
| 8 | `audience_targeting` | Ad set | Name, geo, age, targeting, placements | Audience filled; targeting skipped only if conversion incomplete | `05_*` |
| 9 | `ad_creative_copy` | Ad | Creative + copy | Ads created | `ad_*` |
| 10 | `finish_draft` | — | Finish | Draft / hand-off; Publish **not** clicked | `99_draft` |
