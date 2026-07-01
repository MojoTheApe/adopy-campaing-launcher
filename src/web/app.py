"""FastAPI app: interactive morning report dashboard.

Routes:
    GET  /              - render the full morning report as HTML
    GET  /details       - return one <tr><td colspan> drill fragment
    POST /refresh       - invalidate cache, rebuild, redirect to /
    POST /build-upload  - §11 selections → validated Upload FB xlsx download
    GET  /api/ref-data  - JSON with ad copies, creatives, targetings from Upload FB.xlsx
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Make project root importable when invoked via `uvicorn src.web.app:app`
_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from src.analysis_agent.morning_drilldown import get_underlying_rows
from src.campaign_bridge.assembly import assemble_account_upload, assemble_from_form_slots
from src.campaign_bridge.dashboard_publisher import preview_jobs
from src.web.publish_jobs import get_job, start_job
from src.web.cache import cache_built_at, cache_clear, get_cached_report
from src.analysis_agent.money_format import fmt_usd
from src.creative_agent.creative_scanner import find_creative_file, scan_file, SUPPORTED_EXTS
from src.creative_agent.audience_builder import build_audience_profile
from src.creative_agent.stats_correlator import correlate
from src.creative_agent.vision_analyzer import analyze as vision_analyze
from src.creative_agent.prompt_generator import generate_brief
from src.upload_agent.build_upload_excel import load_reference_data
from src.campaign_bridge.open_rec_mapper import parse_creative_from_ad_title
from config.paths import get_upload_fb_excel_path


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_FAVICON = _STATIC_DIR / "favicon.svg"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


_ref_opts_cache: dict | None = None
_ref_opts_mtime: float = -1.0


def _load_ref_options() -> dict:
    """Load Upload FB.xlsx and return structured options for campaign builder dropdowns.

    Result is cached by file mtime to avoid slow xlsx re-reads on every request.
    """
    global _ref_opts_cache, _ref_opts_mtime
    excel_path = get_upload_fb_excel_path()
    empty: dict = {"ad_copies": [], "image_files": [], "video_files": [], "targetings": []}
    if not os.path.isfile(excel_path):
        return empty
    try:
        mtime = os.path.getmtime(excel_path)
    except OSError:
        return empty
    if _ref_opts_cache is not None and mtime == _ref_opts_mtime:
        return _ref_opts_cache
    try:
        ref = load_reference_data(excel_path)
    except Exception:
        return empty
    ad_copies = sorted(ref.get("ad_copy") or {})
    image_files = [
        {"id": k, "filename": v}
        for k, v in sorted((ref.get("image_files") or {}).items(), key=lambda x: x[0])
    ]
    video_files = [
        {"id": k, "filename": v}
        for k, v in sorted((ref.get("video_files") or {}).items(), key=lambda x: x[0])
    ]
    targetings = sorted(ref.get("interests") or {})
    result = {
        "ad_copies": ad_copies,
        "image_files": image_files,
        "video_files": video_files,
        "targetings": targetings,
    }
    _ref_opts_cache = result
    _ref_opts_mtime = mtime
    return result


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
templates.env.filters["creative_type"] = lambda name: parse_creative_from_ad_title(name)[0]
templates.env.filters["creative_id"] = lambda name: parse_creative_from_ad_title(name)[1]

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

    ref_opts = _load_ref_options()
    return templates.TemplateResponse(
        "report.html",
        {
            "request": request,
            "report": report,
            "warnings": warnings,
            "built_at": cache_built_at(),
            "upload_errors": upload_errors or {},
            "ref_options": ref_opts,
            "ref_options_json": json.dumps(ref_opts, ensure_ascii=False),
        },
    )


def _parse_build_upload_form(form) -> tuple[str, str, list[dict]]:
    """Parse campaign builder form (new slot-based format)."""
    country = (form.get("country") or "").strip()
    account_label = (form.get("account_label") or "").strip()
    try:
        camp_count = int(form.get("camp_count") or 0)
    except (TypeError, ValueError):
        camp_count = 0

    slots: list[dict] = []
    for idx in range(camp_count):
        creative_type = (form.get(f"camp_{idx}_creative_type") or "Image").strip()
        targeting     = (form.get(f"camp_{idx}_targeting") or "").strip()
        os_val        = (form.get(f"camp_{idx}_os") or "All").strip()
        age_val       = (form.get(f"camp_{idx}_age") or "35").strip()
        asset_val     = (form.get(f"camp_{idx}_asset") or "").strip().replace(" ", "")
        country_val   = country.upper()
        from datetime import date as _date
        ddmm = _date.today().strftime("%d.%m")
        auto_name     = "_".join(p for p in [country_val, asset_val, os_val, creative_type, targeting, age_val, ddmm] if p)
        name = (form.get(f"camp_{idx}_name") or "").strip() or auto_name
        if not name:
            continue
        slots.append(
            {
                "name": name,
                "creative_type": (form.get(f"camp_{idx}_creative_type") or "Image").strip(),
                "creative_id": (form.get(f"camp_{idx}_creative_id") or "").strip(),
                "creative_file": (form.get(f"camp_{idx}_creative_file") or "").strip(),
                "copy": (form.get(f"camp_{idx}_copy") or "").strip(),
                "targeting": (form.get(f"camp_{idx}_targeting") or "").strip(),
                "os": (form.get(f"camp_{idx}_os") or "All").strip(),
                "age": (form.get(f"camp_{idx}_age") or "35").strip(),
                "budget": (form.get(f"camp_{idx}_budget") or "").strip(),
                "asset": (form.get(f"camp_{idx}_asset") or "").strip(),
            }
        )
    return country, account_label, slots


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
                "country": country,
                "message": "Drill-down not available for this section.",
            },
        )
    try:
        ncols = int(ncols_override) if ncols_override else _NCOLS_BY_SECTION.get(section, 7)
    except (TypeError, ValueError):
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
                "country": country,
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


@app.get("/export/assets")
async def export_assets() -> Response:
    """Download an Excel file with asset summary (offer × country) for all countries."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, numbers
    from openpyxl.utils import get_column_letter

    try:
        report, _df, _warnings = get_cached_report()
    except (FileNotFoundError, ValueError) as e:
        return Response(content=str(e), status_code=400, media_type="text/plain")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Assets"

    # Header style
    hdr_fill = PatternFill("solid", fgColor="1F2937")
    hdr_font = Font(bold=True, color="58A6FF", size=10)
    hdr_align = Alignment(horizontal="center", vertical="center")

    headers = ["Country", "Asset", "Leads", "Deposits", "CR %", "Player Value ($)"]
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = hdr_align

    # Data rows
    row_idx = 2
    alt_fill = PatternFill("solid", fgColor="161B22")
    num_align = Alignment(horizontal="right")

    for c in report.get("countries", []):
        country = c.get("country", "")
        for asset in c.get("asset_summary", []):
            leads = asset.get("leads", 0)
            deposits = asset.get("deposits", 0)
            cr = round(deposits / leads * 100, 1) if leads > 0 else 0.0
            net = asset.get("net", 0.0)

            fill = alt_fill if row_idx % 2 == 0 else None
            row_data = [country, asset.get("offer", ""), leads, deposits, cr, net]
            for col_idx, val in enumerate(row_data, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                if fill:
                    cell.fill = fill
                if col_idx >= 3:
                    cell.alignment = num_align
                if col_idx == 5:
                    cell.number_format = '0.0"%"'
                if col_idx == 6:
                    cell.number_format = '#,##0.00'
            row_idx += 1

    # Column widths
    col_widths = [16, 26, 10, 12, 10, 18]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Auto-filter
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row_idx - 1}"

    # Freeze header row
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    from datetime import datetime as _dt
    date_str = _dt.now().strftime("%Y-%m-%d")
    filename = f"assets_{date_str}.xlsx"

    return Response(
        content=buf.read(),
        media_type=_MIME_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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


@app.get("/api/ref-data")
async def api_ref_data() -> JSONResponse:
    """Return ad copies, creatives, targetings from Upload FB.xlsx for campaign builder JS."""
    return JSONResponse(_load_ref_options())


@app.post("/preview-publish")
async def preview_publish(request: Request) -> JSONResponse:
    """Dry-run: convert form slots → PublishJob plan, return JSON (no Meta API calls)."""
    form = await request.form()
    country, account_label, slots = _parse_build_upload_form(form)
    if not country:
        return JSONResponse({"error": "Country not set — check form."})
    if not account_label:
        return JSONResponse({"error": "Account label not set — check form."})
    if not slots:
        return JSONResponse({"error": "No campaign slots found. Make sure the form has at least one campaign."})
    try:
        plan = preview_jobs(slots, account_label=account_label, country=country)
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "detail": traceback.format_exc()})
    return JSONResponse({"plan": plan, "account": account_label, "country": country})


