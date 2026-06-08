# Section 11 → Upload FB Bridge Design

**Date:** 2026-06-08  
**Status:** Approved (brainstorming)  
**Scope:** Web dashboard §11 + campaign assembly for Upload FB Excel export. Meta API / Multilogin publish is **phase 2** (prepared, not implemented).

## Problem

§11 “What to Open” recommends up to four strategies per ready account (campaign name + three creatives) but is **read-only**. Media buyers must manually rebuild the same structure in `Upluad FB.xlsx` before any FB upload.

FB no longer accepts bulk Excel upload; future publish will use **Marketing API** via **Multilogin** (per-account IP). The Excel format remains the human-readable source of truth and matches Agent 8 output.

## Goals

1. Checkbox selection of strategies per account; **Submit per account** (multiple Submit buttons per country §11).
2. Editable **campaign/ad set name** (single field — always equal) and **three ad titles** per selected strategy.
3. **Pre-flight validation** in UI; block Submit on errors; on success **download** `.xlsx` in Agent 8 / Upload FB template format.
4. **Link gate** from `Upluad FB.xlsx` Accounts sheet: empty → Jira; Jira URL → Keitaro; other URL → tracking OK.
5. Architecture that reuses the same internal bundle for future `publish_api(via_multilogin=True)` without re-entering data.

## Non-goals (this phase)

- Live Meta API publish from dashboard Submit button
- Multilogin session launch from web UI
- Changing §11 recommendation logic (`build_open_recommendations`)
- Markdown report changes (§11 stays informational in `.md`)

## User decisions (locked)

| Topic | Choice |
|-------|--------|
| Selection UX | Checkboxes per strategy; **one Submit per account** |
| Submit outcome | Validate first; block on errors; download xlsx when clean |
| Ad Set vs Campaign Name | **Always equal** — one editable field sets both |
| Ads per ad set | **3 rows** in Excel per selected strategy (shared campaign/ad set name) |
| Ad title → Excel | **Copy** = edited title; **Creative number** parsed from title digits |
| Link classification | **By domain** (see Validation) |
| FB upload path | Excel export now; API + Multilogin later via shared `CampaignBundle` |

## Architecture

```
§11 UI (forms: checkboxes + inputs + Submit per account)
        ↓ POST /build-upload
src/campaign_bridge/assembly.py
  ├─ open_rec_to_plan_rows()      # 1 strategy → up to 3 plan dicts
  ├─ validate_account_bundle()  # link, pixel, creative files, account map
  └─ build_upload_workbook()      # reuse Agent 8 (build_upload_excel.py)
        ↓
  export_xlsx()  →  browser download          [phase 1 — this spec]
  publish_api()  →  Agent 9 + Multilogin      [phase 2 — deferred]
```

### New modules

| Path | Responsibility |
|------|----------------|
| `src/campaign_bridge/open_rec_mapper.py` | Map §11 rec + user edits → plan rows; parse creative number from ad title |
| `src/campaign_bridge/link_validation.py` | Classify Accounts `Link` by hostname |
| `src/campaign_bridge/validation.py` | Account lookup, pixel, creative file on disk |
| `src/campaign_bridge/assembly.py` | Orchestrate validate → workbook bytes |
| `src/campaign_bridge/bundle.py` | `CampaignBundle` dataclass (rows + account metadata) |

### Web route

`POST /build-upload` in `src/web/app.py`:

- Input: form fields from one account’s §11 block (selected `strategy_type` values, edited names/titles, `country`, `account_label`).
- Output on success: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` attachment.
- Output on validation failure: HTML fragment or redirect with error list (same page, `.upload-errors` region); HTTP 422 or 200 with errors — prefer **200 + errors in template** so the report stays visible.

Reference data: `Upluad FB.xlsx` via `config.paths.get_upload_fb_excel_path()`.

## Data mapping: §11 rec → plan row

Source: `open_recs[account_label]` entry from `build_open_recommendations()`:

```python
{
  "strategy_type": 1,
  "campaign_name": "EC_Ait_All_Video_Day_35+_08.06",
  "targeting": "Day",
  "offer": "AI Trader",
  "creatives": [{"name": "2046 - Text 4", ...}, ...],
  "device": "all",
  "age": "35+",
}
```

`device` and `age` are added to each rec in `_assemble_open_campaign()` (additive fields for the bridge; markdown rendering ignores them).

For each **selected** strategy and each of **3 creatives** (pad to 3 if fewer in rec — user can fill blanks):

| Plan field | Source |
|------------|--------|
| `account` | `account_label` → resolve Upload FB sheet name (`get_account_info` fuzzy match) |
| `country` | Country section name / code from form |
| `asset` | `rec.offer` |
| `os` | Device from primary combo (`All` / `IOS` / `Android`) |
| `creative` | `Image` or `Video` from creative type heuristic (same as §11 `_creative_type_for_campaign` per ad) |
| `targeting` | `rec.targeting` |
| `age` | Primary combo age or `35` |
| `creative_number` | Digits extracted from edited ad title (`_parse_creative_from_ad_title`) |
| `copy` | Edited ad title string |
| `campaign_name` | User-edited campaign/ad set name (override Agent 8 auto-name) |
| `ad_set_name` | Same as `campaign_name` |

`build_row()` in Agent 8 already sets `Ad Set Name` = `Campaign Name` when column exists; pass explicit override via plan keys `campaign_name` / `ad_set_name`.

**Row count:** N selected strategies × 3 creatives = up to 12 rows per account Submit.

## UI (report.html §11)

Each `.open-account` becomes a `<form method="post" action="/build-upload">`:

```
☐ 1 · trend + stable
   [input: campaign_name]           ← default r.campaign_name
   [input: ad title 1]              ← default cr.name
   [input: ad title 2]
   [input: ad title 3]

