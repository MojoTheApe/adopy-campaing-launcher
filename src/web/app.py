"""FastAPI app: interactive morning report dashboard.

Routes:
    GET  /              - render the full morning report as HTML
    GET  /details       - return one <tr><td colspan> drill fragment
    POST /refresh       - rebuild report (reuse Excel DF), JSON or redirect to /
    POST /build-upload  - §11 selections → validated Upload FB xlsx download
    GET  /api/ref-data  - JSON with ad copies, creatives, targetings from Upload FB.xlsx
"""
from __future__ import annotations

import asyncio
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
from src.analysis_agent.morning_report import _my_cm_totals
from src.campaign_bridge.assembly import assemble_account_upload, assemble_from_form_slots
from src.campaign_bridge.dashboard_publisher import preview_jobs
from src.fb_ads_rpa.config import use_rpa_publish
from src.fb_ads_rpa.preview import preview_campaign_drafts
from src.fb_ads_rpa.rpa_publisher import RpaPublisher
from src.web.publish_jobs import get_job, start_job, cancel_job
from src.web.cache import cache_built_at, cache_clear, get_cached_report
from src.analysis_agent.money_format import fmt_usd
from src.creative_agent.creative_scanner import find_creative_file, scan_file, SUPPORTED_EXTS
from src.creative_agent.audience_builder import build_audience_profile
from src.creative_agent.stats_correlator import correlate
from src.creative_agent.vision_analyzer import analyze as vision_analyze
from src.creative_agent.prompt_generator import generate_brief
from src.upload_agent.build_upload_excel import load_reference_data
from src.campaign_bridge.open_rec_mapper import parse_creative_from_ad_title
from config.paths import get_upload_fb_excel_path, get_creatives_root, get_ad_account_map_path
from src.campaign_bridge.link_validation import classify_link
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry
from src.web.adset_helpers import parse_adset_for_builder, resolve_interest_alias
from src.meta_campaign_agent.creative_finder import (
    find_creative_file as _find_creative_file_scoped,
    CreativeNotFoundError,
    AmbiguousCreativeError,
)


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_FAVICON = _STATIC_DIR / "favicon.svg"
_FAVICON_ICO = _STATIC_DIR / "favicon.ico"
_DRAFT_STATUS_PATH = _PROJ_ROOT / "output" / "draft_status.json"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


_ref_opts_cache: dict | None = None
_ref_opts_mtime: float = -1.0
_ref_full_cache: dict | None = None
_ref_full_mtime: float = -1.0


def _write_link_to_excel(account_name: str, link: str) -> str | None:
    """Write tracking link to Upload FB.xlsx → Accounts sheet → Link column.

    Returns the matched account name if found, or None if not found.
    Invalidates the in-memory ref cache so the next request picks up the new value.
    """
    from openpyxl import load_workbook as _lw

    excel_path = get_upload_fb_excel_path()
    if not os.path.isfile(excel_path):
        return None

    wb = _lw(excel_path, read_only=False, data_only=False)
    if "Accounts" not in wb.sheetnames:
        return None

    ws = wb["Accounts"]

    # Find columns
    account_col = link_col = None
    for col in range(1, ws.max_column + 1):
        hdr = (ws.cell(row=1, column=col).value or "")
        if str(hdr).strip().lower() == "account":
            account_col = col
        elif str(hdr).strip().lower() == "link":
            link_col = col

    if not account_col or not link_col:
        return None

    # Find matching row (case-insensitive, partial match same as get_account_info)
    name_lower = account_name.strip().lower()
    matched_row = None
    matched_name = None
    for r in range(2, ws.max_row + 1):
        val = ws.cell(row=r, column=account_col).value
        if val is None:
            continue
        key = str(val).strip()
        if key.lower() == name_lower or name_lower in key.lower() or key.lower() in name_lower:
            matched_row = r
            matched_name = key
            break

    if matched_row is None:
        return None

    ws.cell(row=matched_row, column=link_col).value = link.strip() or None
    wb.save(excel_path)

    # Invalidate ref caches so the next dashboard load picks up the new link
    global _ref_full_cache, _ref_full_mtime, _ref_opts_cache, _ref_opts_mtime
    _ref_full_cache = None
    _ref_full_mtime = -1.0
    _ref_opts_cache = None
    _ref_opts_mtime = -1.0

    return matched_name


