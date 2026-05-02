---
name: jenya-agent-start
description: >-
  Runs analyze_fb_stats.py with --country all, writes analysis_agent/_full_analytics_run.md,
  and requires reading the output into chat. Use when the user runs /jenya-agent-start,
  Jenya agent start, full CM analytics for all countries, or WORKFLOW_Country_CM_Report refresh.
---

# Jenya agent start (slash: `/jenya-agent-start`)

Same as `.cursor/commands/jenya-agent-start.md`. Run from **repo root** `fb_agents`.

## One run — all countries

```powershell
Set-Location "C:\Users\EvgeniSedin\fb_agents"
python src/analysis_agent/analyze_fb_stats.py --country all --top 2 --output analysis_agent/_full_analytics_run.md
```

- Excel: default `config/paths.py`, or `--excel "…"`.
- Optional: `--with-context`.
- Do **not** restrict to a single country for this workflow; output must include every geo from Excel.

## After success (required)

1. Path to `analysis_agent/_full_analytics_run.md`.
2. **Read** and **show** in the reply (≤250 lines: full file; >250: start until first `CM breakdown`, one sample `CM breakdown` + matching `Offer comparison`, list all `CM breakdown for …` headings, full path + open in editor).
3. Point to `analysis_agent/WORKFLOW_Country_CM_Report.md` (country CM narrative). For all CLI modes and flags, also point to `analysis_agent/ANALYSIS_AGENT_WORKFLOW.md`.

## On failure

Show stderr/traceback; suggest Excel columns and path checks.

## Related

- `src/analysis_agent/analyze_fb_stats.py`
- `analysis_agent/WORKFLOW_Country_CM_Report.md`
- `analysis_agent/ANALYSIS_AGENT_WORKFLOW.md`
- `.cursor/commands/jenya-agent-start.md`
