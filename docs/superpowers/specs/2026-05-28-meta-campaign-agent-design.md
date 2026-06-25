# Agent 9: Excel → Meta Marketing API — Design Spec

**Date:** 2026-05-28  
**Status:** Draft (awaiting review)  
**Owner:** Evgeni Sedin  
**Approach:** Hybrid — publish agent in `fb_agents`; `media-buyer-local` for costs, allowlist, Write approval (UI later)

---

## Goal

Publish Facebook campaigns via **Marketing API** using the existing **Agent 8 Excel format** as the source of truth. FB no longer accepts bulk Excel upload; this agent replaces manual upload while preserving the same column semantics from `Upluad FB.xlsx` / `build_upload_excel.py` output.

Creative files live on disk under one root folder; the agent finds files recursively by filename and uploads via `adimages` / `advideos`.

Multilogin is **out of scope** for this pipeline (browser profiles only for manual edge cases).

---

## Context (existing code)

| Component | Role |
|-----------|------|
| **Agent 1** (`run_morning.py`, `run.py`) | Morning analysis, combo recommendations |
| **Agent 8** (`run_upload_from_plan.py`, `src/upload_agent/build_upload_excel.py`) | Plan CSV → upload `.xlsx` (one sheet per account, column names match FB template) |
| **media-buyer-local** | Allowlist `act_*`, Multilogin A/B mapping, Meta Insights (costs), Read/Write flags |

### Pipeline

```
Morning report / plan.csv
        ↓
   Agent 8  →  output/upload_plan.xlsx
        ↓
   Agent 9  →  Meta Graph API (Campaign → Ad Set → Ad)
```

---

## New Files (`fb_agents`)

| File | Purpose |
|------|---------|
| `run_publish_campaigns.py` | CLI entry point |
| `src/meta_campaign_agent/__init__.py` | Package |
| `src/meta_campaign_agent/excel_reader.py` | Read Agent 8 xlsx by **header names** (not column letters) |
| `src/meta_campaign_agent/creative_finder.py` | Recursive search under `FB_CREATIVES_ROOT` by filename |
| `src/meta_campaign_agent/creative_uploader.py` | Upload image/video; cache `{path, mtime} → hash/video_id` |
| `src/meta_campaign_agent/campaign_builder.py` | Map Excel row → Graph API payloads |
| `src/meta_campaign_agent/meta_client.py` | HTTP client, error parsing, optional retry |
| `src/meta_campaign_agent/publish.py` | Orchestrator: validate → upload creatives → create objects |
| `config/ad_account_map.csv` | `account_name,ad_account_id` (sheet name from xlsx → `act_*`) |
| `docs/agent9_meta_campaign_publish.md` | User-facing runbook (RU) |

## Modified Files

| File | Change |
|------|--------|
| `config/paths.py` | `get_creatives_root()`, `get_ad_account_map_path()` |
| `.env.example` | `META_ACCESS_TOKEN`, `META_GRAPH_API_VERSION`, `FB_CREATIVES_ROOT` |
| `README.md` | Agent 9 section |
| `docs/AGENTS_MASTER_PLAN.md` | Row for Agent 9 |

---

## Configuration

### Environment (`.env`)

```
META_ACCESS_TOKEN=...              # System User; ads_management + ads_read on target accounts
META_GRAPH_API_VERSION=v22.0
FB_CREATIVES_ROOT=C:\Users\EvgeniSedin\Desktop\Creatives
```

### Ad account mapping (`config/ad_account_map.csv`)

```csv
account_name,ad_account_id
FacebookUL574 EC Fav,act_1234567890
```

- **Key:** Excel sheet name (Agent 8 truncates to 31 chars — map must use the same name).
- **Value:** Meta ad account id with `act_` prefix.
- Optional env override: `META_AD_ACCOUNT_MAP_PATH`.

### Defaults (product decisions)

| Setting | Default | Rationale |
|---------|---------|-----------|
| New campaign status | **PAUSED** | Safe; activate via `--activate` or separate command |
| Dry run | **`--dry-run` available** | Inspect payloads before spend |
| Write gate | Check `config/ad_account_map.csv` + optional sync with media-buyer-local `approved_write` (Phase 5) |