def _load_draft_status() -> dict:
    """Load per-account draft-ready status from output/draft_status.json."""
    if not _DRAFT_STATUS_PATH.is_file():
        return {}
    try:
        with open(_DRAFT_STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_upload_ref_cached() -> dict:
    """Return the full load_reference_data() result, cached by file mtime."""
    global _ref_full_cache, _ref_full_mtime
    excel_path = get_upload_fb_excel_path()
    if not os.path.isfile(excel_path):
        return {}
    try:
        mtime = os.path.getmtime(excel_path)
    except OSError:
        return {}
    if _ref_full_cache is not None and mtime == _ref_full_mtime:
        return _ref_full_cache
    try:
        ref = load_reference_data(excel_path)
        _ref_full_cache = ref
        _ref_full_mtime = mtime
        return ref
    except Exception:
        return {}


def _build_account_preflight(account_labels: list[str]) -> dict[str, dict]:
    """Return preflight info per account:
    {acc_label: {link, link_status, missing_map_fields: [str], has_map_entry}}
    """
    from src.upload_agent.build_upload_excel import get_account_info

    ref = _get_upload_ref_cached()
    map_path = get_ad_account_map_path()
    account_map = load_ad_account_map(map_path) if os.path.isfile(map_path) else {}

    result: dict[str, dict] = {}
    for label in account_labels:
        # Link + ad_account_id from Upload FB Accounts sheet
        if ref:
            info = get_account_info(ref, label)
            link = (info.get("link") or "").strip()
            excel_act_id = (info.get("ad_account_id") or "").strip()
        else:
            link = ""
            excel_act_id = ""
        link_status = classify_link(link)

        # Account map completeness
        entry = resolve_account_entry(account_map, label)
        missing: list[str] = []
        prefill: dict[str, str] = {}  # pre-populate inputs from Excel values

        if not entry:
            missing = ["ad_account_id", "profile_id"]
        else:
            if not (entry.ad_account_id or "").strip():
                missing.append("ad_account_id")
            if not (entry.profile_id or "").strip():
                missing.append("profile_id")

        # page_name is optional (used only by the visual ad preview, not the
        # real publish flow) — tracked separately so a missing value doesn't
        # trip the "⚠ missing: ..." header badge that signals a broken account.
        page_name = (entry.page_name if entry else "") or ""

        # Pre-fill ad_account_id from Excel if it's missing from the map
        if "ad_account_id" in missing and excel_act_id:
            prefill["ad_account_id"] = excel_act_id

        result[label] = {
            "link": link,
            "link_status": link_status,
            "missing_map_fields": missing,
            "has_map_entry": entry is not None,
            "prefill": prefill,
            "page_name": page_name,
        }
    return result


def _load_sheet_targets() -> list[dict]:
    """Targeting profiles from the Google Sheet "Detailed targeting" tab.

    Same shape as _load_kb_targets(). The sheet loader keeps its own 5-min
    cache, so calling this per request is cheap; [] on any failure so the
    caller can fall back.
    """
    try:
        from src.analysis_agent.gsheet_targeting import load_targeting_profiles

        profiles = load_targeting_profiles()
    except Exception:
        return []
    result = [
        {"code": p.name, "label": p.name, "fb_interest": p.interests}
        for p in profiles.values()
    ]
    result.sort(key=lambda x: x["code"].lower())
    return result


def _load_kb_targets() -> list[dict]:
    """Load targeting codes + FB interests from config/knowledge_base.json.

    Returns a list of dicts: [{code, label, fb_interest}, ...] sorted by code.
    """
    from pathlib import Path as _Path
    kb_path = _Path(__file__).resolve().parents[2] / "config" / "knowledge_base.json"
    if not kb_path.is_file():
        return []
    try:
        with open(kb_path, encoding="utf-8") as f:
            kb = json.load(f)
    except Exception:
        return []
    targets = kb.get("targets") or {}
    result = []
    for code, entry in targets.items():
        fb_interest = (entry.get("fb_interest") or "").strip()
        label = (entry.get("label") or code).strip()
        if fb_interest:
            result.append({"code": code, "label": label, "fb_interest": fb_interest})
    result.sort(key=lambda x: x["code"].lower())
    return result


def _load_ref_options() -> dict:
    """Load Upload FB.xlsx and return structured options for campaign builder dropdowns.

    Result is cached by file mtime to avoid slow xlsx re-reads on every request.
    Targeting codes are loaded from knowledge_base.json (synced from Google Sheet).
    """
    global _ref_opts_cache, _ref_opts_mtime
    excel_path = get_upload_fb_excel_path()
    empty: dict = {"ad_copies": [], "image_files": [], "video_files": [], "targetings": []}
    if not os.path.isfile(excel_path):
        result = {**empty, "targetings": _load_kb_targets()}
        return result
    try:
        mtime = os.path.getmtime(excel_path)
    except OSError:
        return {**empty, "targetings": _load_kb_targets()}
    if _ref_opts_cache is not None and mtime == _ref_opts_mtime:
        # targetings come from the Google Sheet (own 5-min cache) — refresh
        # them independently of the Excel mtime this cache is keyed on, or
        # sheet edits would stay invisible until the Excel file changes.
        fresh = _load_sheet_targets()
        if fresh:
            return {**_ref_opts_cache, "targetings": fresh}
        return _ref_opts_cache
    try:
        ref = load_reference_data(excel_path)
    except Exception:
        return {**empty, "targetings": _load_kb_targets()}
    ad_copies = sorted(ref.get("ad_copy") or {})
    image_files = [
        {"id": k, "filename": v}
        for k, v in sorted((ref.get("image_files") or {}).items(), key=lambda x: x[0])
    ]
    video_files = [
        {"id": k, "filename": v}
        for k, v in sorted((ref.get("video_files") or {}).items(), key=lambda x: x[0])
    ]
    # Single source of truth (user direction 2026-07-30): the "Detailed
    # targeting" tab of the shared Google Sheet — the same profiles the RPA
    # resolves at publish time, so the picker can never offer a profile the
    # publish path doesn't know. KB/Excel remain as fallbacks only.
    targetings = _load_sheet_targets()
    if not targetings:
        targetings = _load_kb_targets()
    if not targetings:
        targetings = [
            {"code": t, "label": t, "fb_interest": t}
            for t in sorted(ref.get("interests") or {})
        ]
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
templates.env.filters["tojson"] = lambda obj: json.dumps(obj, ensure_ascii=False, default=str)

# Drill fragment column counts per section — keep in sync with report.html
# table headers. The detail <td colspan> must match the parent table's
# column count or the layout breaks.
_NCOLS_BY_SECTION = {
    "cm_comparison": 8,   # CM, Dep, Leads, Spend, Net, CPA, ROI, (drill col)
    "trend": 6,           # offer, device, interest, creative, deposits_total/recent
    "top_combos": 10,     # offer, device, interest, age, creative, category, deposits, leads, spend, (builder col)
    "targeting": 5,       # interest, deposits, leads, spend, cpa
    "creatives": 5,       # ad_title, deposits, leads, spend, cpa
    "optimization": 7,    # adset, spend, leads, ftds, cpl, cpa, reason
}

app = FastAPI(title="Adopy Campaigns Launcher")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    if _FAVICON_ICO.is_file():
        return FileResponse(_FAVICON_ICO, media_type="image/x-icon")
    return FileResponse(_FAVICON, media_type="image/svg+xml")


def _enrich_country_overview(report: Dict[str, Any]) -> None:
    """Ensure per-country my_totals / my_spend for home overview (cached reports)."""
    for country in report.get("countries", []):
        if "my_totals" not in country:
            country["my_totals"] = _my_cm_totals(country.get("cm_comparison", []))
        if "my_spend" not in country:
            country["my_spend"] = country["my_totals"]["spend"]


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

    _enrich_country_overview(report)
    ref_opts = _load_ref_options()

    # Collect all account labels from open_recs across all countries
    all_account_labels: list[str] = []
    for c in report.get("countries", []):
        for label in (c.get("open_recs") or {}).keys():
            if label not in all_account_labels:
                all_account_labels.append(label)

    account_preflight = _build_account_preflight(all_account_labels)
    draft_status = _load_draft_status()

    # {country_slug: {account_alias: account_label}} — lets client JS resolve the
    # "Account Alias" strings shown in Combos/CM-drill rows to the accordion key
    # (account_label) the Campaign Builder actually keys its UI on.
    account_alias_map: dict[str, dict[str, str]] = {}
    for c in report.get("countries", []):
        cid = _anchor_slug(c.get("country", ""))
        alias_map: dict[str, str] = {}
        for acc in c.get("account_rows") or []:
            alias = acc.get("alias")
            label = acc.get("account_label")
            if alias and label:
                alias_map[alias] = label
        account_alias_map[cid] = alias_map

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
            "account_preflight": account_preflight,
            "account_preflight_json": json.dumps(account_preflight, ensure_ascii=False),
            "account_alias_map_json": json.dumps(account_alias_map, ensure_ascii=False),
            "draft_status": draft_status,
        },
    )


_COUNTRY_CODES: dict[str, str] = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ", "angola": "AO",
    "argentina": "AR", "armenia": "AM", "australia": "AU", "austria": "AT",
    "azerbaijan": "AZ", "bahrain": "BH", "bangladesh": "BD", "belarus": "BY",
    "belgium": "BE", "bolivia": "BO", "bolivia (plurinational state of)": "BO",
    "bosnia and herzegovina": "BA", "botswana": "BW", "brazil": "BR",
    "bulgaria": "BG", "cambodia": "KH", "cameroon": "CM", "canada": "CA",
    "chile": "CL", "china": "CN", "colombia": "CO", "costa rica": "CR",
    "croatia": "HR", "cuba": "CU", "czech republic": "CZ", "czechia": "CZ",
    "denmark": "DK", "dominican republic": "DO", "ecuador": "EC", "egypt": "EG",
    "el salvador": "SV", "ethiopia": "ET", "finland": "FI", "france": "FR",
    "georgia": "GE", "germany": "DE", "ghana": "GH", "greece": "GR",
    "guatemala": "GT", "honduras": "HN", "hungary": "HU", "india": "IN",
    "indonesia": "ID", "iran": "IR", "iraq": "IQ", "ireland": "IE",
    "israel": "IL", "italy": "IT", "ivory coast": "CI", "cote d'ivoire": "CI",
    "jamaica": "JM", "japan": "JP", "jordan": "JO", "kazakhstan": "KZ",
    "kenya": "KE", "kuwait": "KW", "kyrgyzstan": "KG", "laos": "LA",
    "latvia": "LV", "lebanon": "LB", "libya": "LY", "lithuania": "LT",
    "luxembourg": "LU", "malaysia": "MY", "mali": "ML", "mexico": "MX",
    "moldova": "MD", "morocco": "MA", "mozambique": "MZ", "myanmar": "MM",
    "namibia": "NA", "nepal": "NP", "netherlands": "NL", "new zealand": "NZ",
    "nicaragua": "NI", "nigeria": "NG", "north korea": "KP", "norway": "NO",
    "oman": "OM", "pakistan": "PK", "panama": "PA", "paraguay": "PY",
    "peru": "PE", "philippines": "PH", "poland": "PL", "portugal": "PT",
    "qatar": "QA", "romania": "RO", "russia": "RU", "russian federation": "RU",
    "saudi arabia": "SA", "senegal": "SN", "serbia": "RS", "singapore": "SG",
    "slovakia": "SK", "somalia": "SO", "south africa": "ZA",
    "south korea": "KR", "republic of korea": "KR", "spain": "ES",
    "sri lanka": "LK", "sudan": "SD", "sweden": "SE", "switzerland": "CH",
    "syria": "SY", "syrian arab republic": "SY", "taiwan": "TW",
    "tajikistan": "TJ", "tanzania": "TZ", "united republic of tanzania": "TZ",
    "thailand": "TH", "tunisia": "TN", "turkey": "TR", "turkmenistan": "TM",
    "ukraine": "UA", "united arab emirates": "AE", "united kingdom": "GB",
    "united states": "US", "usa": "US", "uruguay": "UY", "uzbekistan": "UZ",
    "venezuela": "VE", "venezuela (bolivarian republic of)": "VE",
    "vietnam": "VN", "viet nam": "VN", "yemen": "YE", "zambia": "ZM",
    "zimbabwe": "ZW",
}