@app.post("/publish-jobs")
async def publish_jobs_start(request: Request) -> JSONResponse:
    """Launch a background publish job. Returns job_id for polling."""
    form = await request.form()
    country, account_label, slots = _parse_build_upload_form(form)
    via_ml = (form.get("via_multilogin") or "1").strip() not in ("0", "false", "no")
    activate = (form.get("activate") or "0").strip() in ("1", "true", "yes")
    if not country or not account_label or not slots:
        return JSONResponse({"error": "Missing country, account or slots."}, status_code=400)
    job_id = start_job(
        account_label, country, slots,
        via_multilogin=via_ml,
        activate=activate,
    )
    return JSONResponse({"job_id": job_id, "total": len(slots)})


@app.get("/publish-jobs/{job_id}")
async def publish_jobs_status(job_id: str) -> JSONResponse:
    """Poll publish job progress. Returns status + results so far."""
    job = get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return JSONResponse(job)


@app.post("/build-upload")
async def build_upload(request: Request) -> Response:
    """Build Upload FB xlsx from campaign builder slots for one account."""
    form = await request.form()
    country, account_label, slots = _parse_build_upload_form(form)

    if not country or not account_label:
        return _render_report(
            request,
            upload_errors={"": {"": ["Missing country or account."]}},
        )
    if not slots:
        return _render_report(
            request,
            upload_errors={country: {account_label: ["Add at least one campaign slot."]}},
        )

    errors, xlsx_bytes, filename = assemble_from_form_slots(
        country=country,
        account_label=account_label,
        slots=slots,
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
