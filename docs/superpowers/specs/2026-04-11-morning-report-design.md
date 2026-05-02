# Morning Report System — Design Spec

**Date:** 2026-04-11  
**Status:** Approved  
**Owner:** Evgeni Sedin

---

## Goal

Replace the ad-hoc morning workflow with a single command that produces a structured, actionable Markdown file covering 11 analysis sections per country. The file opens in Cursor for interactive AI follow-up (sections 6 & 7 happen in Cursor chat — no LLM calls during generation).

```powershell
python run_morning.py
# or
python run.py morning -e "C:\path\to\cursor example.xlsx"
```

Output: `analysis_agent/morning_report.md`

---

## New Files

| File | Purpose |
|------|---------|
| `run_morning.py` | Entry point (mirrors other `run_*.py` files at root) |
| `src/analysis_agent/gsheet_accounts.py` | Reads last GSheet tab, returns account status rows |
| `src/analysis_agent/morning_report.py` | Builds `MorningReport` dict (11 sections per country) |
| `src/analysis_agent/morning_markdown.py` | Renders `MorningReport` → Markdown string |

## Modified Files

| File | Change |
|------|--------|
| `run.py` | Add `morning` subcommand |
| `.env.example` | Add `GSHEET_ACCOUNTS_ID`, `GSHEET_ACCOUNTS_SHEET`, `MY_CM_NAME` |
| `analysis_agent/ANALYSIS_AGENT_WORKFLOW.md` | Document new `morning` command |

---

## Architecture

### Existing pipeline (untouched)

```
Excel → compute_metrics() → enrich_structure() → aggregate_combos()
                                                → report_markdown.py  (Markdown report)
```

### New morning pipeline

```
Excel + GSheet → morning_report.py → MorningReport dict
                                   → morning_markdown.py → morning_report.md
```

`morning_report.py` calls the same existing functions (`compute_metrics`, `enrich_structure`, `aggregate_combos`) and adds new aggregations on top. Existing `run.py analyze` is fully untouched.

---

## Report Structure (per country)

Each country section in `morning_report.md` contains:

| # | Section | Data source | Notes |
|---|---------|-------------|-------|
| 1 | **Evgeni vs team** | Stats Excel, `CM` column | Table: CM / Deposits / Leads / Spend / CPA / ROI; Evgeni row marked |
| 2 | **Recent trend (7 days)** | Stats Excel, `Date` column | What gained or lost deposits in last 7 days vs overall |
| 3 | **Top combos** | `aggregate_combos()` output | Device + Interest + Creative → Deposits / ROI, top 5 Strong-first |
| 4 | **Targeting breakdown** | Stats Excel, `Interest` parsed field | Interest code → Deposits / Leads / Spend / CPA |
| 5 | **Creative breakdown** | Stats Excel, `Ad Title` column | Ad Title → Deposits / Leads / Spend / CPA |
| 6 | **Context block for AI** | KB (`config/knowledge_base.json`) | Pre-formatted paste block: working combos + target KB entries; user pastes into Cursor chat |
| 7 | **Hypotheses block** | Same data | Pre-formatted paste block for test hypothesis generation; no LLM call |
| 8 | **GSheet overview** | Google Sheet last tab | Table: Account / To Do / Ready? |
| 9 | **Account readiness** | Google Sheet col J | Disabled/Restricted/VideoSelfie flagged; ready accounts listed |
| 10 | **Close recommendations** | Stats Excel per account | Ad sets with 0 FTD + notable spend per ready account |
| 11 | **Open 2 campaigns** | Top combos from section 3 | Per ready account: campaign name + 2 ad set specs |

---

## Google Sheet Integration (`gsheet_accounts.py`)

Reuses `_build_sheets_service()` pattern from `scripts/sync_targets_from_gsheet.py`.

### Environment variables

```
GSHEET_ACCOUNTS_ID=<sheet-id>          # from spreadsheet URL
GSHEET_ACCOUNTS_SHEET=auto_last        # "auto_last" = rightmost tab (today's data)
MY_CM_NAME=Evgeni Sedin                # your name as it appears in CM column
```

### Last-tab resolution

When `GSHEET_ACCOUNTS_SHEET=auto_last`: call `spreadsheets.get()` → pick the sheet with the largest `properties.index` (UI left-to-right order), **not** `sheets[-1]` (API order is not guaranteed to match the UI).

### Column mapping

| Column | Field |
|--------|-------|
| A | CM (campaign manager name) |
| B | Country (country code) |
| C | Account (full label: e.g. `FacebookMKTO1211 MX`) |
| J | To Do (status) |

### Account readiness

| To Do value | `is_ready` |
|-------------|-----------|
| `Account Disabled` | `False` |
| `Account Restricted` | `False` |
| `Video Selfie` | `False` |
| empty or any other value | `True` |

### Country assignment (morning report §6–7 per Excel country)

Each GSheet row appears **only** under the country whose Excel name maps to the **same code as column B** on that row. Excel `Account Alias` is **not** used to attach a row to a country (avoids duplicate aliases across geos landing in the wrong section).

- Skip rows where col C is empty

---

## Account Matching (GSheet → Excel)

**GSheet col C format:** `FacebookMKTO1211 MX`  
**Excel `Account Alias`:** `FacebookMKTO1211`

**Matching logic:** strip trailing ` XX` country-code suffix from GSheet col C value, compare to `Account Alias`. Normalization for fuzzy fallback: lowercase + strip underscores.

---

## Section 10: Close Recommendations

Per ready account, per country: rows where `Account Alias` matches account AND `# FTDs == 0` AND `$ Spend > 30`. Sort by Spend descending. Show top 5 worst offenders.

Format:
```
- AdsetName (Spend: $X, Leads: N, 0 FTDs)
```

---

## Section 11: Open 2 Campaigns

Take top 2 combos for country from `aggregate_combos()` (Strong first → Interesting). For each ready account in that country, suggest the same 2 combos.

Format:
```
FacebookMKTO1211 — Mexico (Amazon):
  Campaign 1: MX_Amazon_all_CCblc-pro_Mix4_40
    Ad Set: all + Mix4 + 40 + CCblc - pro  [Strong | 2 dep | ROI 0.24]
  Campaign 2: MX_Amazon_ios_Day_40
    Ad Set: ios + Day + 40 + CCblc - pro   [Interesting | 1 dep]
```

---

## Usage

```powershell
# Minimal (uses FB_STATS_EXCEL env default):
python run_morning.py

# With explicit Excel path:
python run_morning.py -e "C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"

# Via unified CLI:
python run.py morning -e "C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"

# Skip GSheet (if credentials not set up):
python run_morning.py --no-gsheet
```

Output always written to `analysis_agent/morning_report.md`.

---

## Dependencies

- `google-api-python-client` — already used by `scripts/sync_targets_from_gsheet.py`
- `google-auth-httplib2`, `google-auth-oauthlib` — same
- No new Python packages required
