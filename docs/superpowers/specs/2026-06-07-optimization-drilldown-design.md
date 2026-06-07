# Optimization §9 Drill-Down Design

**Date:** 2026-06-07  
**Status:** Approved  
**Scope:** Web dashboard only (`src/web`); markdown §9 stays a compact ad-set overview.

## Problem

§9 Optimization shows Account → Ad set metrics but hides the ad level (`Ad Title`). Media buyers need to see which specific ad consumes spend inside a underperforming ad set.

## Solution

Extend the existing morning web dashboard (FastAPI + HTMX) with the same drill-down pattern used in §1–5:

```
Account (header)
  └─ Ad set row [click] → inline Ad Title rows sorted by Spend ↓
```

## Data model

| Level | Key | Metrics |
|-------|-----|---------|
| Account | `account_label` | group header |
| Ad set | `Adset Name` | Spend, Leads, FTDs, CPL, CPA, reason |
| Ad | `Ad Title` | Spend, Leads, FTDs, CPL, CPA, Ad Asset |

Each ad-set row carries `aggregate_key`:

```json
{
  "section": "optimization",
  "country": "Mexico",
  "account": "MX-acc-1",
  "adset": "MX_Amz_IOS_Mix_SM_35_03.06"
}
```

## Backend

1. `filter_optimization_rows(df, country, account, adset)` in `section_filters.py`
2. `get_underlying_rows(section="optimization", ...)` in `morning_drilldown.py`
3. `build_close_recommendations` adds `aggregate_key` per row (no aggregation change)

Drill rows sorted by `$ Spend` descending.

## UI

- `report.html` §9: table with drillable ad-set rows (replaces bullet list)
- `_details_fragment.html`: slim columns for `optimization` (Ad title, Spend, Leads, FTDs, CPL, CPA, Asset)
- `reason` styled red when `0 FTD` or benchmark breach
- Hint in section header: click ad set to drill

## Markdown

No structural change. Optional one-line hint pointing to `uvicorn src.web.app:app`.

## Testing

- `filter_optimization_rows` matches ad-set aggregate rows
- `get_underlying_rows(section="optimization")` returns sorted ad-level dicts
- `build_close_recommendations` includes `aggregate_key`

## Out of scope

- Separate `/optimization` page
- Nested tables inside `morning_report.md`
- Close/pause actions (read-only drill)
