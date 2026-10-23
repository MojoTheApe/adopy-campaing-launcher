# ACL-103 — BI to Stats workbook: field and granularity mapping

Source: MySQL `BI_Export`.`UF.Export` (read-only session, SELECT only). Target: the Stats
workbook schema, so the same pipeline runs (`compute_metrics`, `enrich_structure`).
Code: `src/analysis_agent/bi_stats.py` (`COLUMN_MAP`, `normalize`, `assign_cm`),
`bi_archive.py`, `bi_compare.py`. Connection settings come only from the machine `.env`
(`BI_MYSQL_HOST`, `BI_MYSQL_USER`, `BI_MYSQL_PASSWORD`; optional `BI_MYSQL_PORT`,
`BI_MYSQL_DATABASE`, `BI_MYSQL_TABLE`). They are never logged or shown.

## Fields

| Workbook column | BI column | Type after load | Notes |
|---|---|---|---|
| (CM name) | `CM_ID` | text, `Unknown` when empty | Code mapped to a name by `config/bi_cm_map.json`; see CM rules |
| Date | `DATE` | date | Day as delivered by BI; no time zone in the table |
| Account Alias | `ACCOUNT_ALIAS` | text | The key used to compare with account readings |
| Adset Name | `ADSET_NAME` | text | |
| Ad Title | `AD_TITLE` | text | |
| Ad Asset | `AD_ASSET` | text | |
| Country | `COUNTRY` | text | |
| Variant | `VARIANT` | text | |
| # Leads | `LEADS` | integer, empty counts as 0 | |
| # FTDs | `FTDS` | integer, empty counts as 0 | |
| $ Spend | `SPEND` | decimal as float, empty counts as 0 | Money; currency see below |
| $ Net | `NET` | decimal as float, empty counts as 0 | Money; currency see below |

## Granularity

One BI row is one combination of day, account alias, ad set, ad (title and asset), country,
variant and CM code, with the metrics for that combination. This is read from the columns the
query selects; row uniqueness was not verified against the live table. The table is read in
full on every read (no date bound), then reduced to the workbook schema.

## CM rules

- A code in `bi_cm_map.json` becomes that manager's name.
- `Unknown` takes the manager the same account has in its other BI rows (a foreign code on a
  shared account is not assigned to us).
- Any other unmapped code (other teams, Taboola) is dropped by default, because the workbook
  never held those rows. `BI_STATS_ALL_CMS=1` keeps them as `CM <code>`.
- `bi_cm_map.json` is derived from a six-day overlap with the workbook and is to be confirmed
  by BI.

## Currency and day boundary (103e)

The BI query carries neither a currency nor a time zone, and the table has no such column in
the selected set. The code therefore states the assumption explicitly (`bi_compare.provenance`):

- Currency: `USD` (`BI_CURRENCY` overrides).
- Day boundary: the BI calendar day exactly as delivered (`BI_DAY_BOUNDARY` overrides the text).
- While `BI_ASSUMPTIONS_CONFIRMED` is not set, the dashboard shows "BI · USD (assumed)" in the
  header, adds the note to the report warnings, and `/api/source-vs-account` returns
  `source.assumed = true`.
- After a live BI read confirms currency and day boundary, set `BI_ASSUMPTIONS_CONFIRMED=1`
  (and the two overrides if they differ). No code change is needed.
- A reading from an account in a different currency is never subtracted from a BI figure; the
  row is marked `currency_mismatch`.

## Source versus account figures (103d)

`GET /api/source-vs-account?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD` returns, per account alias,
the source spend (BI, archive or workbook, for the period) and the spend read from the account
(`account_spend` readings for the same period), separately, with their currencies, the gap
(account minus source) and a `differs` flag above 0.01. A missing, running or failed account
reading leaves the account side and the gap empty. The two numbers are never merged.

## Not confirmed without a live BI read

Currency, time zone / day boundary, row uniqueness of the granularity above, and the CM code
list.