def _country_code(name: str) -> str:
    """Return 2-letter ISO code for a country name, or uppercase 2-char fallback."""
    if not name:
        return ""
    s = name.strip()
    if len(s) == 2 and s.isupper():
        return s
    return _COUNTRY_CODES.get(s.lower(), s.upper()[:2])


def _abbrev_asset(s: str) -> str:
    """3-letter asset abbreviation.

    1 word  → first 3 chars          ('Amazon' → 'Ama')
    2 words → first 2 of w1 + 1 of w2 ('Aramco Form' → 'ArF')
    3+ words→ first letter of each    ('Zahab AI Form' → 'ZAF')
    """
    words = s.strip().split()
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:3]
    if len(words) == 2:
        return words[0][:2] + words[1][:1]
    return "".join(w[0] for w in words[:3])


def _parse_build_upload_form(form) -> tuple[str, str, list[dict]]:
    """Parse campaign builder form.

    Each campaign card may contain multiple ad rows (camp_N_ad_M_*).
    All ads from the same campaign share the same name → group into one CampaignDraft.
    """
    from datetime import date as _date

    country = (form.get("country") or "").strip()
    account_label = (form.get("account_label") or "").strip()
    tracking_link = (form.get("tracking_link") or "").strip()
    try:
        camp_count = int(form.get("camp_count") or 0)
    except (TypeError, ValueError):
        camp_count = 0

    slots: list[dict] = []
    ddmm = _date.today().strftime("%d.%m")
    country_val = _country_code(country)

    for idx in range(camp_count):
        targeting = (form.get(f"camp_{idx}_targeting") or "").strip()
        os_val    = (form.get(f"camp_{idx}_os") or "All").strip()
        age_val   = (form.get(f"camp_{idx}_age") or "35").strip()
        asset_val = _abbrev_asset((form.get(f"camp_{idx}_asset") or "").strip())
        budget    = (form.get(f"camp_{idx}_budget") or "").strip()
        name      = (form.get(f"camp_{idx}_name") or "").strip()

        try:
            ad_count = int(form.get(f"camp_{idx}_ad_count") or 1)
        except (TypeError, ValueError):
            ad_count = 1

        # Build auto-name from campaign-level fields if not set
        if not name:
            # Use first ad's creative type for name
            first_ctype = (form.get(f"camp_{idx}_ad_0_creative_type") or "Image").strip()
            name = "_".join(p for p in [country_val, asset_val, os_val, first_ctype, targeting, age_val, ddmm] if p)
        if not name:
            continue

        for a_idx in range(ad_count):
            creative_type = (form.get(f"camp_{idx}_ad_{a_idx}_creative_type") or "Image").strip()
            creative_file = (form.get(f"camp_{idx}_ad_{a_idx}_creative_file") or "").strip()
            copy          = (form.get(f"camp_{idx}_ad_{a_idx}_copy") or "").strip()

            # Legacy single-ad fallback (forms submitted before multi-ad upgrade)
            if not creative_file and a_idx == 0:
                creative_file = (form.get(f"camp_{idx}_creative_file") or "").strip()
            if not copy and a_idx == 0:
                copy = (form.get(f"camp_{idx}_copy") or "").strip()

            slots.append(
                {
                    "name": name,
                    "creative_type": creative_type,
                    "creative_id": (form.get(f"camp_{idx}_creative_id") or "").strip(),
                    "creative_file": creative_file,
                    "copy": copy,
                    "targeting": targeting,
                    "os": os_val,
                    "age": age_val,
                    "budget": budget,
                    "asset": asset_val,
                    "tracking_link": tracking_link,
                }
            )
    # Return the ISO code, not the raw form value ("Costa Rica"). Confirmed
    # live 2026-07-30: the full name leaked into draft.country, where both
    # the GCC/LatAm language rule and the Excel Countries lookup expect
    # 2-letter codes — so draft.language came out empty and the audience
    # Language step was silently SKIPPED on every dashboard run (while CLI
    # runs, always invoked with --country CR, kept setting it). Telemetry
    # made this visible: dashboard runs had no `languages` row at all.
    return country_val or country, account_label, slots


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


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return True
    return (request.query_params.get("format") or "").lower() == "json"


@app.post("/refresh")
async def refresh(request: Request) -> Response:
    """Rebuild report now (reuse Excel DF if mtime unchanged), then redirect or JSON.

    Runs in a worker thread so the event loop stays responsive (Stop / other
    requests keep working during Refresh). Prefer JSON via Accept: application/json
    for the dashboard button — then the client reloads a warm cache.

    Upload FB.xlsx ref caches are left alone — they already invalidate on mtime.
    """
    # Keep excel_df so force_rebuild skips re-parsing when the file is unchanged.
    cache_clear(keep_excel_df=True)
    try:
        await asyncio.to_thread(lambda: get_cached_report(force_rebuild=True))
    except (FileNotFoundError, ValueError) as e:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return RedirectResponse(url="/", status_code=303)

    if _wants_json(request):
        return JSONResponse({"ok": True, "built_at": cache_built_at()})
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


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


@app.get("/api/creatives")
async def api_creatives() -> JSONResponse:
    """Scan FB_CREATIVES_ROOT recursively and return all creative files.

    Returns [{filename, rel_path, folder, type}] sorted by filename.
    Used by Campaign Builder for direct file search (no Excel lookup).
    """
    root = Path(get_creatives_root())
    if not root.is_dir():
        return JSONResponse({"error": f"Creatives folder not found: {root}", "files": []})

    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in _IMAGE_EXTS:
            ftype = "Image"
        elif ext in _VIDEO_EXTS:
            ftype = "Video"
        else:
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = Path(p.name)
        folder = str(rel.parent) if rel.parent != Path(".") else ""
        files.append({
            "filename": p.name,
            "rel_path": str(rel).replace("\\", "/"),
            "folder": folder.replace("\\", "/"),
            "type": ftype,
        })
    return JSONResponse({"files": files})


@app.get("/api/ad-copy")
async def api_ad_copy() -> JSONResponse:
    """Return every "ad texts" row from the Google Sheet — canonical source for
    the Campaign Builder's Ad Title search (see src/web/ad_copy_gsheet.py; the
    old Excel "Ad copy" sheet is no longer used for this).

    Returns [{ad_title, headline, body, description, asset, language}].
    """
    from src.web.ad_copy_gsheet import load_ad_copy

    data = load_ad_copy()
    rows = [
        {"ad_title": title, **entry}
        for title, entry in data.items()
    ]
    return JSONResponse({"rows": rows})


@app.get("/api/resolve-targeting")
async def api_resolve_targeting(text: str = "") -> JSONResponse:
    """Resolve a free-text interest string (e.g. 'trade') to its canonical KB
    target (e.g. 'Trading') via config/knowledge_base.json's aliases[] — used by
    the Campaign Builder's "Send to Builder" popup to fix naming mismatches
    between how a real ad set/combo was named and the dashboard's own taxonomy.
    """
    return JSONResponse(resolve_interest_alias(text))


