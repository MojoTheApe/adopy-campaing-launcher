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
| Date | `DATE` | date | Daily label as delivered; source timezone is explicit metadata or unknown |
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
- Remaining unmapped codes stay outside the selected-team mapping and are excluded by
  default; absence from that mapping does not prove their real team. `BI_STATS_ALL_CMS=1` keeps them as `CM <code>`.
- `bi_cm_map.json` is derived from a six-day overlap with the workbook and is to be confirmed
  by BI. Excluded unresolved CM rows remain outside the selected team, with a visible
  incomplete-coverage warning; inference alone does not prove every excluded row is foreign.

## Currency and day boundary (103e)

The original selected BI columns do not establish currency/timezone. Their absence from a
projection does not prove the warehouse has no metadata. On the existing read-only connection,
the reader uses a SELECT against information_schema.COLUMNS to discover optional explicit
`STATS_CURRENCY`, `DATE_TIMEZONE` and `DAY_BOUNDARY` columns. `STATS_CURRENCY` declares the
unit of the source's monetary metrics, including spend and net. An `ACCOUNT_CURRENCY` or
spend-only currency field is deliberately not used to relabel harmonised BI figures or net.
A denied metadata lookup does not hide the statistics: normal SELECT still works and missing
metadata remains unknown. This is supported behavior, not evidence those columns exist live.

- Currency is a canonical three-letter code, independent of its displayed sign. Unknown or
  ambiguous signs are never silently interpreted as USD.
- Dates remain the delivered BI DATE/calendar labels. Daily aggregates cannot be shifted into
  another timezone without their underlying intraday data; no such shift occurs.
- Explicit machine source-contract declarations can supply `BI_CURRENCY`, `BI_TIMEZONE` and
  `BI_DAY_BOUNDARY`. The corresponding workbook declarations are `STATS_CURRENCY`,
  `STATS_TIMEZONE` and `STATS_DAY_BOUNDARY`. `calendar_day` means the calendar day in the
  stated timezone. Set these only from authentic source metadata/contract, not from the
  account's potentially different unit/timezone. No value is configured automatically.
- Missing metadata is visible as unknown/incomplete in the report header, warning and API.
  The historical `BI_ASSUMPTIONS_CONFIRMED` flag cannot create facts and no longer turns a
  guessed USD/day into confirmed metadata. A new live or human approval ceremony is not needed
  to implement this safe state; authentic retained/schema/configuration evidence can supply facts.
- SQLite preserves row source and metadata. Additive migration retains old rows and workbook
  version receipts; old metadata-less rows stay unknown, rather than inheriting today's settings.
- Source table/Net/attribution/Workbench and Home amounts use their retained ISO unit (or
  explicit currency unknown); separate account money and Builder USD budgets are unchanged.
  Monetary period changes require comparable units/day bases; count changes remain available.
- The header, tooltips and API describe the selected period's actual workbook/BI/mixed rows,
  including during archive failure fallback. Archive is storage, not a new upstream provider.

## Source versus account figures (103d)

`GET /api/source-vs-account?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD` returns, per account alias,
the source spend (BI, archive or workbook, for the period) and the spend read from the account
(`account_spend` readings for the same inclusive period), separately, with per-account source
metadata. Account API currency/timezone is retained from the existing read without an additional
Graph call; display currency remains unchanged. Legacy ambiguous signs retain an unknown ISO code.
The gap (account minus source) and `differs` above0.01 exist only for known matching currency,
timezone and day basis. Missing readings, unknown metadata and confirmed currency/day mismatches
have distinct `comparison_state` values and an empty gap. Both original measured figures remain
available; unknown is not reported as a discrepancy or an artificial zero.

## Discovery facts and retained history

The author's21October clarification records that BI retains roughly8days and the owner supplied
a year workbook: SQLite accumulates BI days, replaces restated BI days and seeds workbook-only
history once per file version. This remains required. A later BI-window metadata change does not
rewrite the unit of older archived rows. ACL-105/Issue14 period behavior remains intact.

Actual BI unit/day contract, grain uniqueness and CM mapping membership remain unconfirmed in
the retained records; a successful fixture or confirmation flag does not establish them. These
are concrete source facts, not an added owner-only/live/upload acceptance gate. Warehouse access
already answers the access route; an Adverity extract/API key is not required in parallel.
The300s local revision poll and MAX(DATE_INSERTED)/rowcount identify local fetch/revision behavior,
not the upstream ETL cadence. Keep the original discovery questions and known unknowns visible.