☐ 2 · trend + variation
   ...

[ Submit — {account_label} ]        ← disabled if no checkbox checked
<div class="upload-errors">...</div>
```

Rules:

- Unchecked strategies are omitted from payload and not validated.
- Submit disabled until ≥1 checkbox in that account form.
- Standard form POST (not HTMX) for reliable file download.
- Hidden fields: `country`, `account_label`; per strategy: `strategy_type` only.

**Server-side rec lookup:** On POST, reload the full open rec from cached morning report by `(country, account_label, strategy_type)`. User edits apply only to `campaign_name` and the three ad titles; `offer`, `targeting`, `device`, `age` come from the cached rec.

Styling: match existing dark theme; errors in red/muted panel below Submit.

## Validation

Lookup account in `Upluad FB.xlsx` Accounts sheet (`load_reference_data` + `get_account_info`).

### Link classification (`link_validation.py`)

| Condition | User message | Blocks Submit |
|-----------|--------------|---------------|
| `Link` empty or whitespace | **Open Jira Task** — provision asset/link in Jira first | yes |
| Host matches Jira allowlist | **Open Keitaro** — replace Jira URL with tracking link | yes |
| Any other non-empty URL | OK (tracking link) | no |

Jira host allowlist:

- Default: `*.atlassian.net` (suffix match)
- Env override: `JIRA_LINK_HOSTS=company.atlassian.net,jira.internal.example` (comma-separated exact or suffix hosts)

Use `urllib.parse.urlparse`; require `http`/`https` scheme for non-empty links.

### Additional checks (per selected strategy / row)

| Condition | Message | Blocks |
|-----------|---------|--------|
| Account not in Upload FB | **Account not found in Upload FB** | yes |
| `Pixel` empty | **Pixel missing** in Upload FB Accounts | yes |
| Creative number not parseable from ad title | **Cannot parse creative number from: {title}** | yes |
| Creative filename missing in ref workbook | **Creative file name missing for #{n}** | yes |
| File not under `FB_CREATIVES_ROOT` | **Creative file not found: {filename}** | yes |

Warnings (do not block in phase 1): empty `Campaign Daily Budget`, unresolved Interests JSON — surface as yellow hints if Agent 8 would leave them empty.

## Excel output

- One workbook per Submit.
- **One sheet** named after resolved Upload FB account sheet.
- Rows: all selected strategies × 3 creatives, column order from `Upload all` template in reference workbook.
- Filename: `upload_{account_slug}_{YYYYMMDD_HHMM}.xlsx` (slug = alphanumeric from account label).

Reuse: `parse_plan` equivalent (in-memory list of dicts) → `build_sheets()` / `write_upload_excel()` from `src/upload_agent/build_upload_excel.py`.

## Phase 2: API + Multilogin (prepared, not built)

`CampaignBundle` in `bundle.py`:

```python
@dataclass
class CampaignBundle:
    account_sheet: str
    account_label: str
    plan_rows: list[dict]
    link_status: str  # ok | jira | empty

    def to_workbook_bytes(self) -> bytes: ...
    def to_publish_jobs(self) -> list[PublishJob]: ...  # phase 2
```

Future dashboard action calls `publish_live_via_multilogin()` from `src/meta_campaign_agent/publish.py` with jobs built from the same bundle — no manual Excel re-upload.

Requires: `config/ad_account_map.csv`, Multilogin tokens, `--confirm` safety gate.

## Error handling

- Missing `Upluad FB.xlsx`: form-level error “Upload FB workbook not found at {path}”.
- Report cache miss / stale strategy key: “Recommendation no longer available — refresh report”.
- Partial creative list (<3): render 3 inputs; empty titles for missing slots skipped in rows unless user fills them (empty title → validation error for that strategy).

## Testing

| Test file | Covers |
|-----------|--------|
| `tests/test_open_rec_mapper.py` | 1 strategy → 3 plan rows; shared campaign name; creative number parse |
| `tests/test_link_validation.py` | empty, atlassian.net, tracking URL |
| `tests/test_campaign_assembly.py` | validate + workbook bytes; blocked on Jira link |
| `tests/test_web_build_upload.py` | POST with fixtures → 200 attachment or errors in body |

Use monkeypatched `datetime` where campaign names include date (consistent with other morning tests).

## Implementation order (for planning skill)

1. `link_validation` + `open_rec_mapper` + unit tests
2. `validation` + `assembly` + `CampaignBundle`
3. `POST /build-upload` + template form in §11
4. Integration test end-to-end with fixture Upload FB xlsx

## References

- Agent 8: `src/upload_agent/build_upload_excel.py` (`COL_AD_SET_NAME`, `build_row`)
- Bridge precedent: `run_analysis_then_upload.py` (`combo_row_to_plan`, `_parse_creative_from_ad_title`)
- Agent 9 / Multilogin: `docs/agent9_meta_campaign_publish.md`, `src/meta_campaign_agent/publish.py`
- §11 builder: `src/analysis_agent/morning_report.py` (`build_open_recommendations`, `_assemble_open_campaign`)
- GSheet link workflow: `src/analysis_agent/gsheet_accounts.py` (col E Accounts tab)
