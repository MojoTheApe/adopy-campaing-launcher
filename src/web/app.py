"""FastAPI app: interactive morning report dashboard.

Three routes:
    GET  /          - render the full morning report as HTML
    GET  /details   - return one <tr><td colspan> drill fragment
    POST /refresh   - invalidate cache, rebuild, redirect to /
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Make project root importable when invoked via `uvicorn src.web.app:app`
_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from src.analysis_agent.morning_drilldown import get_underlying_rows
from src.web.cache import cache_built_at, cache_clear, get_cached_report
from src.analysis_agent.money_format import fmt_usd


_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _aggregate_key_to_query(key: Dict[str, Any]) -> str:
    """Render an aggregate_key dict as a URL query string for HTMX hx-get."""
    return urlencode({k: ("" if v is None else str(v)) for k, v in key.items()})


def _anchor_slug(text: str) -> str:
    """URL-safe slug for in-page anchors (e.g. 'Mexico' -> 'mexico')."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower())
    return slug.strip("-") or "unknown"


# Expose helpers to Jinja2 via custom filters
templates.env.filters["agg_qs"] = _aggregate_key_to_query
templates.env.filters["anchor_slug"] = _anchor_slug
templates.env.filters["fmt_usd"] = fmt_usd

# Drill fragment column counts per section — keep in sync with report.html
# table headers. The detail <td colspan> must match the parent table's
# column count or the layout breaks.
_NCOLS_BY_SECTION = {
    "cm_comparison": 8,   # CM, deposits, leads, spend, net, cpa, roi, drill
    "trend": 6,           # offer, device, interest, creative, deposits_total/recent
    "top_combos": 9,      # offer, device, interest, age, creative, category, deposits, leads, spend
    "targeting": 5,       # interest, deposits, leads, spend, cpa
    "creatives": 5,       # ad_title, deposits, leads, spend, cpa
    "optimization": 7,    # adset, spend, leads, ftds, cpl, cpa, reason
}

app = FastAPI(title="fb_agents — Morning Report")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    try:
        report, _df, warnings = get_cached_report()
    except FileNotFoundError as e:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "No data file", "message": str(e)},
            status_code=200,  # UI message, not API error
        )
    except ValueError as e:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "Excel format problem", "message": str(e)},
            status_code=200,
        )

    return templates.TemplateResponse(
        "report.html",
        {
            "request": request,
            "report": report,
            "warnings": warnings,
            "built_at": cache_built_at(),
        },
    )


@app.get("/details", response_class=HTMLResponse)
async def details(request: Request) -> HTMLResponse:
    """Return a single <tr><td colspan> fragment of underlying rows.

    HTMX swaps it as `afterend` of the clicked <tr>, so the response MUST
    be a single <tr> with one <td colspan="N"> child or the browser's
    HTML parser foster-parents it out of the table.
    """
    params = dict(request.query_params)
    section = params.pop("section", "")
    country = params.pop("country", "")
    # Drill-down is web-only and limited to §9 Optimization.
    if section != "optimization":
        ncols = 6
        return templates.TemplateResponse(
            "_details_fragment.html",
            {
                "request": request,
                "rows": [],
                "ncols": ncols,
                "section": section,
                "message": "Drill-down is only available in §9 Optimization.",
            },
        )
    ncols = _NCOLS_BY_SECTION.get(section, 7)

    try:
        _report, df, _warnings = get_cached_report()
    except (FileNotFoundError, ValueError):
        # Cache build failed — return a no-op fragment so the table layout
        # stays intact and the user is invited to fix the / page first.
        return templates.TemplateResponse(
            "_details_fragment.html",
            {
                "request": request,
                "rows": [],
                "ncols": ncols,
                "section": section,
                "message": "Data unavailable. Reload the page.",
            },
        )

    rows = get_underlying_rows(df, section=section, country=country, **params)
    return templates.TemplateResponse(
        "_details_fragment.html",
        {
            "request": request,
            "rows": rows,
            "ncols": ncols,
            "section": section,
            "message": "" if rows else "No matching rows.",
        },
    )


@app.post("/refresh")
async def refresh() -> RedirectResponse:
    """Force cache rebuild on next /. Redirects to / so the browser
    follows the redirect and re-renders with fresh data."""
    cache_clear()
    return RedirectResponse(url="/", status_code=303)