def normalise_ad_account_id(raw: str) -> str:
    """Accept whatever the operator has in hand and return `act_<digits>`.

    The only field still typed by hand when connecting an account, and the
    thing people have in front of them is rarely the bare id — it is the Ads
    Manager URL. So take the id out of anything that contains it:

        https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1234567890123456&…
        act_1234567890123456
        1234567890123456

    Returns "" when there is no plausible id, so the caller can say so instead
    of writing a broken row.

    Replaces `act.lstrip('act_')`, which strips any leading run of the
    characters a/c/t/_ rather than the prefix — wrong for ids that happen to
    start with those characters, and no help at all with a URL.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    m = re.search(r"act[=_](\d{6,})", text) or re.search(r"(?<!\d)(\d{8,})(?!\d)", text)
    return f"act_{m.group(1)}" if m else ""


@app.get("/api/buyers")
async def api_buyers() -> JSONResponse:
    """The campaign managers present in the stats, and who is selected here.

    The list comes from the workbook's own CM column, so a new buyer shows up
    as soon as their first row lands — nothing to configure.
    """
    from src.web.buyer import default_cm, get_selected_cm, known_cms

    try:
        _report, df, _warnings = get_cached_report()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc), "buyers": []}, status_code=200)

    return JSONResponse({
        "ok": True,
        "buyers": known_cms(df),
        "selected": get_selected_cm(),
        "default": default_cm(),
    })


@app.post("/api/buyers")
async def api_select_buyer(request: Request) -> JSONResponse:
    """Switch whose dashboard is shown. Body JSON: {name}.

    Switching reuses the parsed workbook and only reassembles the report, so
    it costs seconds rather than a cold start.
    """
    from src.web.buyer import set_selected_cm

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    selected = set_selected_cm(data.get("name") or "")
    try:
        get_cached_report(my_cm=selected)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc), "selected": selected}, status_code=200)
    return JSONResponse({"ok": True, "selected": selected})


@app.get("/api/accounts-overview")
async def api_accounts_overview() -> JSONResponse:
    """Every account this buyer has, and whether this machine can reach it.

    Two halves that were never shown together: the shared sheet says which
    accounts exist and what state they are in; the machine-local map says how
    to reach them (ad account id, Multilogin profile). An account can be
    perfectly healthy in the sheet and unusable here because the local map has
    no profile for it — which is exactly the case the operator needs to see.
    """
    from src.analysis_agent.account_country import (
        country_from_stats,
        primary_country,
        resolve_countries,
    )
    from src.analysis_agent.analyze_fb_stats import normalize_account_alias
    from src.analysis_agent.gsheet_accounts import fetch_account_rows
    from src.fb_ads_rpa.mlx_profiles import (
        ad_account_id_for,
        folder_id_for,
        open_profiles_detailed,
        profile_name_key,
    )
    from src.web.buyer import get_selected_cm

    map_path = get_ad_account_map_path()
    account_map = load_ad_account_map(map_path) if os.path.isfile(map_path) else {}

    try:
        sheet_rows = fetch_account_rows()
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        return JSONResponse(
            {"ok": False, "error": f"Не удалось прочитать таблицу аккаунтов: {exc}",
             "accounts": [], "open_profiles": [], "map_path": map_path},
            status_code=200,
        )

    # Ownership comes from the STATS workbook's CM column, not from the
    # accounts sheet — that sheet has no owner column, so its whole tab is
    # attributed to one name and every buyer would see the same list.
    # Accounts that never appear in the stats keep an empty owner and stay
    # visible: hiding one would make it impossible to connect.
    owner_by_alias: dict[str, str] = {}
    try:
        _r, stats_df, _w = get_cached_report()
        if stats_df is not None and {"Account Alias", "CM"} <= set(stats_df.columns):
            pairs = stats_df[["Account Alias", "CM"]].fillna("").astype(str)
            counts: dict[str, dict[str, int]] = {}
            for alias, cm in pairs.itertuples(index=False):
                key, name = normalize_account_alias(alias), cm.strip()
                if not key or not name:
                    continue
                counts.setdefault(key, {})[name] = counts.setdefault(key, {}).get(name, 0) + 1
            # A handful of accounts changed hands and carry two managers; the
            # one with more rows is the current owner.
            owner_by_alias = {k: max(v, key=v.get) for k, v in counts.items()}
    except Exception:
        owner_by_alias = {}

    # Which country an account belongs to. Three sources, and the order between
    # them matters more than any of them:
    #
    #   1. the country SECTION of the accounts sheet — the operator maintains
    #      it, and it is the only statement of intent we have;
    #   2. failing that, the account's own recent stats — by LATEST DATE, not by
    #      how many rows each country has;
    #   3. never the suffix in the account label.
    #
    # The suffix ("… CL ML", "… PA Amz") looks authoritative and is not: it is
    # typed by hand, sometimes wrong, and accounts move between countries while
    # the name stays. Reading a country off it produced seven "mismatches" out
    # of twenty-four accounts on 2026-08-30 — every one of them the label being
    # stale, not the account being wrong.
    #
    # Latest date rather than most frequent, because an account that moved has
    # more history in the country it LEFT. FacebookElf303 has 46 Chile rows and
    # 4 Panama, and the newest day is mostly Panama: counting rows answers
    # "where has it been", and the question is "where is it now".
    # Country per account. The rule and its reasons live in
    # analysis_agent/account_country.py — the short version: the sheet's country
    # section wins, recent stats fill the gaps, and the suffix in the label is
    # never a source.
    country_by_alias: dict[str, str] = {}
    try:
        _r2, stats_df2, _w2 = get_cached_report()
        if stats_df2 is not None and {"Account Alias", "Country"} <= set(stats_df2.columns):
            has_date = "Date" in stats_df2.columns
            wanted = ["Account Alias", "Country"] + (["Date"] if has_date else [])
            frame = stats_df2[wanted]
            if has_date:
                frame = frame.sort_values("Date", kind="stable")
            country_by_alias = country_from_stats(list(frame.itertuples(index=False)))
    except Exception:
        country_by_alias = {}
    _resolved = resolve_countries(country_by_alias, sheet_rows)
    country_by_alias = primary_country(_resolved)

    selected_cm = (get_selected_cm() or "").strip()
    open_detail = open_profiles_detailed()
    open_ids = [p["profile_id"] for p in open_detail]
    # Profiles already spoken for, so the picker can say so instead of letting
    # one profile be attached to two accounts.
    taken = {
        (getattr(e, "profile_id", "") or "").strip(): label
        for label, e in account_map.items()
        if (getattr(e, "profile_id", "") or "").strip()
    }

    accounts = []
    for row in sheet_rows:
        label = (row.get("account_label") or row.get("alias") or "").strip()
        entry = resolve_account_entry(account_map, label) if label else None
        ad_id = (getattr(entry, "ad_account_id", "") or "").strip() if entry else ""
        profile_id = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
        missing = [n for n, v in (("ad_account_id", ad_id), ("profile_id", profile_id)) if not v]
        owner = owner_by_alias.get(normalize_account_alias(label), "")
        if selected_cm and owner and owner != selected_cm:
            continue
        accounts.append({
            "label": label,
            "owner": owner,
            "country_name": country_by_alias.get(normalize_account_alias(label), ""),
            "country": row.get("country_code") or "",
            "stock": row.get("stock") or "",
            "status": row.get("account_status") or "",
            "ad_account_id": ad_id,
            "profile_id": profile_id,
            "profile_open": profile_id in open_ids,
            "missing": missing,
            "connected": not missing,
        })
    # Accounts this machine knows but the sheet does not. Someone added them
    # here on purpose — leaving them out would make them impossible to see,
    # edit or disconnect from the only screen that manages the map.
    listed = {a["label"] for a in accounts}
    for label, entry in account_map.items():
        label = (label or "").strip()
        if not label or label in listed:
            continue
        owner = owner_by_alias.get(normalize_account_alias(label), "")
        if selected_cm and owner and owner != selected_cm:
            continue
        ad_id = (getattr(entry, "ad_account_id", "") or "").strip()
        profile_id = (getattr(entry, "profile_id", "") or "").strip()
        missing = [n for n, v in (("ad_account_id", ad_id), ("profile_id", profile_id)) if not v]
        accounts.append({
            "label": label,
            "owner": owner,
            "country_name": country_by_alias.get(normalize_account_alias(label), ""),
            "country": (getattr(entry, "country", "") or ""),
            "stock": "",
            "status": "",
            "ad_account_id": ad_id,
            "profile_id": profile_id,
            "profile_open": profile_id in open_ids,
            "missing": missing,
            "connected": not missing,
            "local_only": True,
        })

    # Suggest a profile for rows that have none: "KLD_BFL_W27" and
    # "FacebookMKTOBFLW27 SV Ait" are plainly the same account, and matching
    # them saves the operator from choosing among identical-looking ids.
    # Only when exactly ONE open profile matches — two accounts can share a
    # key ("FacebookElf191 SA" / "FacebookElf191 PE"), and guessing there
    # would attach a campaign to the wrong browser.
    by_key: dict[str, list[str]] = {}
    for p in open_detail:
        k = profile_name_key(p.get("name") or "")
        if k:
            by_key.setdefault(k, []).append(p["profile_id"])
    for a in accounts:
        a["suggested_profile"] = ""
        if a["profile_id"]:
            continue
        k = profile_name_key((a["label"] or "").split(" ")[0])
        hits = by_key.get(k, []) if k else []
        if len(hits) == 1:
            a["suggested_profile"] = hits[0]

    accounts.sort(key=lambda a: (a["connected"], a["label"].lower()))

    # Accounts this buyer actually runs (they appear in the stats) but which
    # are neither in the sheet tab nor in this machine's map. These are the
    # names worth suggesting when adding by hand: typing a label that matches
    # nothing joins to nothing, which is how the map and the sheet drifted
    # apart in the first place.
    listed_norm = {normalize_account_alias(a["label"]) for a in accounts}
    suggestions = sorted(
        {
            alias
            for alias, owner in owner_by_alias.items()
            if alias and alias not in listed_norm
            and (not selected_cm or owner == selected_cm)
        }
    )

    return JSONResponse({
        "ok": True,
        "map_path": map_path,
        "accounts": accounts,
        "suggestions": suggestions,
        # folder_id comes off the profile's own path, so the operator never
        # has to find or type it.
        "open_profiles": [
            {
                "profile_id": p["profile_id"],
                # The Multilogin profile NAME, read off the browser window
                # title — the only place it is available, since the profile
                # API answers 404/403 with the token we have. A dropdown of
                # bare UUIDs told the operator nothing.
                "name": p.get("name") or "",
                "key": profile_name_key(p.get("name") or ""),
                "folder_id": folder_id_for(p["profile_id"]),
                # The ad account this profile actually works in, read from its
                # own browser history — the last field a person had to copy.
                "ad_account_id": ad_account_id_for(p["profile_id"]),
                "taken_by": taken.get(p["profile_id"], ""),
            }
            for p in open_detail
        ],
    })


@app.get("/api/targeting-profiles")
async def api_targeting_profiles() -> JSONResponse:
    """The shared targeting library, as every buyer sees it.

    One tab in one spreadsheet is the source of truth for the whole team, so
    what this returns is identical on every machine — that is what makes a
    profile added here immediately usable by everyone.
    """
    from src.analysis_agent.gsheet_targeting import load_targeting_profiles
    from src.analysis_agent.gsheet_write import is_authorised

    profiles = load_targeting_profiles()
    return JSONResponse({
        "ok": True,
        # Adding requires the machine's Google write token. Report it up front
        # so the UI can explain why the form is unavailable instead of failing
        # only once someone has typed a profile.
        "can_add": is_authorised(),
        "profiles": [
            {"name": p.name, "interests": p.interests, "narrow": p.narrow}
            for p in sorted(profiles.values(), key=lambda x: x.name.lower())
        ],
    })


@app.post("/api/targeting-profiles")
async def api_add_targeting_profile(request: Request) -> JSONResponse:
    """Add one profile to the shared library.

    Body JSON: {name, interests, narrow?}
    """
    from src.analysis_agent.gsheet_targeting import (
        TargetingWriteError,
        add_targeting_profile,
    )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    try:
        profile = add_targeting_profile(
            data.get("name") or "",
            data.get("interests") or "",
            data.get("narrow") or "",
        )
    except TargetingWriteError as exc:
        # Operator-actionable: wrong input, duplicate name, or missing consent.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse({
        "ok": True,
        "profile": {
            "name": profile.name,
            "interests": profile.interests,
            "narrow": profile.narrow,
        },
    })


@app.get("/api/parse-adset")
async def api_parse_adset(name: str = "") -> JSONResponse:
    """Parse a raw Meta ad set name into (country_code, device, age, targeting)
    and resolve its interest token via the same KB alias lookup as
    /api/resolve-targeting. Used for the "Send to Builder" popup when the only
    field available is a real ad set's free-text name (CM Comparison drill).
    """
    return JSONResponse(parse_adset_for_builder(name))


@app.get("/creative-file")
async def creative_file(path: str = "") -> Response:
    """Serve creative bytes (image/video) for the Campaign Builder's visual
    ad preview — the only route that returns actual file content; /api/creatives
    only lists metadata. `path` is whatever the client already knows (a
    rel_path from /api/creatives, or a bare filename) and is resolved the same
    root-scoped way publish-time enrichment does, so it can never escape
    get_creatives_root().
    """
    if not path.strip():
        return JSONResponse({"error": "path is required"}, status_code=400)
    try:
        resolved = _find_creative_file_scoped(get_creatives_root(), path)
    except CreativeNotFoundError:
        return JSONResponse({"error": "Creative not found"}, status_code=404)
    except AmbiguousCreativeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return FileResponse(resolved)


@app.post("/account-map-patch")
async def account_map_patch(request: Request) -> JSONResponse:
    """Upsert a row in config/ad_account_map.csv with the provided account fields.

    Body JSON: {account_name, ad_account_id, folder_id, profile_id, page_id?, multilogin_instance?}
    Creates the CSV if it doesn't exist yet.
    """
    import csv as _csv

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    account_name = (data.get("account_name") or "").strip()
    if not account_name:
        return JSONResponse({"ok": False, "error": "account_name is required"}, status_code=400)

    map_path = Path(get_ad_account_map_path())
    _FIELDNAMES = [
        "account_name", "ad_account_id", "page_id", "multilogin_instance",
        "folder_id", "profile_id", "proxy_type", "proxy_host", "proxy_port",
        "proxy_user", "proxy_pass", "country", "page_name",
    ]

    try:
        # Read existing rows
        rows: list[dict] = []
        if map_path.is_file():
            with open(map_path, encoding="utf-8-sig", newline="") as f:
                reader = _csv.DictReader(f)
                rows = list(reader)

        # Upsert: find existing row by account_name (case-insensitive) or append
        name_lower = account_name.lower()
        existing_idx: int | None = None
        for i, row in enumerate(rows):
            if (row.get("account_name") or "").strip().lower() == name_lower:
                existing_idx = i
                break

        new_row: dict = {f: "" for f in _FIELDNAMES}
        if existing_idx is not None:
            new_row.update(rows[existing_idx])

        new_row["account_name"] = account_name
        for field in ("ad_account_id", "folder_id", "profile_id", "page_id", "multilogin_instance", "country", "page_name"):
            val = (data.get(field) or "").strip()
            if val:
                # Uppercase two-letter country CODES ("cl" -> "CL"), but leave
                # full names alone: the dashboard now offers them from the
                # sheet ("Chile"), and "CHILE" would sit oddly beside them.
                if field == "country" and len(val) == 2 and val.isalpha():
                    val = val.upper()
                new_row[field] = val

        # Accept a pasted Ads Manager URL, a bare number, or act_<digits>.
        #
        # Only what the caller actually sent is validated. This used to check
        # whatever ended up in the row, including a value carried over from the
        # file — so connecting a Multilogin profile to an account whose stored
        # id was malformed failed with an error about the cabinet, which the
        # caller had not mentioned and could not fix from that screen. Validate
        # input, not storage.
        act_raw = (data.get("ad_account_id") or "").strip()
        if act_raw:
            act = normalise_ad_account_id(act_raw)
            if not act:
                return JSONResponse(
                    {"ok": False,
                     "error": "Не удалось разобрать ad_account_id. Вставьте ссылку из "
                              "Ads Manager, либо номер кабинета."},
                    status_code=400,
                )
            # A second account pointing at the same ad account is almost always
            # a paste into the wrong row, and it would send campaigns somewhere
            # nobody intended.
            clash = next(
                (r.get("account_name") for i, r in enumerate(rows)
                 if i != existing_idx and (r.get("ad_account_id") or "").strip() == act),
                None,
            )
            if clash:
                return JSONResponse(
                    {"ok": False,
                     "error": f"Этот кабинет уже привязан к «{clash}». "
                              f"Проверьте, туда ли вставили."},
                    status_code=400,
                )
            new_row["ad_account_id"] = act

        if existing_idx is not None:
            rows[existing_idx] = new_row
        else:
            rows.append(new_row)

        map_path.parent.mkdir(parents=True, exist_ok=True)
        with open(map_path, "w", encoding="utf-8", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        # Same class of bug fixed in /account-link-patch below: an unhandled
        # exception here previously made FastAPI's default 500 come back as
        # plain text, not JSON, so the dashboard's r.json() threw and showed
        # a misleading "Network error" instead of "file is open elsewhere".
        return JSONResponse(
            {"ok": False, "error": f"{map_path.name} is open elsewhere (e.g. Excel) — close it and retry"},
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Failed to save: {exc}"}, status_code=500)

    return JSONResponse({"ok": True, "saved": new_row["account_name"]})


@app.post("/account-map-remove")
async def account_map_remove(request: Request) -> JSONResponse:
    """Disconnect an account from THIS machine. Body JSON: {account_name}.

    Removes the row from the machine-local map only. The account keeps
    existing in the company's sheet, keeps its history and its campaigns —
    this machine simply stops knowing how to reach it. Nothing here can or
    should delete a shared record.
    """
    import csv as _csv

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    # One name or many. Removing several in a single request rewrites the file
    # once instead of N times: fewer chances of leaving the map half-edited.
    names = data.get("account_names")
    if isinstance(names, list):
        wanted = [str(n).strip() for n in names if str(n).strip()]
    else:
        one = (data.get("account_name") or "").strip()
        wanted = [one] if one else []

    if not wanted:
        return JSONResponse(
            {"ok": False, "error": "account_name или account_names обязательны"},
            status_code=400,
        )

    map_path = Path(get_ad_account_map_path())
    if not map_path.is_file():
        return JSONResponse({"ok": False, "error": "Карта аккаунтов не найдена"}, status_code=404)

    try:
        with open(map_path, encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)

        target = set(wanted)
        kept = [r for r in rows if (r.get("account_name") or "").strip() not in target]
        removed = [
            (r.get("account_name") or "").strip() for r in rows
            if (r.get("account_name") or "").strip() in target
        ]
        if not removed:
            shown = "«" + "», «".join(wanted[:3]) + "»"
            return JSONResponse(
                {"ok": False, "error": f"{shown} нет в карте этой машины"},
                status_code=404,
            )

        with open(map_path, "w", encoding="utf-8", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(kept)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # `missing` is reported rather than treated as failure: the rest were
    # removed, and pretending otherwise would invite a confused retry.
    missing = sorted(set(wanted) - set(removed))
    return JSONResponse(
        {"ok": True, "removed": removed, "missing": missing, "left": len(kept)}
    )


@app.post("/account-link-patch")
async def account_link_patch(request: Request) -> JSONResponse:
    """Write a tracking link directly to Upload FB.xlsx → Accounts sheet → Link column.

    Body JSON: {account_name: str, link: str}
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    account_name = (data.get("account_name") or "").strip()
    link = (data.get("link") or "").strip()
    if not account_name:
        return JSONResponse({"ok": False, "error": "account_name is required"}, status_code=400)

    try:
        matched = _write_link_to_excel(account_name, link)
    except PermissionError:
        # openpyxl's wb.save() raises this when Upload FB.xlsx is open in Excel
        # (or another program) elsewhere -- previously unhandled, which made
        # FastAPI's default 500 response come back as plain text instead of
        # JSON, so the dashboard's fetch().then(r => r.json()) threw and the
        # UI showed a misleading "Network error" instead of the real cause.
        return JSONResponse(
            {"ok": False, "error": "Upload FB.xlsx is open elsewhere (e.g. Excel) — close it and retry"},
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Failed to save link: {exc}"}, status_code=500)
    if matched is None:
        excel_path = get_upload_fb_excel_path()
        if not os.path.isfile(excel_path):
            return JSONResponse({"ok": False, "error": "Upload FB.xlsx not found"}, status_code=404)
        return JSONResponse({"ok": False, "error": f"Account '{account_name}' not found in Accounts sheet"}, status_code=404)

    status = classify_link(link)
    return JSONResponse({"ok": True, "account": matched, "link_status": status})


@app.post("/rpa-takeover")
async def rpa_takeover(request: Request) -> JSONResponse:
    """Pause or resume the RPA. Body: {action: 'pause'|'resume'}"""
    from src.fb_ads_rpa.pacing import request_takeover, release_takeover, is_takeover_active
    try:
        data = await request.json()
    except Exception:
        data = {}
    action = (data.get("action") or "pause").lower()
    if action == "pause":
        request_takeover()
        return JSONResponse({"ok": True, "status": "paused"})
    else:
        release_takeover()
        return JSONResponse({"ok": True, "status": "resumed"})


@app.get("/rpa-takeover")
async def rpa_takeover_status() -> JSONResponse:
    from src.fb_ads_rpa.pacing import is_takeover_active, get_teach_state
    teach = get_teach_state()
    return JSONResponse({"active": is_takeover_active(), "teach": teach})


@app.post("/rpa-teach/resume")
async def rpa_teach_resume() -> JSONResponse:
    """Tell the RPA that the user has clicked Website manually — resume the flow."""
    from src.fb_ads_rpa.pacing import end_teach_wait, get_teach_state
    state = get_teach_state()
    if not state.get("waiting"):
        return JSONResponse({"ok": False, "error": "Not in teach-mode wait"}, status_code=400)
    end_teach_wait()
    return JSONResponse({"ok": True, "message": "RPA resumed — capturing locator"})


@app.get("/rpa-teach/status")
async def rpa_teach_status() -> JSONResponse:
    from src.fb_ads_rpa.pacing import get_teach_state
    return JSONResponse(get_teach_state())


@app.get("/rpa-teach/pending-strategies")
async def rpa_teach_pending_strategies() -> JSONResponse:
    from src.fb_ads_rpa.variations.ranks import StrategyRanks

    ranks = StrategyRanks.load()
    return JSONResponse(ranks.list_pending())


@app.post("/rpa-teach/approve-strategy")
async def rpa_teach_approve_strategy(request: Request) -> JSONResponse:
    body = await request.json()
    step_id = str(body.get("step_id") or "")
    strategy_id = str(body.get("strategy_id") or "")
    from src.fb_ads_rpa.variations.ranks import StrategyRanks

    ranks = StrategyRanks.load()
    ok = ranks.approve_pending(step_id, strategy_id)
    if ok:
        ranks.save()
    return JSONResponse({"ok": ok})


@app.post("/rpa-record/start")
async def rpa_record_start(request: Request) -> JSONResponse:
    """Start click recorder on an already-open Multilogin CDP browser.

    Body JSON: {recipe_name?: str, cdp_url?: str, account_label?: str}
    """
    from src.fb_ads_rpa.click_recorder import start_recording
    try:
        data = await request.json()
    except Exception:
        data = {}
    recipe_name = (data.get("recipe_name") or "campaign").strip()
    cdp_url = (data.get("cdp_url") or "").strip() or None
    account_label = (data.get("account_label") or "").strip() or None
    result = await asyncio.to_thread(
        start_recording, recipe_name=recipe_name, cdp_url=cdp_url, account_label=account_label
    )
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.post("/rpa-record/stop")
async def rpa_record_stop() -> JSONResponse:
    from src.fb_ads_rpa.click_recorder import stop_recording
    result = await asyncio.to_thread(stop_recording)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.get("/rpa-record/status")
async def rpa_record_status() -> JSONResponse:
    from src.fb_ads_rpa.click_recorder import get_status
    return JSONResponse(get_status())


@app.get("/rpa-record/recipes")
async def rpa_record_recipes() -> JSONResponse:
    from src.fb_ads_rpa.click_recorder import list_recipes
    return JSONResponse({"ok": True, "recipes": list_recipes()})


@app.post("/rpa-codegen/start")
async def rpa_codegen_start(request: Request) -> JSONResponse:
    """Open the Playwright Inspector (codegen) on an already-open Multilogin
    CDP browser — same window, no fresh login. See
    docs/agent10-codegen-via-multilogin.md.

    Body JSON: {cdp_url?: str, account_label?: str}
    """
    from src.fb_ads_rpa.codegen_recorder import start_codegen
    try:
        data = await request.json()
    except Exception:
        data = {}
    cdp_url = (data.get("cdp_url") or "").strip() or None
    account_label = (data.get("account_label") or "").strip() or None
    result = await asyncio.to_thread(start_codegen, cdp_url=cdp_url, account_label=account_label)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.post("/rpa-codegen/stop")
async def rpa_codegen_stop() -> JSONResponse:
    from src.fb_ads_rpa.codegen_recorder import stop_codegen
    result = await asyncio.to_thread(stop_codegen)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.get("/rpa-codegen/status")
async def rpa_codegen_status() -> JSONResponse:
    from src.fb_ads_rpa.codegen_recorder import get_status
    return JSONResponse(get_status())


@app.post("/rpa-website-lab/prepare")
async def rpa_website_lab_prepare(request: Request) -> JSONResponse:
    """Connect CDP + open Conversion location → Single so Website is visible."""
    from src.fb_ads_rpa.website_click_lab import prepare, connect
    try:
        data = await request.json()
    except Exception:
        data = {}
    cdp_url = (data.get("cdp_url") or "").strip() or None
    if cdp_url:
        conn = await asyncio.to_thread(connect, cdp_url=cdp_url)
        if not conn.get("ok") and not conn.get("connected"):
            return JSONResponse(conn, status_code=400)
    result = await asyncio.to_thread(prepare)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/rpa-website-lab/step")
async def rpa_website_lab_step(request: Request) -> JSONResponse:
    """Run one strategy step: A|B|C|D|E|F."""
    from src.fb_ads_rpa.website_click_lab import run_step
    try:
        data = await request.json()
    except Exception:
        data = {}
    step = (data.get("step") or "").strip().upper()
    result = await asyncio.to_thread(run_step, step)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/rpa-website-lab/run-all")
async def rpa_website_lab_run_all(request: Request) -> JSONResponse:
    """Live flow: prepare then A→B→F→C→D→E until Website is selected."""
    from src.fb_ads_rpa.website_click_lab import run_all
    try:
        data = await request.json()
    except Exception:
        data = {}
    cdp_url = (data.get("cdp_url") or "").strip() or None
    stop = data.get("stop_on_success", True)
    result = await asyncio.to_thread(run_all, stop_on_success=bool(stop), cdp_url=cdp_url)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/rpa-website-lab/status")
async def rpa_website_lab_status() -> JSONResponse:
    from src.fb_ads_rpa.website_click_lab import get_status
    return JSONResponse(get_status())


@app.post("/rpa-website-lab/disconnect")
async def rpa_website_lab_disconnect() -> JSONResponse:
    from src.fb_ads_rpa.website_click_lab import disconnect
    result = await asyncio.to_thread(disconnect)
    return JSONResponse(result)


@app.post("/preview-publish")
async def preview_publish(request: Request) -> JSONResponse:
    """Dry-run: convert form slots → PublishJob plan, return JSON (no Meta API calls)."""
    form = await request.form()
    country, account_label, slots = _parse_build_upload_form(form)
    if not country:
        return JSONResponse({"error": "Country not set — check form."}, status_code=400)
    if not account_label:
        return JSONResponse({"error": "Account label not set — check form."}, status_code=400)
    if not slots:
        return JSONResponse({"error": "No campaign slots found. Make sure the form has at least one campaign."}, status_code=400)
    try:
        if use_rpa_publish():
            pub = RpaPublisher(dry_run=True)
            drafts = pub.build_drafts(
                slots,
                account_label=account_label,
                country=country,
                preview_mode=True,
            )
            plan = preview_campaign_drafts(drafts)
        else:
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
    dry_run = (form.get("dry_run") or "0").strip() in ("1", "true", "yes")
    hand_off = (form.get("hand_off") or "0").strip() in ("1", "true", "yes")
    user_confirmed = (form.get("user_confirmed") or "0").strip() in ("1", "true", "yes")
    if not user_confirmed:
        return JSONResponse(
            {"error": "Publish blocked: confirm in the dashboard UI first (user_confirmed=0)."},
            status_code=403,
        )
    if not country or not account_label or not slots:
        return JSONResponse({"error": "Missing country, account or slots."}, status_code=400)
    job_id = start_job(
        account_label, country, slots,
        via_multilogin=via_ml,
        activate=activate,
        dry_run=dry_run,
        hand_off=hand_off,
    )
    if use_rpa_publish():
        from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

        total = len(
            slots_to_campaign_drafts(
                slots,
                account_label=account_label,
                country=country,
                activate=activate,
            )
        )
    else:
        total = len(slots)
    return JSONResponse({"job_id": job_id, "total": total})


@app.post("/publish-jobs/{job_id}/cancel")
async def publish_jobs_cancel(job_id: str) -> JSONResponse:
    """Stop a running publish/RPA job; Multilogin window stays open."""
    result = cancel_job(job_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404 if result.get("error") == "Job not found" else 400)
    return JSONResponse(result)


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

    # The parser returns the country CODE ("MX"), which the RPA needs — without
    # it the audience Language step was silently skipped. But the template looks
    # errors up by the country NAME it renders the section under ("Mexico"), so
    # keying them by the code meant no error was EVER displayed: a build that
    # failed validation came back as the plain report page with nothing to say
    # why. Key by what the reader's section is keyed by.
    err_key = (form.get("country") or "").strip() or country

    if not country or not account_label:
        return _render_report(
            request,
            upload_errors={"": {"": ["Missing country or account."]}},
        )
    if not slots:
        return _render_report(
            request,
            upload_errors={err_key: {account_label: ["Add at least one campaign slot."]}},
        )

    errors, xlsx_bytes, filename = assemble_from_form_slots(
        country=country,
        account_label=account_label,
        slots=slots,
    )
    if errors:
        return _render_report(
            request,
            upload_errors={err_key: {account_label: errors}},
        )

    return Response(
        content=xlsx_bytes,
        media_type=_MIME_XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.get("/multilogin-token-status")
async def multilogin_token_status(instance: str = "A") -> JSONResponse:
    """Is the saved automation token alive? (token-first connect, no re-login)

    Checks locally first (MLX automation tokens are JWTs — decode `exp`
    without a network call), then confirms with one light API request.
    Never touches credentials: the token either works or the client falls
    back to the login form.
    """
    import base64 as _b64
    import json as _json
    import time as _time

    import requests as _req

    instance = (instance or "A").strip().upper()
    if instance not in ("A", "B"):
        instance = "A"
    env_key = f"MULTILOGIN_AUTOMATION_TOKEN_{instance}"
    token = (os.environ.get(env_key) or "").strip()
    if not token:
        return JSONResponse({"ok": True, "has_token": False, "alive": False})

    expires_at: str | None = None
    email: str | None = None
    parts = token.split(".")
    if len(parts) == 3:
        try:
            payload_raw = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = _json.loads(_b64.urlsafe_b64decode(payload_raw))
            exp = payload.get("exp")
            email = payload.get("email") or payload.get("sub")
            if isinstance(exp, (int, float)):
                from datetime import datetime, timezone
                expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
                if exp < _time.time() + 60:
                    return JSONResponse({"ok": True, "has_token": True, "alive": False,
                                         "reason": "expired", "expires_at": expires_at})
        except Exception:
            pass  # not a decodable JWT — let the API call decide

    try:
        r = _req.get(
            "https://api.multilogin.com/workspace/folders",
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=12,
        )
    except Exception as exc:
        return JSONResponse({"ok": True, "has_token": True, "alive": False,
                             "reason": f"network: {exc}", "expires_at": expires_at})
    if r.status_code == 401:
        return JSONResponse({"ok": True, "has_token": True, "alive": False,
                             "reason": "expired", "expires_at": expires_at})
    return JSONResponse({"ok": True, "has_token": True, "alive": r.status_code == 200,
                         "expires_at": expires_at, "email": email})


@app.post("/multilogin-search")
async def multilogin_search(request: Request) -> JSONResponse:
    """Search ML profiles using saved automation token (no re-login needed).

    Body JSON: {search_text: str, instance?: 'A'|'B'}
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    search_text = (data.get("search_text") or "").strip()
    instance = (data.get("instance") or "A").strip().upper()
    if instance not in ("A", "B"):
        instance = "A"

    import requests as _req

    env_key = f"MULTILOGIN_AUTOMATION_TOKEN_{instance}"
    token = (os.environ.get(env_key) or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": f"{env_key} not set — sign in first to save a token"})

    MLX_API = "https://api.multilogin.com"
    H = {"Accept": "application/json", "Content-Type": "application/json",
         "Authorization": f"Bearer {token}"}

    folders: list[dict] = []
    try:
        rf = _req.get(f"{MLX_API}/workspace/folders", headers=H, timeout=15)
        if rf.status_code == 200:
            fd = rf.json().get("data") or {}
            raw = fd.get("folders") if isinstance(fd, dict) else fd
            folders = [
                {"folder_id": f.get("folderID") or f.get("folder_id") or f.get("id") or "",
                 "name": f.get("name") or f.get("folder_name") or ""}
                for f in (raw if isinstance(raw, list) else [])
            ]
    except Exception:
        pass

    profiles: list[dict] = []
    try:
        rp = _req.post(
            f"{MLX_API}/profile/search",
            headers=H,
            json={"limit": 100, "offset": 0, "search_text": search_text},
            timeout=15,
        )
        if rp.status_code == 401:
            return JSONResponse({"ok": False, "error": "Токен истёк — войди заново через Connect"})
        if rp.status_code == 200:
            pd = rp.json().get("data") or {}
            raw_p = pd.get("profiles") if isinstance(pd, dict) else pd
            profiles = [
                {
                    "profile_id": p.get("profile_id") or p.get("uuid") or p.get("id") or "",
                    "folder_id": p.get("folder_id") or p.get("folderID") or "",
                    "name": p.get("name") or p.get("profile_name") or "",
                }
                for p in (raw_p if isinstance(raw_p, list) else [])
                if (p.get("profile_id") or p.get("uuid") or p.get("id"))
            ]
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})

    return JSONResponse({"ok": True, "profiles": profiles, "folders": folders})


@app.post("/multilogin-start-direct")
async def multilogin_start_direct(request: Request) -> JSONResponse:
    """Disabled — use Campaign Builder → Preview → Publish (with confirmation modal)."""
    return JSONResponse(
        {
            "ok": False,
            "error": "Direct browser restart is disabled. Use Publish in Campaign Builder after confirming the modal.",
        },
        status_code=403,
    )


@app.post("/multilogin-save-token")
async def multilogin_save_token(request: Request) -> JSONResponse:
    """Save a manually pasted automation token to .env.

    Body JSON: {token: str, instance?: 'A'|'B'}
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"})

    token = (data.get("token") or "").strip()
    instance = (data.get("instance") or "A").strip().upper()
    if instance not in ("A", "B"):
        instance = "A"
    if not token:
        return JSONResponse({"ok": False, "error": "token is required"})

    env_key = f"MULTILOGIN_AUTOMATION_TOKEN_{instance}"
    env_path = _PROJ_ROOT / ".env"
    try:
        lines: list[str] = []
        if env_path.is_file():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{env_key}="):
                lines[i] = f"{env_key}={token}"
                updated = True
                break
        if not updated:
            lines.append(f"{env_key}={token}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ[env_key] = token
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Failed to write .env: {exc}"})

    return JSONResponse({"ok": True, "instance": instance, "env_key": env_key})


@app.post("/multilogin-signin")
async def multilogin_signin(request: Request) -> JSONResponse:
    """Sign in to Multilogin, return folders + profiles list.

    Body JSON: {email, password, save_token?: bool}
    Credentials are used for a single API call and never persisted.
    If save_token is true, updates MULTILOGIN_AUTOMATION_TOKEN_A in .env.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    save_token = bool(data.get("save_token", True))
    instance = (data.get("instance") or "A").strip().upper()
    if instance not in ("A", "B"):
        instance = "A"

    if not email or not password:
        return JSONResponse({"ok": False, "error": "email and password are required"}, status_code=400)

    import hashlib as _hashlib
    import requests as _req

    # Multilogin X API requires password to be MD5 hashed (per official docs)
    password_hash = _hashlib.md5(password.encode("utf-8")).hexdigest()

    MLX_API = "https://api.multilogin.com"
    H = {"Accept": "application/json", "Content-Type": "application/json"}

    # Step 1 — sign in
    try:
        r = _req.post(f"{MLX_API}/user/signin", headers=H, json={"email": email, "password": password_hash}, timeout=20)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Network error: {exc}"})

    if r.status_code != 200:
        body = r.json() if "application/json" in r.headers.get("content-type", "") else {}
        msg = (body.get("status") or {}).get("message") or r.text[:300]
        return JSONResponse({"ok": False, "error": f"Sign-in failed [{r.status_code}]: {msg}", "debug_body": body})

    bearer = ((r.json().get("data") or {}).get("token") or "").strip()
    if not bearer:
        return JSONResponse({"ok": False, "error": "Sign-in returned no token"})

    auth_h = {**H, "Authorization": f"Bearer {bearer}"}

    # Step 2 — get automation token (1 month)
    auto_token = ""
    try:
        r2 = _req.get(f"{MLX_API}/workspace/automation_token?expiration_period=1mo", headers=auth_h, timeout=20)
        if r2.status_code == 200:
            d2 = r2.json().get("data") or {}
            auto_token = d2.get("token") or d2.get("automation_token") or ""
    except Exception:
        pass

    working_token = auto_token or bearer

    # Step 3 — save token to .env
    env_key = f"MULTILOGIN_AUTOMATION_TOKEN_{instance}"
    if save_token and auto_token:
        # Update both .env file and live os.environ so /multilogin-search works immediately
        os.environ[env_key] = auto_token
        env_path = _PROJ_ROOT / ".env"
        try:
            if env_path.is_file():
                lines = env_path.read_text(encoding="utf-8").splitlines()
                updated = False
                for i, line in enumerate(lines):
                    if line.startswith(f"{env_key}="):
                        lines[i] = f"{env_key}={auto_token}"
                        updated = True
                        break
                if not updated:
                    lines.append(f"{env_key}={auto_token}")
                env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[multilogin-signin] Failed to write .env: {e}", file=sys.stderr)

    auth_h2 = {**H, "Authorization": f"Bearer {working_token}"}

    # Step 4 — list folders
    folders: list[dict] = []
    try:
        rf = _req.get(f"{MLX_API}/workspace/folders", headers=auth_h2, timeout=20)
        if rf.status_code == 200:
            fd = rf.json().get("data") or {}
            raw = fd.get("folders") if isinstance(fd, dict) else fd
            folders = [
                {"folder_id": f.get("folderID") or f.get("folder_id") or f.get("id") or "",
                 "name": f.get("name") or f.get("folder_name") or ""}
                for f in (raw if isinstance(raw, list) else [])
            ]
    except Exception:
        pass

    # Step 5 — list profiles (try multiple endpoints for sub-accounts)
    profiles: list[dict] = []
    profile_debug: list[str] = []

    def _extract_profiles(body: dict) -> list[dict]:
        data = body.get("data") or {}
        raw = data.get("profiles") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not raw and isinstance(data, dict):
            raw = data.get("items") or data.get("list") or []
        return [
            {
                "profile_id": p.get("profile_id") or p.get("uuid") or p.get("id") or "",
                "folder_id": p.get("folder_id") or p.get("folderID") or "",
                "name": p.get("name") or p.get("profile_name") or "",
            }
            for p in (raw if isinstance(raw, list) else [])
            if (p.get("profile_id") or p.get("uuid") or p.get("id"))
        ]

    # Try 1: POST /profile/search with empty string (workspace owners)
    try:
        rp = _req.post(
            f"{MLX_API}/profile/search",
            headers=auth_h2,
            json={"limit": 200, "offset": 0, "search_text": ""},
            timeout=20,
        )
        profile_debug.append(f"search(empty) -> {rp.status_code}")
        if rp.status_code == 200:
            profiles = _extract_profiles(rp.json())
    except Exception as exc:
        profile_debug.append(f"search(empty) -> err:{exc}")

    # Try 2: POST /profile/search with "facebook" (sub-accounts may need search term)
    if not profiles:
        try:
            rp2 = _req.post(
                f"{MLX_API}/profile/search",
                headers=auth_h2,
                json={"limit": 200, "offset": 0, "search_text": "facebook"},
                timeout=20,
            )
            profile_debug.append(f"search(facebook) -> {rp2.status_code}")
            if rp2.status_code == 200:
                profiles = _extract_profiles(rp2.json())
        except Exception as exc:
            profile_debug.append(f"search(facebook) -> err:{exc}")

    # Try 3: GET /profile (older endpoint, some MLX versions)
    if not profiles:
        try:
            rp3 = _req.get(f"{MLX_API}/profile", headers=auth_h2, timeout=20)
            profile_debug.append(f"GET /profile -> {rp3.status_code}: {rp3.text[:120]}")
            if rp3.status_code == 200:
                profiles = _extract_profiles(rp3.json())
        except Exception as exc:
            profile_debug.append(f"GET /profile -> err:{exc}")

    return JSONResponse({
        "ok": True,
        "instance": instance,
        "token_saved": save_token and bool(auto_token),
        "folders": folders,
        "profiles": profiles,
        "debug": profile_debug,
    })
