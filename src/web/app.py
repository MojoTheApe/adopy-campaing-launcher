"""FastAPI app: interactive morning report dashboard.

Routes:
    GET  /              - render the full morning report as HTML
    GET  /details       - return one <tr><td colspan> drill fragment
    POST /refresh       - invalidate cache, rebuild, redirect to /
    POST /build-upload  - §11 selections → validated Upload FB xlsx download
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Make project root importable when invoked via `uvicorn src.web.app:app`
_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from src.analysis_agent.morning_drilldown import get_underlying_rows
from src.campaign_bridge.assembly import assemble_account_upload
from src.web.cache import cache_built_at, cache_clear, get_cached_report
from src.analysis_agent.money_format import fmt_usd
from src.creative_agent.creative_scanner import find_creative_file, scan_file, SUPPORTED_EXTS
from src.creative_agent.audience_builder import build_audience_profile
from src.creative_agent.stats_correlator import correlate
from src.creative_agent.vision_analyzer import analyze as vision_analyze
from src.creative_agent.prompt_generator import generate_brief


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_FAVICON = _STATIC_DIR / "favicon.svg"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _aggregate_key_to_query(key: Dict[str, Any]) -> str:
    """Render an aggregate_key dict as a URL query string for HTMX hx-get."""
    return urlencode({k: ("" if v is None else str(v)) for k, v in key.items()})


def _anchor_slug(text: str) -> str:
    """URL-safe slug for in-page anchors (e.g. 'Mexico' -> 'mexico')."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower())
    return slug.strip("-") or "unknown"


# Expose helpers to Jinja2 via custom filters
from urllib.parse import quote as _url_quote

templates.env.filters["agg_qs"] = _aggregate_key_to_query
templates.env.filters["anchor_slug"] = _anchor_slug
templates.env.filters["fmt_usd"] = fmt_usd
templates.env.filters["urlencode"] = lambda s: _url_quote(str(s), safe="")

# Drill fragment column counts per section — keep in sync with report.html
# table headers. The detail <td colspan> must match the parent table's
# column count or the layout breaks.
_NCOLS_BY_SECTION = {
    "cm_comparison": 8,   # CM, Dep, Leads, Spend, Net, CPA, ROI, (drill col)
    "trend": 6,           # offer, device, interest, creative, deposits_total/recent
    "top_combos": 9,      # offer, device, interest, age, creative, category, deposits, leads, spend
    "targeting": 5,       # interest, deposits, leads, spend, cpa
    "creatives": 5,       # ad_title, deposits, leads, spend, cpa
    "optimization": 7,    # adset, spend, leads, ftds, cpl, cpa, reason
}

app = FastAPI(title="fb_agents — Morning Report")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(_FAVICON, media_type="image/svg+xml")


def _render_report(
    request: Request,
    *,
    upload_errors: dict[str, dict[str, list[str]]] | None = None,
) -> HTMLResponse:
    try:
        report, _df, warnings = get_cached_report()
    except FileNotFoundError as e:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "No data file", "message": str(e)},
            status_code=200,
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
            "upload_errors": upload_errors or {},
        },
    )


def _parse_build_upload_form(form) -> tuple[str, str, list[dict]]:
    country = (form.get("country") or "").strip()
    account_label = (form.get("account_label") or "").strip()
    strategies = form.getlist("strategy")
    selections: list[dict] = []
    for raw_st in strategies:
        st = str(raw_st).strip()
        if not st.isdigit():
            continue
        campaign_name = (form.get(f"campaign_name_{st}") or "").strip()
        ad_titles = [
            (form.get(f"title_{st}_{i}") or "").strip()
            for i in range(3)
        ]
        selections.append(
            {
                "strategy_type": int(st),
                "campaign_name": campaign_name,
                "ad_titles": ad_titles,
            }
        )
    return country, account_label, selections


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render_report(request)


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
    # Optional ncols override (used for nested drill-downs, e.g. ads inside CM campaigns)
    ncols_override = params.pop("ncols", None)
    _DRILL_ENABLED = {"optimization", "cm_comparison"}
    if section not in _DRILL_ENABLED:
        ncols = 6
        return templates.TemplateResponse(
            "_details_fragment.html",
            {
                "request": request,
                "rows": [],
                "ncols": ncols,
                "section": section,
                "message": "Drill-down not available for this section.",
            },
        )
    ncols = int(ncols_override) if ncols_override else _NCOLS_BY_SECTION.get(section, 7)

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
            "country": country,
            "message": "" if rows else "No matching rows.",
        },
    )


@app.post("/refresh")
async def refresh() -> RedirectResponse:
    """Force cache rebuild on next /. Redirects to / so the browser
    follows the redirect and re-renders with fresh data."""
    cache_clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/brief", response_class=HTMLResponse)
async def creative_brief(request: Request, creative: str = "") -> HTMLResponse:
    """Analyze a creative by name — search recursively in FB_CREATIVES_ROOT.

    If found: run full analysis and render brief.
    If not found: render upload form.
    """
    creative_name = creative.strip()
    if not creative_name:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "No creative name", "message": "Pass ?creative=<name>"},
        )

    found_path = find_creative_file(creative_name)

    if found_path is None:
        # File not found — render upload form
        return templates.TemplateResponse(
            "brief.html",
            {
                "request": request,
                "creative_name": creative_name,
                "found": False,
                "brief_md": "",
                "prompt": "",
                "error": "",
            },
        )

    # File found — run analysis
    try:
        scan = scan_file(found_path)
        audience = build_audience_profile(scan["interest"], scan["country_code"])
        try:
            _, cached_df, _ = get_cached_report()
        except Exception:
            cached_df = None
        stats = correlate(scan["interest"], scan["text_segment"], scan["country_code"], df=cached_df)
        vision = vision_analyze(found_path)
        result = generate_brief(scan, vision, stats, audience)
        return templates.TemplateResponse(
            "brief.html",
            {
                "request": request,
                "creative_name": creative_name,
                "found": True,
                "file_path": str(found_path),
                "brief_md": result["markdown"],
                "prompt": result["prompt"],
                "scan": scan,
                "error": "",
            },
        )
    except Exception as e:
        return templates.TemplateResponse(
            "brief.html",
            {
                "request": request,
                "creative_name": creative_name,
                "found": False,
                "brief_md": "",
                "prompt": "",
                "error": str(e),
            },
        )


@app.post("/build-upload")
async def build_upload(request: Request) -> Response:
    """Build Upload FB xlsx from §11 checkboxes for one account."""
    form = await request.form()
    country, account_label, selections = _parse_build_upload_form(form)

    if not country or not account_label:
        return _render_report(
            request,
            upload_errors={"": {"": ["Missing country or account."]}},
        )
    if not selections:
        return _render_report(
            request,
            upload_errors={country: {account_label: ["Select at least one strategy."]}},
        )

    try:
        report, _df, _warnings = get_cached_report()
    except (FileNotFoundError, ValueError) as e:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "title": "Cannot build upload", "message": str(e)},
            status_code=200,
        )

    _bundle, errors, xlsx_bytes, filename = assemble_account_upload(
        report,
        country=country,
        account_label=account_label,
        selections=selections,
    )
    if errors:
        return _render_report(
            request,
            upload_errors={country: {account_label: errors}},
        )

    return Response(
        content=xlsx_bytes,
        media_type=_MIME_XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