---

## Data flow (one Excel row)

1. **Read row** from upload xlsx: `Campaign Name`, `Pixel`, `Link`, `Countries`, `Age Min`, `Age Max`, `Locales`, `Copy`, `Creative`, `Image File name` / `Video File name`, budget fields, `Campaign Objective`, etc.
2. **Resolve ad account** from sheet name → `ad_account_map.csv`.
3. **Find creative file:** `creative_finder.find(root, filename)` — walk `FB_CREATIVES_ROOT` recursively; match basename case-insensitive; fail with clear error if ambiguous (two matches).
4. **Upload creative:** `POST /{act_id}/adimages` or `advideos`; store hash / video_id in session cache.
5. **Create Campaign** — fields from Excel defaults (`OUTCOME_LEADS`, `AUCTION`, etc. as in Agent 8 `DEFAULT_UPLOAD_VALUES`).
6. **Create Ad Set** — targeting: parse Interests JSON column; geo from `Countries`; age min/max; optimization `LEAD`; daily budget from Excel if present.
7. **Create Ad Creative + Ad** — title/body from Ad copy columns; link from `Link`; attach image hash or video id.

---

## CLI

```powershell
cd fb_agents

# Phase 1: validate + print plan (no API writes)
python run_publish_campaigns.py -i output/upload_plan.xlsx --dry-run

# Phase 4: publish one account sheet, campaigns stay PAUSED
python run_publish_campaigns.py -i output/upload_plan.xlsx --account "FacebookUL574 EC Fav"

# Activate after review
python run_publish_campaigns.py -i output/upload_plan.xlsx --account "..." --activate
```

| Flag | Description |
|------|-------------|
| `-i` / `--input` | Agent 8 output xlsx (required) |
| `--account` | Only one sheet (account name) |
| `--dry-run` | JSON plan only |
| `--activate` | Set `ACTIVE` instead of `PAUSED` |
| `--creatives-root` | Override `FB_CREATIVES_ROOT` |
| `-e` / `--upload-excel` | Path to `Upluad FB.xlsx` if extra lookups needed |

---

## Integration with media-buyer-local (later phases)

| Phase | Integration |
|-------|-------------|
| **1–4** | Standalone CLI in `fb_agents` |
| **5** | Optional HTTP check: `GET /api/accounts` — skip accounts without `approved_write` |
| **6** | Dashboard button: upload xlsx → trigger publish (backend proxy or subprocess) |

Costs remain on existing `/api/metrics/insights` — no duplication.

---

## Error handling

- **Missing creative file:** fail row, continue other rows; summary at end.
- **Graph API error:** capture `error_user_msg`, ad account, campaign name; no partial orphan ads without logging created IDs to `output/publish_log.jsonl`.
- **Idempotency (v1):** no auto-dedup by campaign name; operator uses dry-run. Future: check existing campaign by name before create.

---

## Testing strategy

| Level | What |
|-------|------|
| Unit | `creative_finder` (nested dirs, case, duplicates), `campaign_builder` payload shape |
| Fixture | Small xlsx with 1 row + dummy creative path |
| Integration | `--dry-run` against real upload xlsx |
| Live | One PAUSED campaign on test ad account (manual) |

---

## Implementation phases

| Phase | Deliverable |
|-------|-------------|
| **1** | `excel_reader` + `campaign_builder` + `--dry-run` |
| **2** | `creative_finder` + `creative_uploader` |
| **3** | Campaign + Ad Set create (PAUSED) |
| **4** | Ad + Creative create |
| **5** | Write approval + media-buyer-local hook |
| **6** | Dashboard trigger (optional) |

---

## Out of scope (v1)

- Browser automation / Multilogin launcher
- Automatic plan generation from morning report (use existing Agent 8 / `run_analysis_then_upload.py`)
- Campaign budget scheduling, Advantage+ catalog ads
- Video thumbnail auto-generation beyond Meta defaults

---

## Open items for owner review

1. Confirm **PAUSED by default** is acceptable.
2. Confirm **`config/ad_account_map.csv`** for account → `act_*` mapping.
3. Provide real **`FB_CREATIVES_ROOT`** path for first live test.
