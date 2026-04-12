---
kind: spec
name: project-architecture
task: 0001-create-project-architecture-spec
created: 2026-04-02
status: draft
version: 1
tags:
  - architecture
  - agents
  - fb-campaigns
---

# FB Agents — Project Architecture

Multi-agent system for managing Facebook advertising campaigns:
analysis, Jira task creation, upload-file generation, and a
knowledge base for targeting context.

## Problem

Managing FB campaigns across multiple countries, offers, and
accounts requires repetitive manual work: analyzing statistics
to find winning ad-set combinations, creating Jira tasks for
landing pages, and assembling upload-ready Excel files that match
Facebook's bulk-upload format. This project automates each step
with a dedicated agent, connected through shared data formats
and entry-point scripts.

## Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                       DATA SOURCES                              │
│                                                                 │
│  cursor example.xlsx     Upluad FB.xlsx        .env             │
│  (FB campaign stats)     (reference data:      (JIRA_EMAIL,     │
│                           Accounts, Ad copy,    JIRA_API_TOKEN,  │
│                           Countries, Interests,  env overrides)  │
│                           Video/Image files,                     │
│                           LP's)                                  │
│                                                                 │
│  config/knowledge_base.json   knowledge_base/*.md               │
│  (targeting index)            (markdown descriptions)            │
│                                                                 │
│  Google Sheets (via scripts/sync_targets_from_gsheet.py)        │
└───────┬──────────────────────┬──────────────────┬───────────────┘
        │                      │                  │
        ▼                      ▼                  ▼
┌───────────────┐   ┌──────────────────┐   ┌─────────────┐
│  Agent 1      │   │  Agent 5         │   │  KB Agent   │
│  Analysis     │   │  Jira            │   │  Knowledge  │
│               │   │                  │   │  Base       │
│ Excel → DF    │   │ Excel →          │   │             │
│ compute_      │   │ Summary +        │   │ JSON index  │
│ metrics →     │   │ Description →    │   │ + markdown  │
│ enrich_ →     │   │ Jira API or      │   │ files →     │
│ aggregate_    │   │ browser paste    │   │ resolve,    │
│ combos →      │   │                  │   │ context,    │
│ report        │   │                  │   │ log unknown │
└───────┬───────┘   └──────────────────┘   └──────┬──────┘
        │                                         │
        │  combo rows                             │ target context
        │  (Strong / Interesting)                 │ (used by Agent 1
        │                                         │  during enrich)
        ▼                                         │
┌───────────────────────────────┐                 │
│  Bridge                      │◄────────────────┘
│  run_analysis_then_upload.py │
│                              │
│  combo_row_to_plan() →       │
│  plan rows (CSV format)      │
└───────────┬──────────────────┘
            │  plan rows
            ▼
┌───────────────────────────────┐
│  Agent 8                     │
│  Upload                      │
│                              │
│  load_reference_data() →     │
│  build_row() per combo →     │
│  write_upload_excel()        │
│                              │
│  Output: upload .xlsx        │
│  (one sheet per account)     │
└───────────────────────────────┘
```

### Data Flow: Analysis → Upload Pipeline

1. **Load** — `pd.read_excel(cursor example.xlsx)` → raw DataFrame
2. **Metrics** — `compute_metrics()` adds CPL, CPA, CR, Profit, ROI
3. **Enrich** — `enrich_structure()` parses Adset Name → Country Code,
   Device, Interest, Age; sets Creative from Ad Title; resolves
   targets via KB Agent (`resolve_target_code`, `log_unknown_target`)
4. **Aggregate** — `aggregate_combos()` groups by 7 dimensions,
   classifies Strong (≥2 deposits) / Interesting (1 dep + CR > 5%)
5. **Bridge** — `combo_row_to_plan()` converts combo rows to plan
   dicts (account, country, asset, os, creative, targeting, age,
   creative_number, date, copy)
6. **Upload** — Agent 8 loads reference data from Upluad FB.xlsx,
   calls `build_row()` per plan row (fills pixel, campaign name,
   interests JSON, country codes, creative filenames from reference
   sheets), writes one `.xlsx` with one sheet per account

### Data Flow: Interactive Mode

`run_upload_interactive.py` wraps the pipeline with a step-by-step
console dialog:

1. Prompt for stats Excel path (default from config)
2. Run Agent 1 pipeline → show `print_short_report`
3. List countries from data → user picks one
4. User specifies number of combos and account name
5. Call bridge `run()` → Agent 8 → output Excel

### Data Flow: Jira Pipeline (Agent 5)

1. Read Accounts sheet from Upluad FB.xlsx (rows without Link)
2. Extract country code from account name
3. Look up LP's sheet for landing page URL, stock, country
4. Build Summary (`Brand - Asset LPs for Account - CC - Media`)
   and Description (from Accounts + LP's data)
5. Create via Jira REST API (`--api`) or open browser tabs for
   manual paste

### Data Flow: Knowledge Base

```
Google Sheets ──sync_targets_from_gsheet.py──► config/knowledge_base.json
Excel offers  ──sync_offers_from_excel.py───► config/knowledge_base.json
Excel creatives─sync_creatives_from_excel.py► knowledge_base/creatives.md

knowledge_base.json ◄── knowledge_base.py (Python API)
                         │
                         ├─ resolve_target_code(code) → canonical key
                         ├─ get_target_context(code)  → {label, audience, ...}
                         ├─ get_offer_context(name)   → offer dict
                         ├─ log_unknown_target(code)   → output/unknown_targets.log
                         └─ load_markdown_section(key) → markdown string
```

## Components

### Summary Table

| # | Component | Location | Purpose |
|---|-----------|----------|---------|
| C1 | Analysis Agent | `src/analysis_agent/analyze_fb_stats.py` | Parse FB stats Excel, compute metrics, build Strong/Interesting combos by country |
| C2 | Upload Agent | `src/upload_agent/build_upload_excel.py` | Build FB bulk-upload Excel from plan rows + reference data |
| C3 | Jira Agent | `src/jira_agent/jira_from_accounts.py` | Create Jira LP tasks from Upluad FB.xlsx Accounts sheet |
| C4 | Knowledge Base Agent | `src/knowledge_agent/knowledge_base.py` | Python API for target/offer lookup, alias resolution, unknown logging |
| C5 | Analysis Rules Config | `config/analysis_rules.py` | Load text-segment, device, age, creative rules from JSON |
| C6 | Reading Rules JSON | `config/analysis_reading_rules.json` | Configurable rules: text segments, device keywords, age range, creative prefixes |
| C7 | KB Index | `config/knowledge_base.json` | Structured JSON: targets (with aliases, audience, FB interests) and offers |
| C8 | KB Markdown Files | `knowledge_base/*.md` | Human-readable descriptions: targets, creatives, offers, landing pages, test history |
| C9 | Paths Config | `config/paths.py` | Default file paths for stats Excel and Upluad FB.xlsx; env-var overrides |
| C10 | Bridge Script | `run_analysis_then_upload.py` | CLI pipeline: Agent 1 → filter/sort → `combo_row_to_plan` → Agent 8 |
| C11 | Interactive Entry | `run_upload_interactive.py` | Console UI: stats → short report → country/combo/account dialog → bridge |
| C12 | Plan Upload Entry | `run_upload_from_plan.py` | Direct Agent 8 entry point from plan CSV |
| C13 | Jira Entry Point | `run_jira_from_accounts.py` | Entry point for Agent 5 (Jira task creation) |
| C14 | Sync Scripts | `scripts/sync_*.py` | Sync KB data from Google Sheets and Excel into config/knowledge_base.json and knowledge_base/*.md |
| C15 | Utility Scripts | `scripts/_check_excel.py`, `scripts/_inspect_lps_sheet.py`, `scripts/_test_kb_resolution.py` | Debug/inspection helpers (prefixed with `_`) |
| C16 | LP Scripts | `scripts/apply_lp_fixes.py`, `scripts/parse_landing_pages.py` | Parse and fix landing page data in knowledge base |

### C1 — Analysis Agent

**File:** `src/analysis_agent/analyze_fb_stats.py`

**Contract:**
- **Input:** Excel file path (stats with columns: Country, Account Alias,
  Adset Name, Ad Title, # Leads, # FTDs, $ Spend, CM, Ad Asset, $ Net)
- **Processing:** `compute_metrics` → `enrich_structure` → `aggregate_combos`
- **Output:** DataFrame of combo rows categorized as Strong/Interesting,
  plus Markdown report from `build_full_report_markdown` in
  `report_markdown.py` (thin wrappers: `print_report`, `print_short_report`, etc.)
- **CLI:** `python src/analysis_agent/analyze_fb_stats.py --excel <path>
  [--country <name>] [--top N] [--output FILE] [--short] [--with-context]
  [--asset <name>]`

**Dependencies:** pandas, config.analysis_rules, config.paths,
src.knowledge_agent.knowledge_base

### C2 — Upload Agent

**File:** `src/upload_agent/build_upload_excel.py`

**Contract:**
- **Input:** Plan CSV (columns: account, country, asset, os, creative,
  targeting, age, creative_number, date, copy) + Upluad FB.xlsx for
  reference data
- **Processing:** `load_reference_data` → `build_sheets` → `write_upload_excel`
- **Output:** `.xlsx` with one sheet per account, columns matching FB
  bulk-upload template ("Upload all" sheet headers)
- **CLI:** `python run_upload_from_plan.py <plan.csv> [-o output.xlsx]
  [-e Upluad_FB.xlsx]`

**Dependencies:** openpyxl, config.paths

### C3 — Jira Agent

**File:** `src/jira_agent/jira_from_accounts.py`

**Contract:**
- **Input:** Upluad FB.xlsx (Accounts sheet: Account, Asset, Domain, Link;
  LP's sheet for landing page lookup)
- **Processing:** For each row without a Link value: extract country code,
  build Summary/Description, create Jira issue or open browser
- **Output:** Jira tasks (MAR project, Limited_QA label, Team: Media)
- **CLI:** `python run_jira_from_accounts.py [--api] [--dry-run]
  [--excel path] [--pause]`
- **Auth:** JIRA_EMAIL + JIRA_API_TOKEN from `.env`

**Dependencies:** openpyxl, requests (for API mode), python-dotenv,
config.paths

### C4 — Knowledge Base Agent

**File:** `src/knowledge_agent/knowledge_base.py`

**Contract:**
- **Input:** `config/knowledge_base.json` (structured index),
  `knowledge_base/*.md` (markdown descriptions)
- **Public API:**
  - `resolve_target_code(code)` → canonical key or None
  - `get_target_context(code)` → dict with label, audience, FB interests
  - `get_all_targets()` → full targets dict
  - `get_offer_context(name)` → offer dict or None
  - `get_all_offers()` → full offers dict
  - `log_unknown_target(code, source)` → appends to
    `output/unknown_targets.log` (dedup)
  - `load_markdown_section(filename)` → markdown string
  - `format_target_context_line(interest)` → formatted context line
- **Lookup strategy:** exact key → case-insensitive key → aliases[]

**Dependencies:** stdlib only (json, pathlib)

### C5–C9 — Configuration

| Component | File | Purpose |
|-----------|------|---------|
| C5 | `config/analysis_rules.py` | Loads rules from `analysis_reading_rules.json`; provides `get_text_segment_dictionary()`, `get_device_keywords()`, `get_age_range()`, `get_creative_skip_prefixes()` |
| C6 | `config/analysis_reading_rules.json` | Editable rules: text_segments (ad-title classification), device_keywords, age_range, creative_skip_prefixes |
| C7 | `config/knowledge_base.json` | Target entries with aliases/audience/FB-interests; offer entries |
| C8 | `knowledge_base/*.md` | Human-readable files: targets.md, creatives.md, offers.md, landing_pages.md, test_history.md |
| C9 | `config/paths.py` | Default paths; env-var overrides: `FB_STATS_EXCEL`, `FB_STATS_FOLDER`, `UPLOAD_FB_EXCEL` |

### C10–C13 — Entry Points

| Component | File | Mode | Description |
|-----------|------|------|-------------|
| C10 | `run_analysis_then_upload.py` | CLI (non-interactive) | Agent 1 → bridge → Agent 8; flags: `--excel`, `--account`, `--country`, `--asset`, `--top`, `--output`, `--plan-csv` |
| C11 | `run_upload_interactive.py` | Interactive console | Step-by-step: load stats → short report → pick country/combos/account → bridge → output |
| C12 | `run_upload_from_plan.py` | CLI | Direct Agent 8: plan CSV → upload Excel |
| C13 | `run_jira_from_accounts.py` | CLI | Agent 5: `--api` for Jira REST, default opens browser tabs |

### C14–C16 — Scripts

| File | Purpose |
|------|---------|
| `scripts/sync_targets_from_gsheet.py` | Sync targeting data from Google Sheets → `config/knowledge_base.json` |
| `scripts/sync_offers_from_excel.py` | Sync offer data from Excel → `config/knowledge_base.json` |
| `scripts/sync_creatives_from_excel.py` | Sync creative data from Excel → `knowledge_base/creatives.md` |
| `scripts/parse_landing_pages.py` | Parse LP data into `knowledge_base/landing_pages.md` |
| `scripts/apply_lp_fixes.py` | Apply corrections to LP data |
| `scripts/_check_excel.py` | Debug: inspect Excel structure |
| `scripts/_inspect_lps_sheet.py` | Debug: inspect LP's sheet |
| `scripts/_test_kb_resolution.py` | Debug: test KB target resolution |

## Data Sources and Outputs

### Data Sources

| Source | Format | Used By | Description |
|--------|--------|---------|-------------|
| `cursor example.xlsx` | Excel | Agent 1 | FB campaign statistics (~7000 rows): Country, Account Alias, Adset Name, Ad Title, # Leads, # FTDs, $ Spend, $ Net, CM, Ad Asset |
| `Upluad FB.xlsx` | Excel (multi-sheet) | Agent 5, Agent 8 | Reference data: Accounts (account/domain/pixel/link), Ad copy, Countries, Interests (FB targeting JSON), Video file name, Image FIle name, LP's, Upload all (template) |
| `.env` | Key-value | Agent 5 | JIRA_EMAIL, JIRA_API_TOKEN, optional path overrides |
| `config/knowledge_base.json` | JSON | KB Agent | Structured index of targets (aliases, audience, FB interests) and offers |
| `knowledge_base/*.md` | Markdown | KB Agent | Human-readable descriptions for targets, creatives, offers, landing pages, test history |
| Google Sheets | Spreadsheet | Sync scripts | Source for targeting data (synced via `sync_targets_from_gsheet.py`) |

### Outputs

| Output | Format | Produced By | Description |
|--------|--------|-------------|-------------|
| Console reports | Text (tables) | Agent 1 | Short report (top combos per country), CM breakdown, offer comparison, campaign recommendations |
| `output/upload_from_analysis.xlsx` | Excel | Agent 8 (via bridge) | FB bulk-upload file: one sheet per account, columns matching FB template |
| `output/upload_plan.xlsx` | Excel | Agent 8 (direct) | Upload file from plan CSV |
| Jira tasks | Jira issues (MAR project) | Agent 5 | LP creation tasks with Summary/Description, Labels: Limited_QA, Team: Media |
| `output/jira_task_paste.txt` | Text | Agent 5 (browser mode) | Summary + Description for manual paste |
| `output/unknown_targets.log` | Text log | KB Agent | Unresolved targeting codes (deduplicated) |
| Plan CSV (optional) | CSV | Bridge | Intermediate plan: account, country, asset, os, creative, targeting, age, creative_number, date, copy |

## Planned but Unimplemented Agents

Per `docs/AGENTS_MASTER_PLAN.md`, three agents are planned but not yet built:

### Agent 6 — Jira → Kitaro

**Intended role:** Monitor Jira tasks created by Agent 5 for a "ready"
status/transition, then automatically create a tracking campaign in
Kitaro (ad-tracking platform) using a predefined template.

**Status:** Not implemented. Design questions documented in
`docs/agent6_jira_to_kitaro.md` (Jira readiness criteria, Kitaro API
availability, required field mapping, code vs Make.com approach).

**Dependencies:** Jira API for status polling, Kitaro API or web
automation for campaign creation, campaign template definition.

### Agent 7 — Approved Combos

**Intended role:** Present Agent 1's Strong/Interesting combos to the
user for manual approval/selection, then pass the approved subset to
Agent 8 (upload) or to Jira tasks.

**Status:** Not implemented. Design questions documented in
`docs/agent7_approved_combos.md` (approval UI format — console/Excel/CSV,
output destination, combo source persistence, code vs Make.com approach).

**Dependencies:** Agent 1 output (combo DataFrame or saved CSV), user
approval mechanism, Agent 8 plan-row format.

### Agent 9 — Daily Task Manager

**Intended role:** Generate daily task lists and test follow-ups for
the team. Track what each team member should do and verify completion.

**Status:** Not implemented. No detailed design document exists.

**Dependencies:** Task format definition, team member list, integration
point (Jira or other task system), follow-up rules.

## Configuration

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `FB_STATS_EXCEL` | `C:\Users\EvgeniSedin\Desktop\cursor example.xlsx` | Path to stats Excel |
| `FB_STATS_FOLDER` | Parent dir of stats Excel | Folder for stats files |
| `UPLOAD_FB_EXCEL` | `C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx` | Path to reference Excel |
| `JIRA_EMAIL` | — | Jira Cloud email for API auth |
| `JIRA_API_TOKEN` | — | Jira Cloud API token |
| `JIRA_CREATE_ISSUE_URL` | `{JIRA_BASE_URL}/secure/CreateIssue!default.jspa` | Override Jira create-issue URL |
| `JIRA_TEAM_FIELD_ID` | Auto-detected | Custom field ID for Team (migrated) |

### Jira Configuration (hardcoded in Agent 5)

| Setting | Value |
|---------|-------|
| Base URL | `https://mar-adopy.atlassian.net` |
| Project Key | `MAR` |
| Issue Type | Task |
| Labels | `Limited_QA` |
| Team | Media |

## Verification

| Component | Criterion |
|-----------|-----------|
| Spec file | Exists at `artifacts/specs/project-architecture.md` with valid frontmatter (`kind: spec`, `name`, `task`, `created`, `status`) |
| Architecture | Diagram covers all 4 implemented agents (Analysis, Jira, Upload, KB) and their data flows |
| Components table | Lists every module under `src/`, `config/`, `scripts/`, and all `run_*.py` entry points |
| Data sources | Stats Excel, Upluad FB.xlsx, .env, KB JSON, KB markdown, Google Sheets are documented |
| Outputs | Console reports, upload Excel, Jira tasks, unknown targets log, plan CSV are documented |
| Planned agents | Agents 6 (Jira→Kitaro), 7 (Approved Combos), 9 (Daily Task Manager) described with intended purpose |
| Accuracy | All referenced files exist in the codebase; no phantom paths |
