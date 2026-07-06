# Agent 10 — Facebook Ads RPA Publish

Playwright automation of Ads Manager UI via antidetect browser (Multilogin or Dolphin).
No Marketing API / business token required — session cookies live in the browser profile.

## Architecture

```
Dashboard slots
  → slots_to_campaign_drafts()   # 1 campaign, 1 ad set, N ads
  → enrich_campaign_drafts()     # Upload FB + ad_account_map + creatives/
  → RpaPublisher
       → BrowserProvider (Multilogin | Dolphin)
       → AdsManagerFlow (Playwright)
```

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `FB_PUBLISH_BACKEND` | `rpa` | `rpa` = UI automation; `api` = legacy Graph API |
| `FB_BROWSER_PROVIDER` | `multilogin` | `multilogin` or `dolphin` |
| `DOLPHIN_API_BASE` | `http://127.0.0.1:3001` | Local Dolphin API |
| `DOLPHIN_API_TOKEN` | — | Dolphin local auth token |

## Files

- `src/fb_ads_rpa/draft.py` — CampaignDraft tree
- `src/fb_ads_rpa/build_draft.py` — slot grouping
- `src/fb_ads_rpa/enrich_draft.py` — Upload FB enrichment
- `src/fb_ads_rpa/browser.py` — Multilogin / Dolphin CDP
- `src/fb_ads_rpa/ads_manager_flow.py` — UI wizard steps
- `config/ads_manager_selectors.yaml` — selectors (update when Meta changes UI)
- `scripts/run_rpa_publish.py` — PoC CLI

## PoC

```bash
# Preview only (no browser)
python scripts/run_rpa_publish.py --account "EC Fav" --country MX --preview-only

# Dry-run in Ads Manager (stop before Publish)
python scripts/run_rpa_publish.py --account "EC Fav" --country MX --dry-run

# Full publish (PAUSED by default)
python scripts/run_rpa_publish.py --account "EC Fav" --country MX
```

Screenshots on failure: `output/rpa_screenshots/`.

## Dashboard

- **Preview** (`POST /preview-publish`) — shows CampaignDraft tree when `FB_PUBLISH_BACKEND=rpa`
- **Publish** (`POST /publish-jobs`) — background RPA job, poll `/publish-jobs/{id}`

## Next steps

- Harden selectors per current Ads Manager layout
- Parse campaign/ad IDs from URL after publish
- Dolphin as default for accounts mapped without Multilogin folder_id
