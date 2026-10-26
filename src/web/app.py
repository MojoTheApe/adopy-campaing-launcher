"""FastAPI app: interactive morning report dashboard.

Routes:
    GET  /              - render the full morning report as HTML
    GET  /details       - return one <tr><td colspan> drill fragment
    POST /refresh       - rebuild report (reuse Excel DF), JSON or redirect to /
    POST /build-upload  - §11 selections → validated Upload FB xlsx download
    GET  /api/ref-data  - JSON with ad copies, creatives, targetings from Upload FB.xlsx
"""
from __future__ import annotations

from src.analysis_agent.country_names import COUNTRY_CODES as _COUNTRY_CODES

import asyncio
import contextvars
import functools
import ipaddress
import json
import math
import os
import re as _re
import re
import sys
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from src.web import account_notes as _account_notes
from src.web import builder_drafts as _builder_drafts

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
from src.web.publish_jobs import DuplicateJobError, get_job, start_job, cancel_job
from src.web.cache import (
    background_build_state,
    background_progress_callback,
    cache_built_at,
    cache_clear,
    entry_source_key,
    forget_background_error,
    get_cached_report,
    peek_cached_report,
    start_background_build,
    verified_source_key,
)
from src.analysis_agent import bi_stats
from src.web import report_store
from src.web.request_limits import (
    BUILDER_FORM,
    DEFAULT_MAX_BODY,
    ROUTE_MAX_BODY,
    RequestBodyLimit,
    SafeStaticFiles,
    read_form,
)
from concurrent.futures import ThreadPoolExecutor
from src.analysis_agent.money_format import fmt_usd, fmt_currency, fmt_summary_amount
from src.analysis_agent import metric_columns
from src.web import period_compare
from src.creative_agent.creative_scanner import find_creative_file, scan_file, SUPPORTED_EXTS
from src.creative_agent.audience_builder import build_audience_profile
from src.creative_agent.stats_correlator import correlate
from src.creative_agent.vision_analyzer import analyze as vision_analyze
from src.creative_agent.prompt_generator import generate_brief
from src.upload_agent.build_upload_excel import load_reference_data
from src.campaign_bridge.open_rec_mapper import parse_creative_from_ad_title
from config.paths import (
    get_ad_account_map_path,
    get_creatives_root,
    get_upload_fb_excel_path,
    update_machine_env,
    write_csv_atomic,
)
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
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


_ref_opts_cache: dict | None = None
_ref_opts_mtime: float = -1.0
_ref_full_cache: dict | None = None
_ref_full_mtime: float = -1.0


def _save_link_to_machine_map(account_name: str, link: str) -> bool:
    """Store the tracking link locally; False means the account is not mapped.

    Rewrites the whole CSV rather than patching a line: the file is a dozen
    rows, and a half-written map is worse than a slow one.
    """
    import csv as _csv

    map_path = Path(get_ad_account_map_path())
    if not map_path.is_file():
        return False
    with open(map_path, newline="", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        return False
    if "tracking_link" not in fieldnames:
        fieldnames.append("tracking_link")

    name_lower = (account_name or "").strip().casefold()
    exact = [row for row in rows if (row.get("account_name") or row.get("account") or "").strip().casefold() == name_lower]
    candidates = exact or [row for row in rows if name_lower and
        (row.get("account_name") or row.get("account") or "").strip() and (
        name_lower in (row.get("account_name") or row.get("account") or "").casefold()
        or (row.get("account_name") or row.get("account") or "").strip().casefold() in name_lower)]
    if not candidates:
        return False
    if len(candidates) != 1:
        raise ValueError("Account name is ambiguous; use the complete account name")
    hit = candidates[0]
    hit["tracking_link"] = (link or "").strip()
    hit["tracking_link_set"] = "1"
    if "tracking_link_set" not in fieldnames:
        fieldnames.append("tracking_link_set")
    write_csv_atomic(map_path, fieldnames, rows)
    return True


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
        wb.close()
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
        wb.close()
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
        wb.close()
        return None

    ws.cell(row=matched_row, column=link_col).value = link.strip() or None
    # Save beside the workbook and replace only after openpyxl produced a
    # complete ZIP. A crash during wb.save() must not destroy the operator's
    # Upload FB source file.
    target = Path(excel_path)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=f".tmp{target.suffix}", dir=target.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        wb.save(temp_path)
        os.replace(temp_path, target)
    finally:
        wb.close()
        temp_path.unlink(missing_ok=True)

    # Invalidate ref caches so the next dashboard load picks up the new link
    global _ref_full_cache, _ref_full_mtime, _ref_opts_cache, _ref_opts_mtime
    _ref_full_cache = None
    _ref_full_mtime = -1.0
    _ref_opts_cache = None
    _ref_opts_mtime = -1.0

    return matched_name


def _load_draft_status() -> dict:
    """Load current build verdicts, including interrupted and unverified runs."""
    from src.web.publish_jobs import load_draft_status

    return load_draft_status()


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


def _my_goal_countries(report: dict | None, cm: str | None = None) -> list[str]:
    """Страны человека — стартовый список его целей, пока своих он не задал.

    Где у него есть хоть что-то в книге (спенд, лиды, депозиты), плюс страны,
    к которым он сам привязал аккаунты на карте этой машины. Чужие страны
    сюда не попадают: 30.09 у Дана стояли цели владельца (ACL-1452).

    `report` должен быть полным (за всю книгу), а не отчётом за период:
    панель и сохранение берут страны из одного источника, иначе первая
    правка сохранит не те страны, что человек видел. Отчёт берётся только
    если он про того же `cm`; карта связей этой машины — только для её
    хозяина (MY_CM_NAME): тимлид, правящий чужие цели, не дарит их своих
    стран.
    """
    report = report or {}
    from src.web.buyer import default_cm

    report_cm = (report.get("my_cm") or default_cm() or "").strip()
    who = (report_cm if cm is None else cm).strip()
    out: list[str] = []
    if who.casefold() == report_cm.casefold():
        for country in report.get("countries", []):
            totals = country.get("my_totals") or {}
            if any(float(totals.get(k) or 0) for k in ("spend", "leads", "deposits")):
                out.append((country.get("country") or "").strip())
    owner = default_cm()
    if who and owner and who.casefold() == owner.casefold():
        try:
            from src.web import account_links

            out += [v for v in account_links.all_values("country") if account_links.accounts_for("country", v)]
        except Exception:  # карта связей не должна ронять цели
            pass
    return [c for c in out if c]


def _build_goals_view(report: dict, seed_countries: list[str] | None = None) -> dict:
    """Each country's target next to what actually happened, ready to render.

    Progress is computed here rather than in the page so the compact strip and
    any future screen cannot drift apart — and so a country with a target but no
    data still appears, at zero, instead of vanishing on the day it matters most.
    """
    from src.web import goals as goals_store

    cm = (report.get("my_cm") or os.environ.get("MY_CM_NAME") or "").strip()
    actual: dict[str, dict] = {}
    for country in report.get("countries", []):
        name = (country.get("country") or "").strip()
        totals = country.get("my_totals") or {}
        for key in {name.casefold(), _country_code(name).casefold()}:
            if key:
                actual[key] = totals

    rows = []
    # Стартовые страны — из полного отчёта (seed_countries), а не из отчёта
    # за период: так их видит и сохраняет POST /goals (ACL-1452, ревью #453).
    if seed_countries is None:
        seed_countries = _my_goal_countries(report, cm)
    for goal in goals_store.for_cm(cm, seed_countries):
        name = (goal.get("country") or "").strip()
        got = actual.get(name.casefold()) or actual.get((goal.get("code") or "").casefold()) or {}
        target = int(goal.get("ftd") or 0)
        deposits = int(got.get("deposits") or 0)
        leads = int(got.get("leads") or 0)
        spend = float(got.get("spend") or 0.0)
        rows.append({
            **goal,
            "deposits": deposits,
            "leads": leads,
            "spend": spend,
            "cpa_actual": (spend / deposits) if deposits else None,
            "cr_actual": (deposits / leads * 100.0) if leads else None,
            "pct": min(100, round(deposits / target * 100)) if target else None,
        })

    rows.sort(key=lambda r: (r.get("market") or "", r.get("country") or ""))
    total_target = sum(int(r.get("ftd") or 0) for r in rows)
    total_deposits = sum(r["deposits"] for r in rows)
    return {
        "cm": cm,
        "rows": rows,
        "target": total_target,
        "deposits": total_deposits,
        "pct": min(100, round(total_deposits / total_target * 100)) if total_target else None,
        "team_lead": goals_store.is_team_lead(),
    }


def _workbench_goals(goals_view: dict) -> dict:
    """{страна (casefold): {ftd, cpa, cr}} — цели из My Goals; пустое поле = цели нет."""
    out: dict[str, dict] = {}
    for row in (goals_view or {}).get("rows", []):
        name = (row.get("country") or "").strip().casefold()
        if name:
            out[name] = {"ftd": row.get("ftd") or None, "cpa": row.get("cpa"), "cr": row.get("cr")}
    return out


def _workbook_dates(df) -> list[str]:
    """Every date the workbook actually has, as ISO strings.

    The calendar greys out everything else. Offering a full month when the file
    holds two days is an invitation to pick a day and conclude the accounts are
    dead — the data was never there, and the screen should say so before the
    click, not after.
    """
    try:
        import pandas as pd

        if df is None or "Date" not in getattr(df, "columns", []):
            return []
        # ACL-1399: спрашивается из цикла событий на каждый опрос «готово ли»;
        # книга в кэше не меняется, а разбор дат — ~0.2 с. Помним последнюю.
        memo = _DATES_MEMO[0]
        if memo is not None and memo[0]() is df:
            return list(memo[1])
        days = pd.to_datetime(df["Date"], errors="coerce").dt.date.dropna()
        dates = sorted({d.isoformat() for d in days})
        try:
            _DATES_MEMO[0] = (weakref.ref(df), tuple(dates))
        except TypeError:
            pass
        return dates
    except Exception:
        return []


_DATES_MEMO: list = [None]


def _month_to_date(dates: list[str]) -> tuple[str, str]:
    """Месяц по текущее число — от календаря, а не от последнего дня книги.

    Раньше считалось от книги: если файл отстал на два дня, период молча
    заканчивался позавчера. Выглядело аккуратно и было неправдой — «месяц по
    текущее число» показывал не по текущее число, а пресет «This month» в
    календаре не совпадал с умолчанием, хотя называется так же.

    Отставшая книга даёт в конце пустые дни — это видно в шапке по датам и
    честнее, чем тихо сдвинутая граница. Но если в текущем месяце книга не
    знает вообще ничего, дешборд открылся бы пустым, а причина была бы не
    видна: тогда откатываемся на её последний месяц.
    """
    from datetime import date as _date

    if not dates:
        return "", ""
    today = _date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    if any(start <= d <= end for d in dates):
        return start, end
    last = dates[-1]
    return last[:7] + "-01", last


def resolve_period(df, date_from: str, date_to: str, span: str) -> tuple[str, str]:
    """Какой период показывать: выбранный, «все даты» или умолчание.

    Одно место на весь дешборд — иначе шапка и карта аккаунтов рано или поздно
    начнут показывать разные месяцы и оба будут выглядеть правдой.
    """
    if span == "all":
        return "", ""
    if date_from or date_to:
        return date_from, date_to
    return _month_to_date(_workbook_dates(df))


#: отчёты за период — ключ (excel mtime, менеджер, период). Пересборка стоит
#: около секунды, и без кэша она случалась бы на каждом переключении вкладки.
_PERIOD_CACHE: dict[tuple, dict] = {}
_PERIOD_CACHE_MAX = 8
_PERIOD_LOCK = threading.Lock()


class PeriodReportError(RuntimeError):
    """A requested range could not be calculated; full totals cannot replace it."""


def report_for_period(report: dict, df, date_from: str, date_to: str) -> dict:
    """Тот же отчёт, посчитанный только по строкам внутри периода.

    Excel уже разобран и лежит в кэше — здесь пересобирается только сам отчёт.
    Без периода возвращается ровно то, что пришло: ни лишней работы, ни риска
    разойтись с общим кэшем.
    """
    if df is None or not (date_from or date_to):
        return report

    from src.web.buyer import get_selected_cm

    key = _period_key(report, date_from, date_to)
    # Callers outside the main cache may supply a standalone report. Without
    # a source revision it is safer to recalculate than to retain stale values.
    cacheable = report.get("_cache_revision") is not None
    hit = _PERIOD_CACHE.get(key) if cacheable else None
    if hit is not None:
        return hit

    rows = _filter_by_period(df, date_from, date_to)
    try:
        from src.analysis_agent.morning_service import load_morning_report

        scoped, _df, _warnings = load_morning_report(
            excel_path=report.get("source_excel"),
            my_cm=report.get("my_cm") or get_selected_cm(),
            enriched_df=rows, account_rows_cached=report.get("_account_rows")
        )
    except Exception as exc:
        raise PeriodReportError("Could not calculate the selected period. Refresh and try again.") from exc

    scoped["period_from"] = date_from
    scoped["period_to"] = date_to
    if cacheable:
        # Период теперь считают и фоновая сборка, и пул запросов (ACL-1399).
        with _PERIOD_LOCK:
            while len(_PERIOD_CACHE) >= _PERIOD_CACHE_MAX:
                _PERIOD_CACHE.pop(next(iter(_PERIOD_CACHE)))
            _PERIOD_CACHE[key] = scoped
    return scoped


def _period_key(report: dict, date_from: str, date_to: str) -> tuple:
    from src.web.buyer import get_selected_cm

    return (
        report.get("source_excel"),
        report.get("_cache_revision"),
        (report.get("my_cm") or get_selected_cm() or ""),
        date_from,
        date_to,
    )


def _period_cached(report: dict, date_from: str, date_to: str) -> bool:
    return (report.get("_cache_revision") is not None
            and _period_key(report, date_from, date_to) in _PERIOD_CACHE)


def _filter_by_period(df, date_from: str, date_to: str):
    """Rows inside [from, to]. Either end may be blank — then it is not bounded."""
    if df is None or not (date_from or date_to) or "Date" not in getattr(df, "columns", []):
        return df
    try:
        import pandas as pd

        days = pd.to_datetime(df["Date"], errors="coerce").dt.date
        mask = days.notna()
        if date_from:
            mask &= days >= pd.to_datetime(date_from).date()
        if date_to:
            mask &= days <= pd.to_datetime(date_to).date()
        return df[mask]
    except Exception:
        return df


def _build_account_map_view(
    report: dict,
    df=None,
    date_from: str = "",
    date_to: str = "",
    span: str = "",
) -> dict:
    """The account map on Home: every country, its accounts, and what they did.

    Countries come from the links store, not from the workbook, because the two
    answer different questions. The workbook says where money was spent; the
    links say where this team decided to work. A country that was opened
    yesterday has no spend yet and must still appear, and a country that was
    closed must stop appearing even while last week's numbers are still in the
    file.

    Numbers come from the selected statistics source, per account per country — leads, deposits,
    spend, CPL, CPA, CR are already computed there, so nothing is recalculated
    here and the map cannot disagree with the rest of the page.
    """
    from src.web import account_links

    try:
        account_links.seed_from_account_map()
    except Exception:  # pragma: no cover - a missing map must not blank the page
        pass

    # account performance per (country, account), straight from the report.
    #
    # Keyed by BOTH the full name and the two-letter code: the account map has
    # always stored codes ("CR"), the workbook and the report use full names
    # ("Costa Rica"), and matching on one of them alone silently produces a map
    # where every account says "no data" — which reads as a data problem rather
    # than as a join that missed.
    # Ничего не выбрано — это не «покажи всё». Пустой период молча складывал
    # каждый день книги в одно число, и никто этого не выбирал; месяц по
    # текущее число — тот ответ, который человек и имеет в виду. «Все даты»
    # остаются, но теперь их надо попросить (span=all).
    dates = _workbook_dates(df)
    if not date_from and not date_to and span != "all":
        date_from, date_to = _month_to_date(dates)

    period = _filter_by_period(df, date_from, date_to) if (date_from or date_to) else None

    per_country: dict[str, dict[str, dict]] = {}
    labels_in_report: set[str] = set()
    display_name: dict[str, str] = {}
    # «Что сделать» и «готов ли» жили в отдельной секции под картой, где тот
    # же аккаунт описывался второй раз — своими числами и своей формой
    # подключения. Числа там дублировались, форма была хуже той, что на карте
    # (просила номер кабинета руками вместо того, чтобы прочитать его у
    # профиля), а эти два поля не показывались больше нигде. Секция убрана,
    # поля переехали сюда.
    todo_by_country: dict[str, dict[str, dict]] = {}
    for country in report.get("countries", []):
        name = (country.get("country") or "").strip()
        bucket: dict[str, dict] = {}
        todo: dict[str, dict] = {}
        for row in country.get("account_rows") or []:
            label = (row.get("account_label") or "").strip()
            if not label:
                continue
            labels_in_report.add(label)
            todo[label.casefold()] = {
                "to_do": row.get("to_do_display") or "",
                "ready": bool(row.get("is_ready")),
            }
            if period is None:
                bucket[label] = row.get("account_excel") or {}
            else:
                # Пересчёт за выбранный период тем же кодом, что считает всё
                # остальное на странице: две формулы для одного числа рано или
                # поздно разойдутся, и разойдутся молча.
                from src.analysis_agent.morning_report import build_account_performance_summary

                bucket[label] = build_account_performance_summary(
                    period, name, (row.get("alias") or label)
                )
        for key in {name.casefold(), _country_code(name).casefold()}:
            if key:
                per_country[key] = bucket
                todo_by_country[key] = todo
                display_name.setdefault(key, name)

    map_path = get_ad_account_map_path()
    known = list(load_ad_account_map(map_path)) if os.path.isfile(map_path) else []
    all_known = sorted(set(known) | labels_in_report, key=str.casefold)

    # Названия стран и имена аккаунтов берём из самой книги, а не только из
    # отчёта. Отчёт строится за сегодня: 8 сентября книга заканчивалась 7-м, в
    # отчёте не было ни одной страны — и карта показывала сплошные прочерки при
    # любом совпадении имён. Числа карты не должны зависеть от того, случился ли
    # сегодня хоть один лид.
    wb_country: dict[str, str] = {}
    try:
        if df is not None and "Country Final" in df.columns:
            for name in {str(c).strip() for c in df["Country Final"].dropna()}:
                if not name:
                    continue
                wb_country.setdefault(name.casefold(), name)
                wb_country.setdefault(_country_code(name).casefold(), name)
    except Exception:  # pragma: no cover - кривая книга не должна гасить карту
        wb_country = {}

    frame = period if period is not None else df

    def _numbers(country_name: str, alias: str) -> dict:
        """Числа одного аккаунта прямо из книги за выбранный период.

        Тем же кодом, что считает страницу: `alias_matches` слепа к хвостам
        вроде «EC Ait», так что подключённое имя сходится с книжным само.
        """
        if frame is None or not country_name or not alias:
            return {}
        try:
            from src.analysis_agent.morning_report import build_account_performance_summary

            return build_account_performance_summary(frame, country_name, alias)
        except Exception:  # pragma: no cover
            return {}

    def _elsewhere(country_name: str, alias: str) -> dict[str, float]:
        """Где ещё в книге лежат числа этого аккаунта за тот же период.

        Без этого «строк нет» и «строки есть, но под другой страной» выглядят
        одинаково — прочерком. А это разные вещи: во втором случае аккаунт
        привязан не туда, и человеку надо чинить привязку, а не искать данные.
        """
        if frame is None or not alias:
            return {}
        try:
            from src.analysis_agent.analyze_fb_stats import alias_matches

            hit = frame[alias_matches(frame["Account Alias"], alias)]
            if hit.empty:
                return {}
            here = (country_name or "").strip().casefold()
            out: dict[str, float] = {}
            for name, spend in hit.groupby("Country Final")["$ Spend"].sum().items():
                label = str(name).strip()
                if label and label.casefold() != here and float(spend):
                    out[label] = round(float(spend), 2)
            return out
        except Exception:  # pragma: no cover
            return {}

    # Показания Read нужны и построчно (в таблице), и суммой в свёрнутой
    # строке страны — снимаются один раз до цикла.
    live = {} if span == "all" else _live_spend(date_from, date_to)
    # ACL-1282: у разбивки больше нет своей кнопки, на которой была видна её
    # неудача. Отказ показывается в той же клетке, что и спенд, и должен
    # пережить перезагрузку страницы — значит приходит с сервера.
    splits = {} if span == "all" else _live_splits(date_from, date_to)
    # ACL-1271: у кого за эти даты чтения нет вовсе — последнее удачное чтение
    # за другие даты. Показывается помеченным и в итоги страны не входит.
    # Без дат читать нечего (кнопка «Select dates»), и «not the dates on
    # screen» там неправда — запасное чтение только при выбранном периоде.
    fallback = _fallback_spend(live) if (date_from and span != "all") else {}

    countries: list[dict] = []
    for stored in account_links.all_values("country"):
        key = stored.casefold()
        stats = per_country.get(key, {})
        # Show the country the way the rest of the page shows it. The map stores
        # whatever was typed when the account was connected — often a code — and
        # a Home screen reading "CR" next to a sidebar reading "Costa Rica"
        # invites the reader to wonder whether they are the same place.
        country_name = display_name.get(key) or wb_country.get(key, stored)
        todo = todo_by_country.get(key, {})
        rows = []
        for label in account_links.accounts_for("country", stored):
            excel = _numbers(country_name, label) or stats.get(label) or {}
            # Имя из Multilogin (`KLD_BFL_X84`) с книжным (`FacebookMKTOBFLX84`)
            # не сходится ничем, кроме хвоста — того самого, которым аккаунт и
            # называют вслух. Это уже догадка, поэтому она идёт последней, берётся
            # только при единственном совпадении и всегда подписывается на экране.
            alias = ""
            alias_exact = False
            from src.analysis_agent.account_names import rule_split

            # Под явным правилом имени разрешатель нужен и когда числа уже
            # нашлись: пользователь должен видеть, из какого имени книги они.
            if not excel.get("has_data") or rule_split(label):
                # Тот же разрешатель, что и у раскрытия объявлений: два способа
                # отвечать на один вопрос однажды разойдутся, и разойдутся молча.
                # Resolve identity against the whole workbook. A date filter
                # must not hide a collision or an older exact match.
                resolved = workbook_alias_for(df, country_name, label)
                if resolved and resolved.casefold() != label.casefold():
                    # Явное правило имён — точное совпадение: имя из книги
                    # по-прежнему показываем, но без пометки «догадка по хвосту».
                    from src.analysis_agent.account_names import account_key

                    alias = resolved
                    alias_exact = account_key(resolved) == account_key(label)
                    excel = _numbers(country_name, resolved) or excel
            rows.append({
                "account": label,
                # чем именно подставлены числа — чтобы совпадение по хвосту
                # никогда не выглядело как имя самого аккаунта
                "workbook_alias": alias,
                # True — имя из книги найдено по явному правилу (config/
                # account_name_rules.json), а не угадано по хвосту
                "workbook_alias_exact": alias_exact,
                # страны, под которыми числа этого аккаунта лежат на самом деле
                "elsewhere": {} if excel.get("has_data") else _elsewhere(country_name, alias or label),
                # состояние кабинета — без периода: спенд отвечает «сколько за
                # эти дни», состояние — «что с ним сейчас»
                "status": _account_status(label),
                # то же имя, что и у чисел: связка знает аккаунт по имени
                # Multilogin, отчёт — по книжному
                **{
                    k: (todo.get((alias or label).casefold()) or todo.get(label.casefold()) or {}).get(k, "" if k == "to_do" else False)
                    for k in ("to_do", "ready")
                },
                # «Книга про него не знает» и «книга знает и не считает
                # готовым» — разные вещи, а слово на экране было одно. Тестовый
                # аккаунт, заведённый на дешборде, в рабочей книге не появится
                # никогда, и вечное «needs setup» на нём означало бы задачу,
                # которой не существует.
                "in_workbook": bool(todo.get((alias or label).casefold()) or todo.get(label.casefold()))
                    or _workbook_has_account(df, country_name, label),
                "assets": account_links.values_for(label, "asset"),
                "countries": [
                    display_name.get(v.casefold(), v)
                    for v in account_links.values_for(label, "country")
                ],
                "spend": excel.get("spend"),
                "leads": excel.get("leads"),
                "deposits": excel.get("deposits"),
                "cpl": excel.get("cpl"),
                "cpa": excel.get("cpa"),
                "cr": excel.get("cr"),
                "has_data": bool(excel.get("has_data")),
            })
        # Спенд, снятый с самих кабинетов (Read), суммируется по стране
        # НА валюту: KWD и USD в одну цифру не складываются, а «не читали»
        # не равно нулю (acl-356). Пустой live_sums — «никто не смотрел».
        live_sums: dict[str, float] = {}
        unknown_sums: list[dict] = []
        live_read = 0
        for r in rows:
            lr = live.get(r["account"]) or {}
            if lr.get("state") != "ok" or lr.get("spend") is None:
                continue
            try:
                amount = float(lr["spend"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(amount):
                continue
            live_read += 1
            cur = str(lr.get("currency") or "").upper()
            if re.fullmatch(r"[A-Z]{3}", cur):
                live_sums[cur] = live_sums.get(cur, 0.0) + amount
            else:
                # Unknown units cannot be added to one another. The sign Meta
                # wrote ("$") is kept for display; it still does not say
                # dollars or pesos, so these stay separate values.
                unknown_sums.append({"currency": str(lr.get("currency") or "").strip(),
                                     "spend": amount, "account": r["account"]})

        countries.append({
            # `country` is what the reader sees, `key` is what the store knows.
            # They differ whenever the account map holds a code ("KW") and the
            # report holds a name ("Kuwait") — and a button that sends the label
            # instead of the key returns HTTP 200 having changed nothing.
            "country": country_name,
            "key": stored,
            "rows": rows,
            "spend": sum(r["spend"] or 0 for r in _rows_counted_once(rows)),
            "leads": sum(r["leads"] or 0 for r in _rows_counted_once(rows)),
            "deposits": sum(r["deposits"] or 0 for r in _rows_counted_once(rows)),
            "live_read": live_read,
            "live_sums": [
                {"currency": c, "spend": s} for c, s in sorted(live_sums.items())
            ] + unknown_sums,
        })

    # ── Одна таблица вместо двух (решение владельца 10.09, acl-356) ────
    # Countries overview сливается в карту: каждая строка несёт книжные
    # тоталы страны (my_totals — те же, что показывал overview), а страны
    # с деньгами в книге, но БЕЗ размещённых аккаунтов, попадают в список
    # «виртуальными» строками — их ноль аккаунтов подсвечивается: деньги
    # идут, а покрытия картой нет.
    book = {
        (c.get("country") or "").strip().casefold(): (c.get("my_totals") or {})
        for c in (report or {}).get("countries", [])
    }
    placed_keys = {(c["country"] or "").strip().casefold() for c in countries}
    for mc in countries:
        mc["book"] = book.get((mc["country"] or "").strip().casefold(), {})
    for c in (report or {}).get("countries", []):
        name = (c.get("country") or "").strip()
        if not name or name.casefold() in placed_keys:
            continue
        if not float(c.get("my_spend") or 0):
            continue
        countries.append({
            "country": name,
            "key": "",
            "virtual": True,
            "rows": [],
            "spend": 0, "leads": 0, "deposits": 0,
            "live_read": 0, "live_sums": [],
            "book": c.get("my_totals") or {},
        })

    # ── acl-357: спенд, привязанный к стране самим кабинетом ──────────────
    #
    # Соседняя колонка «Account read» честно подписана «not country-attributed
    # spend»: она показывает весь спенд привязанных кабинетов по всем странам.
    # Эти числа — про страну: Ads Reporting разбивает спенд кабинета по
    # Country, и 15.09 живое чтение DQ501 показало Chile на аккаунте, где весь
    # вечер запускали кампании на Гватемалу.
    #
    # Страна, которой нет ни в связях, ни в книге, получает свою строку: иначе
    # её деньги не видно нигде, а это ровно тот случай, ради которого разбивка
    # и заводилась.
    by_country: dict[str, list[dict]] = {}
    try:
        from src.analysis_agent import country_split

        if date_from:
            for row in country_split.totals_by_country(date_from, date_to or date_from):
                by_country.setdefault(
                    (row.get("country") or "").strip().casefold(), []
                ).append(row)
    except Exception:  # pragma: no cover — показания не должны ронять страницу
        by_country = {}

    for mc in countries:
        mc["country_read"] = by_country.get((mc["country"] or "").strip().casefold(), [])

    seen_keys = {(mc["country"] or "").strip().casefold() for mc in countries}
    for key, rows in sorted(by_country.items()):
        if key in seen_keys:
            continue
        countries.append({
            "country": rows[0].get("country") or key,
            "key": "",
            "virtual": True,
            "from_reading": True,
            "rows": [],
            "spend": 0, "leads": 0, "deposits": 0,
            "live_read": 0, "live_sums": [],
            "book": {},
            "country_read": rows,
        })
    # Порядок как был у overview: деньги сверху, дальше по алфавиту.
    countries.sort(
        key=lambda mc: (-float((mc.get("book") or {}).get("spend") or 0), (mc["country"] or "").lower())
    )

    # ACL-1319: Available accounts — только те, с кем есть смысл работать.
    # `all_known` остаётся прежним: из него строятся выпадающие списки
    # привязки, и там нужен каждый аккаунт. Сужается только этот список и его
    # счётчик; правило и его причины — в `available_accounts.py`.
    from src.web import available_accounts as _avail

    try:
        hidden = account_links.hidden_keys()
    except Exception:  # pragma: no cover - испорченная настройка вида не гасит карту
        hidden = set()
    # Окно активности меряется по всей книге, не по периоду на экране:
    # «работает ли аккаунт» не должно зависеть от выбранных дат.
    activity = _avail.workbook_activity(df)
    buckets = _avail.split_available(
        account_links.available_accounts(all_known),
        connected=known,
        placed=[a for a, _v in account_links.all_pairs("country")],
        activity=activity,
        hidden=hidden,
    )

    from src.analysis_agent import bi_compare

    activity_sources = {}
    if activity is not None and df is not None:
        window = bi_compare.period_rows(df, activity.window_from, activity.window_to)
        if "Account Alias" in window:
            activity_sources = {
                key: bi_compare.provenance(frame=part)
                for key, part in window.groupby(window["Account Alias"].map(_avail.alias_key))
            }
    connected_keys = {account_links.hidden_key(label) for label in known}

    def _with_assets(rows: list[dict]) -> list[dict]:
        out = []
        for row in rows:
            key = (_avail.workbook_key(row["account"], activity,
                   by_tail=account_links.hidden_key(row["account"]) in connected_keys)
                   if activity is not None else "")
            out.append({**row, "assets": account_links.values_for(row["account"], "asset"),
                        "source_info": activity_sources.get(key, {})})
        return out

    for mc in countries:
        # ACL-1633: заметка страны живёт под каноническим ключом, а не под
        # отображаемым именем («CR» и «Costa Rica» — одна заметка).
        mc["note_key"] = _account_notes.country_key(mc.get("country") or "")

    available = _with_assets(buckets["shown"])
    from src.web import source_display

    return {
        "countries": countries,
        "saved_source_check": source_display.saved_comparison(
            df, countries, live, date_from, date_to, span),
        "source_info": bi_compare.provenance(frame=period if period is not None else df) if df is not None else report.get("source_info"),
        # ACL-1654: строка Totals под странами.
        "totals": _amap_totals(countries, live),
        # ACL-1656: «Campaign managers» внизу Home.
        "cm_section": _amap_cm_section(report),
        "available": available,
        "available_inactive": _with_assets(buckets["inactive"]),
        "available_hidden": buckets["hidden"],
        "activity_window": (
            {"from": activity.window_from, "to": activity.window_to, "days": _avail.WINDOW_DAYS}
            if activity else None
        ),
        "all_accounts": all_known,
        "dates": dates,
        "live": live,
        "splits": splits,
        # ACL-1620: заметки менеджера по подписи аккаунта; читаются при каждой
        # сборке карты, поэтому переживают обновление отчёта и htmx-перерисовку.
        "notes": _account_notes.load(),
        "country_notes": _account_notes.load_countries(),
        "fallback": fallback,
        "date_from": date_from,
        "date_to": date_to,
        "span": span,
    }



def _amap_totals(countries: list[dict], live: dict) -> dict:
    """Итог по строкам стран карты (ACL-1654).

    Spend, Leads и FTDs — суммы книжных тоталов стран; CPL, CPA и CR считает
    шаблон по этим суммам (`metric_values`), а не усредняет их по странам.

    `read` — суммарное чтение кабинетов, и считается оно по *различным*
    аккаунтам: один кабинет может быть привязан к нескольким странам, а
    сводка страны показывает весь его спенд, так что сложение по странам
    посчитало бы его дважды. Складывается только когда все прочитанные
    кабинеты в одной валюте (решение владельца 06.10); иначе None — прочерк.
    """
    spend = leads = deposits = 0.0
    for mc in countries:
        bk = mc.get("book") or {}
        spend += float(bk.get("spend") or 0)
        leads += float(bk.get("leads") or 0)
        deposits += float(bk.get("deposits") or 0)
    seen: set[str] = set()
    sums: dict[str, float] = {}
    mixed = False
    read_ok = 0
    for mc in countries:
        for r in mc.get("rows") or []:
            acc = r.get("account")
            if not acc or acc in seen:
                continue
            seen.add(acc)
            lr = (live or {}).get(acc) or {}
            if lr.get("state") != "ok" or lr.get("spend") is None:
                continue
            try:
                amount = float(lr["spend"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(amount):
                continue
            read_ok += 1
            cur = str(lr.get("currency") or "").strip()
            if not re.fullmatch(r"[A-Za-z]{3}", cur):
                mixed = True  # знак без кода не говорит, какая это валюта
                continue
            sums[cur.upper()] = sums.get(cur.upper(), 0.0) + amount
    read = None
    if sums and not mixed and len(sums) == 1:
        (cur, amount), = sums.items()
        read = {"currency": cur, "spend": amount}
    return {
        "spend": spend, "leads": int(leads), "deposits": int(deposits), "read": read,
        # для подсказки: сколько кабинетов прочитано из скольких и почему «—»
        "read_count": read_ok, "accounts": len(seen), "mixed": bool(mixed or len(sums) > 1),
    }


def _amap_cm_section(report: dict) -> list[dict]:
    """Менеджеры по странам для секции внизу Home (ACL-1656).

    Те же строки, что у CM Comparison в Manage (`cm_comparison`): один источник,
    поэтому две таблицы не разойдутся. Строка страны — сумма *всех* менеджеров
    (решение владельца 06.10: это сравнение, а не «мои числа»), CPL/CPA/CR из
    этих сумм считает шаблон. Текущий менеджер помечен `is_me`.
    """
    out: list[dict] = []
    for c in (report or {}).get("countries", []):
        cms = c.get("cm_comparison") or []
        if not cms:
            continue
        rows = [
            {
                "cm": (r.get("cm") or "").strip() or "(unknown)",
                "is_me": bool(r.get("is_me")),
                "spend": float(r.get("spend") or 0),
                "leads": int(r.get("leads") or 0),
                "deposits": int(r.get("deposits") or 0),
            }
            for r in cms
        ]
        rows.sort(key=lambda r: (not r["is_me"], -r["spend"], r["cm"].lower()))
        out.append({
            "country": c.get("country") or "",
            "spend": sum(r["spend"] for r in rows),
            "leads": sum(r["leads"] for r in rows),
            "deposits": sum(r["deposits"] for r in rows),
            "rows": rows,
        })
    out.sort(key=lambda x: (-x["spend"], x["country"].lower()))
    return out


def _live_spend(date_from: str = "", date_to: str = "") -> dict[str, dict]:
    """Показания спенда, снятые с аккаунтов за тот же период, что на экране."""
    try:
        from src.analysis_agent import account_spend

        if not date_from:
            return {}
        return {
            a: r.to_dict()
            for a, r in account_spend.all_readings(date_from, date_to or date_from).items()
        }
    except Exception:  # pragma: no cover - показания не должны ронять страницу
        return {}



def _live_splits(date_from: str = "", date_to: str = "") -> dict[str, dict]:
    """Пострановые чтения за тот же период, что на экране: аккаунт → чтение."""
    try:
        from src.analysis_agent import country_split

        if not date_from:
            return {}
        return country_split.all_splits(date_from, date_to or date_from)
    except Exception:  # pragma: no cover - показания не должны ронять страницу
        return {}


def _fallback_spend(live: dict) -> dict[str, dict]:
    """Последнее удачное чтение тех, у кого за период на экране его нет (ACL-1271).

    Нет чтения за эти даты — показывается запасное. Неудачное за эти даты —
    решение владельца 22.09: показывать оба, неудачу («failed · retry») и под
    ней запасное, иначе одна неудачная попытка прятала и число, и дерево.
    Идущее чтение — ответ в пути, его не подменяем ничем.
    """
    try:
        from src.analysis_agent import account_spend

        return {
            a: r.to_dict()
            for a, r in account_spend.latest_readings().items()
            if a not in live or (live[a] or {}).get("state") == "failed"
        }
    except Exception:  # pragma: no cover - показания не должны ронять страницу
        return {}


def _country_ftds(country: dict):
    """FTD страны для бокового списка ({'you','all'}) или None; сбой не роняет страницу."""
    try:
        from src.analysis_agent.country_workbench import country_ftd_counts

        return country_ftd_counts(country.get("workbench"))
    except Exception:  # pragma: no cover - счётчик необязателен
        return None


def _home_kpis(report: dict) -> list[dict]:
    """Шесть плиток «Вы / Другие» для Home (ACL-1827); пусто, если считать не из чего."""
    try:
        from src.analysis_agent.country_workbench import home_kpi_tiles, home_you_vs_others

        cmp_ = home_you_vs_others(report.get("countries", []), report.get("my_cm", ""))
        if not cmp_["countries_used"]:
            return []
        return home_kpi_tiles(cmp_, money=lambda value: source_display.money(value, report.get("source_info")))
    except Exception:  # pragma: no cover - плитки не должны ронять отчёт
        return []


def _sidebar_countries(report: dict, amap: dict) -> list[dict]:
    """Страны для бокового списка: сначала те, где идут деньги.

    Порядок по расходу, а не по алфавиту: алфавит ничего не говорит, а список
    из двадцати стран человек читает сверху. Подсвечены те, где страна живая —
    либо на ней есть спенд, либо её завели руками в карте аккаунтов. Второе
    важно само по себе: страну открывают до первого доллара, и до него она
    выглядела бы такой же мёртвой, как закрытая полгода назад.
    """
    placed = {
        (c.get("country") or "").strip().lower()
        for c in (amap or {}).get("countries", [])
    }
    rows = []
    for c in report.get("countries", []):
        name = (c.get("country") or "").strip()
        spend = float(c.get("my_spend") or 0)
        rows.append(
            {
                "c": c,
                "spend": spend,
                "active": spend > 0 or name.lower() in placed,
                "in_map": name.lower() in placed,
                # ACL-1827: счётчик «вы / все» FTD под названием страны.
                "ftds": _country_ftds(c),
            }
        )
    # Сначала живые — по расходу, потом остальные по алфавиту. Страна,
    # открытая вчера и ещё без доллара, стоит выше двадцати мёртвых:
    # именно её сегодня и открывают.
    rows.sort(
        key=lambda r: (not r["active"], -r["spend"], (r["c"].get("country") or "").lower())
    )
    return rows


_HISTORY_ACT_CACHE: dict[str, tuple[float, str]] = {}


def _history_act_note(entry) -> str:
    """ACL-1697: заметка, если кабинет в карте не тот, где профиль работал.

    `mlx_profiles.ad_account_id_for` читает последний кабинет из истории
    браузера профиля (файл, без сети). Расхождение с картой — не ошибка, но
    повод проверить: робот при запуске сверит открытый кабинет и остановится
    на чужом (`wrong_ad_account`). Результат кэшируется на 2 минуты —
    чтение копирует файл истории.
    """
    if not entry:
        return ""
    want = (entry.ad_account_id or "").strip()
    pid = (entry.profile_id or "").strip()
    if not want or not pid:
        return ""
    import time as _time

    now = _time.time()
    hit = _HISTORY_ACT_CACHE.get(pid)
    if hit and now - hit[0] < 120:
        seen = hit[1]
    else:
        try:
            from src.fb_ads_rpa import mlx_profiles

            seen = mlx_profiles.ad_account_id_for(pid) or ""
        except Exception:
            seen = ""
        _HISTORY_ACT_CACHE[pid] = (now, seen)
    digits = lambda v: "".join(ch for ch in str(v) if ch.isdigit())
    if not seen or digits(seen) == digits(want):
        return ""
    return (f"The most visited account in this profile's recent history is {seen}, but the map has {want}. "
            "If its Facebook user has no access to the map's account, the robot will stop "
            "with wrong_ad_account - check the ad account id on the Accounts page.")


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
        info = get_account_info(ref, label, account_map=account_map)
        link = (info.get("link") or "").strip()
        excel_act_id = (info.get("ad_account_id") or "").strip()
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
            "ad_account_note": _history_act_note(entry),
        }
    return result


_targets_refresh_lock = threading.Lock()
_targets_refreshing = False


def _refresh_sheet_targets_in_background() -> None:
    """Обновить копию библиотеки таргетингов в своём потоке, один раз за раз.

    ACL-1380: раньше страница сама ходила в api.github.com (`pull(force=True)`,
    таймаут 15 с) на каждом открытии и на каждом `/api/ref-data`. Теперь
    отрисовка читает копию на машине, а обновление идёт рядом: свежие цели
    коллег видны со следующего открытия. Без force — `pull` сам не ходит в сеть
    чаще раза в PULL_EVERY_SECONDS; второй поток, пока первый ещё ждёт ответа,
    не запускается.
    """
    global _targets_refreshing
    with _targets_refresh_lock:
        if _targets_refreshing:
            return
        _targets_refreshing = True

    def run() -> None:
        global _targets_refreshing
        try:
            from src.analysis_agent import targeting_registry as tr
            tr.pull()
        except Exception:
            pass
        finally:
            with _targets_refresh_lock:
                _targets_refreshing = False

    threading.Thread(target=run, daemon=True, name="acl-targets-refresh").start()


def _load_sheet_targets() -> list[dict]:
    """The team targeting library from the machine's read copy (no network)."""
    from src.analysis_agent import targeting_registry as tr
    _refresh_sheet_targets_in_background()
    return [{"code": t.name, "label": t.name, "fb_interest": t.rpa_interests()}
            for t in tr.all_targets()]


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
    from src.analysis_agent import targeting_registry as tr
    library = _load_sheet_targets()
    has_registry = tr._store().exists()
    fallback_targets = library if has_registry or library else _load_kb_targets()
    empty: dict = {"ad_copies": [], "image_files": [], "video_files": [], "targetings": []}
    if not os.path.isfile(excel_path):
        result = {**empty, "targetings": fallback_targets}
        return result
    try:
        mtime = os.path.getmtime(excel_path)
    except OSError:
        return {**empty, "targetings": fallback_targets}
    if _ref_opts_cache is not None and mtime == _ref_opts_mtime:
        return {**_ref_opts_cache, "targetings": fallback_targets}
    try:
        ref = load_reference_data(excel_path)
    except Exception:
        return {**empty, "targetings": fallback_targets}
    ad_copies = sorted(ref.get("ad_copy") or {})
    # Списки файлов из книги здесь больше не собираются: билдер ищет креативы
    # в `/api/creatives`, который рекурсивно обходит папку, и экселевские
    # `image_files`/`video_files` не читал никто. Ключи остаются пустыми
    # списками — их формат входит в ответ, и убирать его отдельным решением.
    image_files: list = []
    video_files: list = []
    targetings = fallback_targets
    if not targetings and not has_registry:
        targetings = [{"code": t, "label": t, "fb_interest": t}
                      for t in sorted(ref.get("interests") or {})]
    result = {
        "ad_copies": ad_copies,
        "image_files": image_files,
        "video_files": video_files,
        "targetings": targetings,
    }
    _ref_opts_cache = result
    _ref_opts_mtime = mtime
    return result


def _add_linked_accounts_to_builder(report: dict) -> int:
    """Аккаунт, привязанный к стране на дешборде, виден и в её билдере.

    Список аккаунтов для билдера строился из книги статистики — то есть из
    того, что попало в отчёт. Аккаунт, подключённый здесь и в книге
    отсутствующий, в билдере не появлялся вовсе: в карте страны он есть, со
    спендом, статусом и кнопкой Read, а собрать на нём кампанию негде. Ровно
    этот случай — новый тестовый аккаунт, которого в рабочей книге и не должно
    быть.

    Билдер отвечает на «что запускаем», а не на «что попало в отчёт», поэтому
    источник тут — связи аккаунтов со странами. Они же давно умеют держать один
    аккаунт в нескольких странах; до сих пор этим нельзя было пользоваться,
    потому что в билдере аккаунт был виден только там, где его знает книга.

    Рекомендаций у добавленных нет и быть не может: их считают по числам,
    которых нет. Пустой список — это и значит «карточек нет, собирайте вручную»,
    и билдер такую страницу уже умеет показывать.
    """
    from src.web import account_links

    account_links.seed_from_account_map()
    by_key: dict[str, list[str]] = {}
    for stored in account_links.all_values("country"):
        by_key.setdefault(stored.casefold(), []).extend(
            account_links.accounts_for("country", stored)
        )

    # A newly linked country can have no statistics at all, including no report
    # section. Give it the same empty sections the dashboard already renders.
    countries = report.setdefault("countries", [])
    known = {key for c in countries for key in
             {(c.get("country") or "").casefold(), _country_code(c.get("country") or "").casefold()}}
    for stored in account_links.all_values("country"):
        if stored.casefold() in known or _country_code(stored).casefold() in known:
            continue
        name = (next((n.title() for n, code in _COUNTRY_CODES.items() if code == stored.upper()), stored)
                if len(stored) == 2 else stored)
        countries.append({"country": name, "cm_comparison": [], "account_rows": [],
                          "asset_summary": [], "asset_variants": [], "variants": [],
                          "top_combos": [], "targeting": [], "creatives": [],
                          "trend": {"has_date": False}, "close_recs": {}, "open_recs": {}})
        known.update({name.casefold(), _country_code(name).casefold()})

    added = 0
    for country in report.get("countries", []):
        name = (country.get("country") or "").strip()
        if not name:
            continue
        recs = country.setdefault("open_recs", {})
        seen = {str(k).casefold() for k in recs}
        # Страна в связях записана так, как её назвали при подключении — то
        # полным именем, то кодом. Смотрим оба написания, иначе привязка к «SA»
        # не найдёт «Saudi Arabia», и аккаунт снова пропадёт.
        for key in {name.casefold(), _country_code(name).casefold()}:
            for label in by_key.get(key, []):
                if label and label.casefold() not in seen:
                    recs[label] = []
                    seen.add(label.casefold())
                    added += 1
    return added


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


def _campaign_table_api() -> bool:
    """ACL-2063: saved structure is available regardless of the read backend.

    This retained template predicate selects the view only. Real reads and
    commands continue to enforce their API-backend/loopback guards.
    """
    return True


templates.env.globals["campaign_table_api"] = _campaign_table_api
from src.web import source_display
templates.env.filters["fmt_usd"] = source_display.source_money
templates.env.filters["fmt_currency"] = fmt_currency
templates.env.filters["fmt_summary_amount"] = fmt_summary_amount
# ACL-1652: шесть метрик одним кодом. Макросы `_metrics.html` берут отсюда и
# заголовки, и ячейки — таблицы не могут разойтись друг с другом.
templates.env.globals["source_comparison_cell"] = source_display.comparison_cell
templates.env.globals["source_comparison_info"] = source_display.comparison_info
templates.env.globals["metric_values"] = source_display.metric_values
# ACL-110: «было X и изменение» под значением, когда включено сравнение периодов.
templates.env.globals["metric_compare"] = source_display.metric_compare
templates.env.globals["value_compare"] = source_display.value_compare
templates.env.globals["compare_state"] = period_compare.state_for
# Owner, 2026-10-07: the "Copy for group" button and its hint under a red account
# status are hidden for now. True brings them back (template: _account_map_country.html).
SHOW_COPY_FOR_GROUP = False
templates.env.globals["SHOW_COPY_FOR_GROUP"] = SHOW_COPY_FOR_GROUP
templates.env.globals["METRIC_HEADERS"] = metric_columns.METRIC_HEADERS
templates.env.globals["HOME_ACCOUNT_HEADERS"] = metric_columns.HOME_ACCOUNT_HEADERS
templates.env.filters["urlencode"] = lambda s: _url_quote(str(s), safe="")
templates.env.filters["creative_type"] = lambda name: parse_creative_from_ad_title(name)[0]
templates.env.filters["creative_id"] = lambda name: parse_creative_from_ad_title(name)[1]


def _geo_language(country_name: str) -> str:
    """Язык аудитории по стране — умолчание поля в билдере.

    Правило берётся у того же кода, что применяет его при заливке, а не
    переписывается в шаблоне: два списка стран разошлись бы, и разошлись бы
    молча — на экране стояло бы одно, в Ads Manager набралось другое.

    Пусто — законный ответ для стран вне GCC и LatAm: угадывать за человека
    язык мы не будем, поле останется пустым и подсвеченным.
    """
    try:
        from src.fb_ads_rpa.enrich_draft import _geo_language_for_country

        return _geo_language_for_country(_country_code(country_name))
    except Exception:
        return ""


templates.env.filters["geo_language"] = _geo_language

# Drill fragment column counts per section — keep in sync with report.html
# table headers. The detail <td colspan> must match the parent table's
# column count or the layout breaks.
_NCOLS_BY_SECTION = {
    # ACL-1652: шесть метрик в каждой таблице, поэтому и числа новые.
    "cm_comparison": 10,  # CM, Spend, Leads, FTDs, CPL, CPA, CR, Net, ROI, (drill col)
    "trend": 10,          # offer, device, interest, creative, six metrics
    "top_combos": 7,      # combo, six metrics
    "targeting": 7,       # target, six metrics
    "creatives": 8,       # creative, text, six metrics
    "optimization": 8,    # adset, six metrics, reason
}

app = FastAPI(title="Adopy Campaign Manager")
# Предел тела проверяется до чтения (ACL-1356, аудит S04). Прослойка
# добавлена раньше local_dashboard_boundary, значит, стоит внутри неё:
# отказ 413 тоже получает защитные заголовки.
app.add_middleware(RequestBodyLimit, default_max_body=DEFAULT_MAX_BODY, route_max_body=ROUTE_MAX_BODY)
# SafeStaticFiles не пускает UNC, буквы дисков и абсолютные пути к файловой
# системе (CVE-2026-48818: исходящий SMB/NTLM на Windows).
app.mount("/static", SafeStaticFiles(directory=str(_STATIC_DIR)), name="static")


# ── Отчёт — не в цикле событий (ACL-1399) ───────────────────────────────
#
# Сервер однопоточный по циклу событий: всё, что обработчик `async def`
# делает синхронно, держит ВСЕ запросы. Холодная сборка отчёта шла прямо в
# GET / — минуту после перезапуска не отвечали ни /health, ни очередь
# публикаций, и Publish со страницы падал «Failed to fetch» (30.09).
#
# Всё, что читает отчёт, уходит в свой пул потоков. Свой, а не общий
# `asyncio.to_thread`: десяток запросов страницы, ждущих одну холодную сборку,
# не должен занять потоки, нужные отмене и запуску публикации.
_REPORT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="acl-report")


async def _in_report_pool(fn, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(_REPORT_POOL, functools.partial(ctx.run, fn, *args, **kwargs))


def _off_loop(fn):
    """Синхронный обработчик → async, исполняемый в пуле отчёта.

    Подпись остаётся прежней (FastAPI читает её через __wrapped__), а сам
    обработчик по-прежнему можно вызвать как корутину.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await _in_report_pool(fn, *args, **kwargs)

    return wrapper

_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _is_loopback_host(value: str | None) -> bool:
    """Accept localhost names and literal loopback IPs, including [::1]."""
    if not value:
        return False
    try:
        host = urlsplit(f"//{value}").hostname
    except Exception:
        return False
    if not host:
        return False
    host = host.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        return _is_loopback_host(urlsplit(value).netloc)
    except Exception:
        return False


def _secure_headers(response: Response, *, keep_cache_control: bool = False) -> Response:
    # ACL-1615: превью креатива кэшируется браузером (его Cache-Control
    # выставляет сам маршрут); всё остальное по-прежнему no-store.
    if not (keep_cache_control and "cache-control" in response.headers):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "media-src 'self' blob:; connect-src 'self'"
    )
    return response


@app.middleware("http")
async def local_dashboard_boundary(request: Request, call_next):
    """Reject DNS-rebinding hosts and cross-site browser mutations."""
    if not _is_loopback_host(request.headers.get("host")):
        return _secure_headers(
            JSONResponse({"error": "Local dashboard host required"}, status_code=400)
        )

    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        origin_ok = not origin or _is_loopback_url(origin)
        referer_ok = not referer or _is_loopback_url(referer)
        if fetch_site == "cross-site" or not origin_ok or not referer_ok:
            return _secure_headers(
                JSONResponse({"error": "Cross-site request blocked"}, status_code=403)
            )

    response = await call_next(request)
    keep = (
        request.url.path == "/creative-file"
        and request.method.upper() in {"GET", "HEAD"}
        and response.status_code in (200, 206, 304)
    )
    return _secure_headers(response, keep_cache_control=keep)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    if _FAVICON_ICO.is_file():
        return FileResponse(_FAVICON_ICO, media_type="image/x-icon")
    return FileResponse(_FAVICON, media_type="image/svg+xml")


# Multilogin refuses anything above 100 with a plain "limit invalid" 400 — and
# the panel asked for 200, so profile listing has been answering "no profiles
# found" for every workspace, whatever the token. A workspace with more than a
# hundred profiles then needs paging rather than a bigger page.
_MLX_PAGE = 100
_MLX_MAX_PROFILES = 2000


def _mlx_extract_profiles(body: dict) -> list[dict]:
    data = body.get("data") or {}
    raw = data.get("profiles") if isinstance(data, dict) else data
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


#: Сколько раз повторять сбойную страницу и сколько ждать между попытками.
#: Восемнадцать запросов подряд — это восемнадцать возможностей споткнуться,
#: и Multilogin спотыкается: три подряд полных загрузки на живом рабочем
#: пространстве дали две по 1759 профилей и один отказ 501.
_MLX_TRIES = 3
_MLX_RETRY_PAUSE_S = 0.8


def _mlx_worth_retrying(status: int) -> bool:
    """Стоит ли повторять этот отказ.

    5xx и 429 — про «сейчас не вышло»: со второй попытки обычно выходит.
    401 и 403 — про токен: он не станет живым оттого, что мы спросим ещё раз,
    а повтор лишь оттянет честный ответ на пару секунд.
    """
    return status >= 500 or status == 429


def _mlx_search_all(requests_mod, headers: dict, search_text: str) -> tuple[list[dict], str]:
    """Every profile the token can see, paged. Returns (profiles, error).

    Страницы тянутся по сотне, и в рабочем пространстве владельца их
    восемнадцать. Раньше единственный сбой на любой из них обрушивал всю
    загрузку: человек видел красное «Multilogin refused» — то есть слышал
    «отказано в доступе» там, где случился разовый сбой, — и лечил это
    нажатием Reload, о чём на экране не говорилось ни слова.
    """
    import time as _time

    found: list[dict] = []
    offset = 0
    while offset < _MLX_MAX_PROFILES:
        r = None
        last = ""
        for attempt in range(_MLX_TRIES):
            if attempt:
                _time.sleep(_MLX_RETRY_PAUSE_S)
            try:
                r = requests_mod.post(
                    f"{MLX_API_BASE}/profile/search",
                    headers=headers,
                    json={"limit": _MLX_PAGE, "offset": offset, "search_text": search_text},
                    timeout=20,
                )
            except Exception as exc:
                # Обрыв связи — из того же рода, что и 5xx: повторяем.
                r, last = None, f"{type(exc).__name__}: {exc}"[:200]
                continue
            if r.status_code == 200 or not _mlx_worth_retrying(r.status_code):
                break
        if r is None:
            return found, f"no answer after {_MLX_TRIES} tries" + (f": {last}" if last else "")
        if r.status_code != 200:
            detail = ""
            try:
                detail = ((r.json().get("status") or {}).get("message") or "").strip()
            except Exception:
                detail = (r.text or "")[:200]
            tries = f" after {_MLX_TRIES} tries" if _mlx_worth_retrying(r.status_code) else ""
            return found, f"HTTP {r.status_code}{tries}" + (f": {detail}" if detail else "")
        page = _mlx_extract_profiles(r.json())
        found.extend(page)
        if len(page) < _MLX_PAGE:
            break
        offset += _MLX_PAGE
    return found, ""


MLX_API_BASE = "https://api.multilogin.com"


def _installed_release() -> dict[str, str]:
    """Never let the header break the report if git is unavailable."""
    try:
        from src.web import acl_update

        return acl_update.installed()
    except Exception:  # pragma: no cover - defensive
        return {"sha": "", "short": "", "tag": ""}


def _stage_status() -> dict:
    """STAGE-значок шапки (ACL-1267). Шапка не должна ломать отчёт."""
    try:
        from src.web import acl_update

        return acl_update.stage_status()
    except Exception:  # pragma: no cover - defensive
        return {"channel": "stage", "known": False, "ahead": None, "why": "status unavailable"}


def mlx_instance_labels() -> dict[str, str]:
    """Human names for the two Multilogin accounts.

    "Instance A" and "Instance B" name a slot in a config file, not anything a
    campaign manager can recognise — and picking the wrong one is not a typo, it
    is a whole day: on 2026-09-03 the token for the Inhouse account was renewed
    with the KLD account's password, and every error along the way named
    something else (no token, no rights, expired plan).

    The slot ids stay A and B: they are written in ad_account_map.csv on every
    machine and in the environment keys. Only the labels change, so nothing has
    to be migrated and an old checkout keeps working.
    """
    return {
        "A": (os.environ.get("ACL_MLX_LABEL_A") or "").strip() or "Inhouse",
        "B": (os.environ.get("ACL_MLX_LABEL_B") or "").strip() or "KLD",
    }


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """Fast identity check for the Windows launcher; never loads workbooks.

    `version` lets the update helper tell the new release from the old one.
    `config` says whose dashboard this is: the launcher attaches only to its
    own, and anything else on the port means it takes another (ACL-1309).
    """
    # ACL-1399: первый вопрос о версии — это git (~3 с на загруженной
    # машине). /health — то, чем лаунчер проверяет, жив ли сервер: версию
    # читает один фоновый поток, а ответ ждёт её не дольше доли секунды и
    # дальше уходит без неё — следующий ответ её уже знает.
    known = _installed_release_event()
    if not known.is_set():
        await asyncio.to_thread(known.wait, 0.3)
    # Прочитана — повторный вызов берёт её из кэша acl_update мгновенно.
    release = _installed_release() if known.is_set() else {"tag": ""}
    from config.paths import config_identity

    return JSONResponse({"app": "ACL", "ok": True, "version": (release or {}).get("tag", ""),
                         "config": config_identity()})


_INSTALLED: dict = {"event": None}
_INSTALLED_LOCK = threading.Lock()


def _installed_release_event() -> threading.Event:
    """Событие «версия прочитана»; первый вызов запускает чтение — одно."""
    with _INSTALLED_LOCK:
        event = _INSTALLED["event"]
        if event is not None:
            return event
        event = _INSTALLED["event"] = threading.Event()

    def run() -> None:
        try:
            _installed_release()
        finally:
            event.set()

    threading.Thread(target=run, name="acl-installed-release", daemon=True).start()
    return event


def _warm_installed_release() -> None:
    """Прочитать версию в фоне (git), чтобы первый /health её уже знал."""
    _installed_release_event()


def _warm_control_paths() -> None:
    """Первые вызовы управляющих ответов — до первого запроса, в фоне.

    Первый снимок очереди профилей импортирует psutil, первая версия — git.
    Под холодной сборкой отчёта (GIL занят разбором книги) это секунды на
    ровном месте — ровно тогда, когда страница впервые спрашивает очередь.
    """
    _warm_installed_release()

    def slots() -> None:
        try:
            from src.web.publish_jobs import slots_snapshot

            slots_snapshot()
        except Exception:  # noqa: BLE001 — прогрев необязателен
            pass

    threading.Thread(target=slots, name="acl-warm-slots", daemon=True).start()


@app.on_event("startup")
async def _warm_on_startup() -> None:
    _warm_control_paths()
    from src.analysis_agent import account_refresh, campaign_inventory

    if campaign_inventory.backend_ready():
        account_refresh.queue().start()


@app.on_event("shutdown")
def _stop_account_refresh() -> None:
    from src.analysis_agent import account_refresh

    account_refresh.stop_queue()


@app.get("/acl/update/check")
async def acl_update_check(request: Request) -> JSONResponse:
    """Is a newer promoted release waiting? Answers the header's Update button.

    Reached on every page load, so it must never raise and never block for long:
    a machine with no network still has a dashboard to run.
    """
    from starlette.concurrency import run_in_threadpool
    from src.web import acl_update

    skip_fetch = (request.query_params.get("fetch") or "1").strip().lower() in {"0", "false", "no"}
    try:
        if getattr(sys, "frozen", False):
            # Установленный ACL.exe: выпуски GitHub, не git (ACL-1240).
            from src.web import release_updates
            force = (request.query_params.get("force") or "").strip().lower() in {"1", "true", "yes"}
            return JSONResponse(await run_in_threadpool(release_updates.check, force=force))
        # `git fetch` ждёт сеть до двух минут — не в цикле событий (ACL-1399).
        return JSONResponse(await run_in_threadpool(acl_update.check, fetch=not skip_fetch))
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse(
            {"ok": False, "available": False, "note": f"Update check failed: {exc}"}
        )


@app.post("/acl/update/apply")
async def acl_update_apply() -> JSONResponse:
    """Hand the update to the launcher and stop, so it can restart us into it.

    Every refusal below is a case where clicking would have produced a dashboard
    that does not come back: no supervising launcher, a run in flight, or a
    candidate the launcher would reject anyway.
    """
    from src.web import acl_update
    from src.web.publish_jobs import active_job_count

    if getattr(sys, "frozen", False):
        return await _apply_release_update(active_job_count)

    if not acl_update.is_supervised():
        return JSONResponse(
            {
                "ok": False,
                "error": "ACM was not started from its desktop shortcut, so nothing "
                         "would bring it back. Start it from the desktop shortcut "
                         "and try again.",
            },
            status_code=409,
        )

    busy = active_job_count()
    if busy:
        return JSONResponse(
            {
                "ok": False,
                "error": f"{busy} campaign job(s) still running. Updating restarts "
                         "ACM, which would abandon them half-built.",
            },
            status_code=409,
        )

    acl_update.forget_cache()
    state = acl_update.check(fetch=True, use_cache=False)
    if not state.get("available"):
        return JSONResponse(
            {"ok": False, "error": state.get("note") or "No promoted release to install."},
            status_code=400,
        )

    candidate = state.get("candidate") or {}
    target = candidate.get("tag") or candidate.get("short") or ""
    acl_update.request_restart(target=target)
    acl_update.schedule_exit()
    return JSONResponse({"ok": True, "target": target})


async def _apply_release_update(active_jobs) -> JSONResponse:
    """Serialize the download and reserve an idle runtime before restarting."""
    from starlette.concurrency import run_in_threadpool
    from src.web import acl_update, release_updates, publish_jobs
    from src.fb_ads_rpa.operations import browser_operation

    if not release_updates.apply_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "An update is already in progress."}, status_code=409)
    restarting = reserved = browser_held = False
    try:
        if active_jobs():
            return JSONResponse({"ok": False, "error": "Wait for campaign jobs to finish before updating."}, status_code=409)
        setup, manifest = await run_in_threadpool(release_updates.download)
        if active_jobs() or not publish_jobs.reserve_update():
            return JSONResponse({"ok": False, "error": "Campaign jobs started during the download; try again when they finish."}, status_code=409)
        reserved = True
        browser_held = browser_operation.acquire(blocking=False)
        if not browser_held:
            return JSONResponse({"ok": False, "error": "Wait for browser reading to finish before updating."}, status_code=409)
        release_updates.start_helper(setup, manifest)
        acl_update.schedule_exit()
        restarting = True
        return JSONResponse({"ok": True, "target": manifest["version"]})
    except (release_updates.UpdateError, OSError, ValueError, TypeError, KeyError) as exc:
        message = str(exc) if isinstance(exc, release_updates.UpdateError) else "The update could not be prepared. ACM is still running."
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    finally:
        # A successful handoff deliberately keeps the reservations until exit.
        if not restarting:
            if browser_held:
                browser_operation.release()
            if reserved:
                publish_jobs.release_update()
            release_updates.apply_lock.release()


def _period_owner(report: dict) -> dict | None:
    """Whose emptiness a chosen period is, when it is the manager's alone (ACL-106).

    Read from the already-built period report, never from the workbook frame:
    this runs on every render. `countries` is built from every manager's rows
    in the period, so no countries means nobody has rows (the existing
    workbook notice owns that case). Countries with no `is_me` row in any
    `cm_comparison` mean other managers have rows and this one has none.
    Rows without a CM name are nobody's and do not count as others' rows.
    """
    cm = str(report.get("my_cm") or "").strip().lower()
    if not cm or not (report.get("period_from") or report.get("period_to")):
        return None
    countries = report.get("countries") or []
    other_rows = 0
    for country in countries:
        for row in country.get("cm_comparison") or []:
            name = str(row.get("cm") or "").strip()
            if name.lower() in ("", "nan", "none"):
                continue
            if row.get("is_me") or name.lower() == cm:
                return None
            other_rows += 1
    if not other_rows:
        return None
    return {"kind": "manager", "manager": str(report.get("my_cm")).strip(), "other_cms": other_rows}


def _enrich_country_overview(report: Dict[str, Any]) -> None:
    """Ensure per-country my_totals / my_spend for home overview (cached reports)."""
    for country in report.get("countries", []):
        if "my_totals" not in country:
            country["my_totals"] = _my_cm_totals(country.get("cm_comparison", []))
        if "my_spend" not in country:
            country["my_spend"] = country["my_totals"]["spend"]


def _build_compare(
    base_report: dict, df, date_from: str, date_to: str, span: str,
    compare: str, cmp_from: str, cmp_to: str,
) -> tuple["period_compare.PeriodCompare | None", str]:
    """Отчёт и карта аккаунтов за сравниваемый период (ACL-110).

    Считаются тем же кодом, что и страница: `report_for_period` и
    `_build_account_map_view`. Сломанное сравнение не должно ронять страницу —
    она остаётся обычной, а причина показывается в шапке.
    """
    info, note = period_compare.resolve_compare(
        compare, cmp_from, cmp_to, date_from, date_to, span)
    if info is None:
        return None, note
    try:
        from copy import deepcopy

        # Копия: отчёт за период лежит в общем кэше, а его параллельно копирует
        # основной путь страницы.
        prev = deepcopy(report_for_period(base_report, df, info["date_from"], info["date_to"]))
        _enrich_country_overview(prev)
        # span="all": чтения с аккаунтов сравнению не нужны, только книжные числа.
        prev_amap = _build_account_map_view(prev, df, info["date_from"], info["date_to"], "all")
        return period_compare.PeriodCompare(info, prev, prev_amap), ""
    except Exception:
        return None, "Could not calculate the earlier period; showing the selected period only."


def _render_report(
    request: Request,
    *,
    upload_errors: dict[str, dict[str, list[str]]] | None = None,
    date_from: str = "",
    date_to: str = "",
    span: str = "",
    compare: str = "",
    cmp_from: str = "",
    cmp_to: str = "",
) -> HTMLResponse:
    try:
        report, _df, warnings = get_cached_report()
    except FileNotFoundError as e:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "title": "No data file", "message": str(e)},
            status_code=200,
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "title": "Excel format problem", "message": str(e)},
            status_code=200,
        )

    # Период выбирается один раз на всю страницу: карта аккаунтов, обзор
    # стран и рекомендации должны говорить об одних и тех же днях. Раньше он
    # менял только карту, и две секции рядом отвечали на разные вопросы,
    # выглядя одинаково достоверно.
    date_from, date_to = resolve_period(_df, date_from.strip(), date_to.strip(), span.strip())
    # Стартовые цели не зависят от выбранных дней — те же страны, что
    # сохранит первая правка (_goal_countries_now).
    goal_seed = _my_goal_countries(report)
    base_report = report
    try:
        report = report_for_period(report, _df, date_from, date_to)
    except PeriodReportError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "title": "Period calculation failed", "message": str(exc)},
            status_code=503,
        )

    # Period reports are cached. Local links belong to this render, otherwise
    # removed accounts remain in the cached builder on subsequent requests.
    from copy import deepcopy
    report = deepcopy(report)
    # До сборки списка аккаунтов ниже: добавленные должны получить и проверку
    # готовности, и строку в билдере, а не одно из двух.
    _add_linked_accounts_to_builder(report)
    _enrich_country_overview(report)
    period_cmp, compare_note = _build_compare(
        base_report, _df, date_from, date_to, span.strip(),
        compare.strip(), cmp_from.strip(), cmp_to.strip())
    ref_opts = _load_ref_options()

    # Collect all account labels from open_recs across all countries
    all_account_labels: list[str] = []
    for c in report.get("countries", []):
        for label in (c.get("open_recs") or {}).keys():
            if label not in all_account_labels:
                all_account_labels.append(label)

    account_preflight = _build_account_preflight(all_account_labels)
    draft_status = _load_draft_status()
    # Один раз: карту читают и шаблон, и боковой список стран.
    _account_map_view = _build_account_map_view(report, _df, date_from, date_to, span)

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

    goals_view = _build_goals_view(report, goal_seed)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "request": request,
            "report": report,
            "warnings": warnings,
            "built_at": cache_built_at(),
            "upload_errors": upload_errors or {},
            "ref_options": ref_opts,
            "ref_options_json": ref_opts,
            "account_preflight": account_preflight,
            "account_preflight_json": account_preflight,
            "account_alias_map_json": account_alias_map,
            "draft_status": draft_status,
            "mlx_labels": mlx_instance_labels(),
            "installed_release": _installed_release(),
            "stage_status": _stage_status(),
            "account_map": _account_map_view,
            "sidebar_countries": _sidebar_countries(report, _account_map_view),
            # ACL-1827: плитки «Вы / Другие» над картой аккаунтов на Home.
            "home_kpis": _home_kpis(report),
            "period": {"date_from": date_from, "date_to": date_to, "span": span},
            # ACL-110: сравнение с другим периодом (None — не включено).
            "compare": period_cmp,
            "compare_note": compare_note,
            # Только проверенное: сырое значение из адреса попало бы в разметку календаря.
            "compare_pick": ({"mode": period_cmp.mode, "date_from": period_cmp.date_from,
                              "date_to": period_cmp.date_to} if period_cmp else {}),
            "period_owner": _period_owner(report),
            "goals": goals_view,
            # ACL-1799: цель страны (My Goals) для Country Workbench — отдельным
            # числом, не смешанным с порогом из benchmarks и результатом пиров.
            "cw_goals": _workbench_goals(goals_view),
            # ACL-1761: кнопка «Check plan (no writes)» в билдере — только при api.
            "fb_backend": _fb_backend(),
            # ACL-1819: общая таблица правил pre-flight audit (та же, что у сервера).
            "preflight_rules_json": _preflight_rules(),
            # ACL-1807: допустимые Meta цели/CTA/destination из реестра (только ga).
            "meta_caps_json": _meta_capabilities(),
        },
    )


def _meta_capabilities() -> dict:
    """Допустимые значения из реестра правил Meta; сбой реестра форму не ломает."""
    try:
        from src.campaign_bridge import meta_rules

        return meta_rules.capabilities()
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Meta rules capabilities unavailable; builder is not restricted by them")
        return {}


def _preflight_rules() -> dict:
    from src.fb_api import preflight_rules

    return preflight_rules.export_rules()


def _fb_backend() -> str:
    try:
        from src.web import publish_api

        return publish_api.backend()
    except Exception:  # pragma: no cover - кнопка не важнее страницы
        return "rpa"




def _account_status(account: str) -> dict:
    """Последнее известное состояние кабинета, прочитанное с него самого."""
    try:
        from src.analysis_agent import account_spend

        return account_spend.get_status(account)
    except Exception:  # pragma: no cover - карта важнее одной колонки
        return {}


def _account_tail(name: str) -> str:
    """Последние три знака имени — то, чем аккаунты различают на слух.

    Книга знает аккаунт как `FacebookMKTOBFLX84`, Multilogin — как
    `KLD_BFL_X84`. Общего у них ровно хвост, и именно им человек называет
    аккаунт вслух: «икс восемьдесят четыре». Поиск профиля на этой же странице
    давно устроен так же — «last letters: X84».
    """
    import re as _re

    core = _re.sub(r"[^A-Za-z0-9]", "", name or "")
    return core[-3:].upper() if len(core) >= 3 else ""


#: ACL-1824: id рекламного кабинета в конце подписи — не короче 8 цифр.
#: Короткий числовой хвост (`Acme_123`) — часть имени, а не id кабинета.
_ID_SUFFIX_MIN_DIGITS = 8
_ID_SUFFIX_RE = re.compile(r"^(?P<alias>.+?)_(?P<digits>\d{%d,})$" % _ID_SUFFIX_MIN_DIGITS)


def _rows_counted_once(rows: list) -> list:
    """Строки страны для итогов: один алиас книги — один раз.

    ACL-1824: `Facebook_5265_<id>` и `Facebook_5265` оба сводятся к одному
    алиасу книги и показывают одни и те же числа. Строки остаются обе, а в
    итог страны идёт только одна — предпочтительно та, что названа алиасом.
    """
    seen: dict = {}
    for r in rows:
        if not r.get("has_data"):
            continue
        key = _account_key(r.get("workbook_alias") or r.get("account"))
        cur = seen.get(key)
        if cur is None or (cur.get("workbook_alias") and not r.get("workbook_alias")):
            seen[key] = r
    keep = {id(r) for r in seen.values()}
    return [r for r in rows if not r.get("has_data") or id(r) in keep]


def _alias_without_id_suffix(label: str, known) -> str:
    """Имя из книги для подписи `<имя>_<id кабинета>` — или пусто.

    Берётся только единственное имя: два разных алиаса в книге — это не
    «выберем первое», а отказ, как и у хвостовой догадки.

    ACL-1377: книга пишет то же имя и без подчёркиваний — `Facebook6540` для
    `Facebook_6540_<id>`. Сжатый ключ (только буквы и цифры) — второй шаг,
    после точного: если книга знает имя с подчёркиванием, берётся оно.
    """
    m = _ID_SUFFIX_RE.match((label or "").strip())
    if not m:
        return ""
    want = _account_key(m.group("alias"))
    if not want:
        return ""
    hits = {str(a).strip() for a in known if _account_key(a) == want}
    if not hits:
        compact = _compact_account_key(m.group("alias"))
        if compact:
            hits = {str(a).strip() for a in known if _compact_account_key(a) == compact}
    return next(iter(hits)) if len(hits) == 1 else ""


def _compact_account_key(name) -> str:
    """`account_key` без разделителей: `Facebook_6540` и `Facebook6540` равны (ACL-1377)."""
    key = _account_key(name)
    if re.match(r"rule\d+:", key):
        return ""  # ключ по явному правилу имён не сжимаем: rule1:0x и rule10:x разные
    return re.sub(r"[^0-9a-z]", "", key)


def _alias_by_tail(label: str, known: dict) -> str:
    """Имя из книги для подключённого аккаунта — или пусто.

    Совпадение принимается только единственное. Два имени с одним хвостом —
    это не «выберем первое»: подставленное наугад число выглядит точно так же,
    как правильное, и проверить его человеку будет нечем.
    """
    # Явное правило имён побеждает догадку по хвосту и не считается догадкой.
    from src.analysis_agent.account_names import exact_alias, has_own_name

    exact = exact_alias(label, known)
    if exact:
        return exact
    if has_own_name(label, known):
        return ""  # своё имя в книге есть — альтернатива не нужна
    tail = _account_tail(label)
    if not tail:
        return ""
    # Само имя из кандидатов исключается: свой хвост у него, разумеется,
    # единственный, и аккаунт «совпадал» сам с собой — на экране появлялась
    # подпись «= KLD_BFL_X01», объясняющая ровно ничего.
    hits = [
        a for a in known
        if _account_tail(a) == tail and a.casefold() != (label or "").casefold()
    ]
    if hits:
        return hits[0] if len(hits) == 1 else ""
    # ACL-1824: суффикс id — только последняя попытка; прежнее совпадение
    # по хвосту и отказ при его неоднозначности сохраняют приоритет.
    return _alias_without_id_suffix(label, known)


#: ACL-1729: индекс алиасов книги по странам — один слот на одну книгу.
#: Держим сильную ссылку на df, поэтому совпадение по `is` надёжно (id() мог бы
#: переиспользоваться); новая книга из кэша отчёта сбрасывает индекс.
_ALIAS_INDEX: dict = {"df": None, "rules": None, "by_country": {}}
_ALIAS_INDEX_LOCK = threading.Lock()


def _account_key(name) -> str:
    from src.analysis_agent.account_names import account_key

    return account_key(name)


def _country_alias_index(df, country: str):
    """{"names": отсортированные алиасы, "keys": множество account_key} или None.

    None — в книге нет строк этой страны (как `sub.empty` раньше).
    """
    # Ключи зависят от правил имён: правка config/account_name_rules.json
    # (горячая перезагрузка ACL-1653) обязана сбросить и индекс.
    from src.analysis_agent import account_names

    account_names._snapshot()
    rules_stamp = account_names._CACHE["stamp"]
    with _ALIAS_INDEX_LOCK:
        if _ALIAS_INDEX["df"] is not df or _ALIAS_INDEX["rules"] != rules_stamp:
            _ALIAS_INDEX["df"] = df
            _ALIAS_INDEX["rules"] = rules_stamp
            _ALIAS_INDEX["by_country"] = {}
        cache = _ALIAS_INDEX["by_country"]
        if country in cache:
            return cache[country]
    sub = df[df["Country Final"] == country] if "Country Final" in df.columns else df
    if sub.empty:
        idx = None
    else:
        col = sub["Account Alias"]
        names = sorted({str(a).strip() for a in col.dropna().unique() if str(a).strip()})
        keys = {_account_key(v) for v in col.fillna("").astype(str).unique()}
        idx = {"names": names, "keys": keys}
    with _ALIAS_INDEX_LOCK:
        if _ALIAS_INDEX["df"] is df and _ALIAS_INDEX["rules"] == rules_stamp:
            _ALIAS_INDEX["by_country"][country] = idx
    return idx


def workbook_alias_for(df, country: str, label: str) -> str:
    """Имя, под которым книга знает подключённый аккаунт.

    Подключённый носит имя из Multilogin (`KLD_BFL_X01`), книга — своё
    (`FacebookMKTOBFLX01`). Общего у них только хвост.

    Одно место на обе надобности — числа в карте и раскрытие объявлений под
    строкой. Пока перевод имени жил только в карте, числа находились, а
    раскрытие приходило пустым: строка показывала $1,795 и говорила, что
    объявлений нет. Разошедшиеся способы отвечать на один вопрос — это не две
    ошибки, а одна, повторённая дважды.
    """
    if df is None or not label:
        return label
    try:
        # ACL-1729: имена и ключи книги по стране считаются один раз на книгу,
        # а не на каждый аккаунт (раньше — проход по ~2M ячеек на вызов).
        idx = _country_alias_index(df, country)
        if idx is None:
            return label
        names = idx["names"]
        # Явное правило имён: если в книге аккаунт записан под другим префиксом
        # (FacebookMKTOBFLB98 для KLD_BFL_B98), отдаём настоящее имя из книги —
        # оно точное, а не догадка.
        from src.analysis_agent.account_names import exact_alias

        exact = exact_alias(label, names)  # "" если своё имя в книге есть
        if exact:
            return exact
        if _account_key(label) in idx["keys"]:
            return label
        return _alias_by_tail(label, names) or label
    except Exception:  # pragma: no cover - перевод имени не должен ронять страницу
        return label


def _workbook_has_account(df, country: str, label: str) -> bool:
    if df is None or "Account Alias" not in df.columns:
        return False
    idx = _country_alias_index(df, country)
    if idx is None:
        return False
    return _account_key(workbook_alias_for(df, country, label)) in idx["keys"]


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

    def _set_fields(idx: int, k: int) -> dict:
        """Поля уровня набора. Набор 0 живёт под прежними именами `camp_N_*`, остальные -
        под `camp_N_adset_K_*` (ACL-1768); значения читаются одинаково."""
        pfx = f"camp_{idx}_" if k == 0 else f"camp_{idx}_adset_{k}_"
        # ACL-1775: место конверсии набора; нет поля (RPA, старая страница) - сайт.
        destination = (form.get(pfx + "destination") or "website").strip().lower()
        # ACL-1767 (только API-бэкенд): поля набора. Пусто = ключа в слоте нет, и
        # прежние черновики не меняются ни байтом. Незнакомое значение уходит как есть,
        # а план откажет вслух, молча в умолчание оно не превращается.
        gender_val = (form.get(pfx + "gender") or "").strip().lower()
        if gender_val == "all":
            gender_val = ""
        device_val = (form.get(pfx + "device") or "").strip().lower()
        if device_val == "all":
            device_val = ""
        return {
            "targeting": (form.get(pfx + "targeting") or "").strip(),
            "os": (form.get(pfx + "os") or "All").strip(),
            "age": (form.get(pfx + "age") or "35").strip(),
            # Пусто — это ответ, а не пропуск: язык сняли намеренно, и правило по
            # стране подставится при обогащении только если поле не трогали.
            "language": (form.get(pfx + "language") or "").strip(),
            "language_provided": (pfx + "language") in form,
            "destination": destination,
            "gender": gender_val,
            "device": device_val,
            "os_min": (form.get(pfx + "os_min") or "").strip(),
            "start_time": (form.get(pfx + "start_time") or "").strip(),
            "languages": [x.strip() for x in (form.get(pfx + "languages") or "").split(",") if x.strip()],
            "adset_name": (form.get(f"camp_{idx}_adset_{k}_name") or "").strip(),
            "pixel": (form.get(f"camp_{idx}_adset_{k}_pixel") or "").strip(),
        }

    camp_spans: list[tuple[str, int, int, list[str]]] = []
    for idx in range(camp_count):
        asset_val = _abbrev_asset((form.get(f"camp_{idx}_asset") or "").strip())
        budget    = (form.get(f"camp_{idx}_budget") or "").strip()
        name      = (form.get(f"camp_{idx}_name") or "").strip()
        first_set = _set_fields(idx, 0)
        targeting, os_val, age_val = first_set["targeting"], first_set["os"], first_set["age"]

        try:
            ad_count = int(form.get(f"camp_{idx}_ad_count") or 1)
        except (TypeError, ValueError):
            ad_count = 1
        # ACL-1768: сколько наборов; нет поля (старая страница, RPA) - один.
        try:
            adset_count = max(1, int(form.get(f"camp_{idx}_adset_count") or 1))
        except (TypeError, ValueError):
            adset_count = 1
        # Ошибки формы этой кампании: уходят в слот и становятся ошибками черновика (отказ вслух).
        form_errors: list[str] = []
        from src.fb_ads_rpa.draft import MAX_AD_SETS, MAX_AD_SETS_ERROR
        if adset_count > MAX_AD_SETS:
            form_errors.append(MAX_AD_SETS_ERROR)
            adset_count = MAX_AD_SETS
        first_slot = len(slots)

        # Build auto-name from campaign-level fields if not set
        if not name:
            # Use first ad's creative type for name
            first_ctype = (form.get(f"camp_{idx}_ad_0_creative_type") or "Image").strip()
            name = "_".join(p for p in [country_val, asset_val, os_val, first_ctype, targeting, age_val, ddmm] if p)
        if not name:
            continue

        # Объявление -> набор: `camp_N_ad_M_adset`; нет поля - набор 0.
        ads_by_set: dict[int, list[int]] = {k: [] for k in range(adset_count)}
        for a_idx in range(ad_count):
            # Нет ключа - набор 0 (старые формы); есть, но не целое в [0, число наборов) - отказ,
            # объявление не угадывается в набор 0 и новый набор не придумывается.
            key = f"camp_{idx}_ad_{a_idx}_adset"
            if key not in form:
                k = 0
            else:
                from src.fb_ads_rpa.build_draft import bad_adset_message, parse_adset_index
                raw_k = form.get(key)
                k = parse_adset_index(raw_k, adset_count)
                if k is None:
                    form_errors.append(bad_adset_message(a_idx + 1, raw_k))
                    continue
            ads_by_set.setdefault(k, []).append(a_idx)

        for k in sorted(ads_by_set):
            sf = first_set if k == 0 else _set_fields(idx, k)
            destination = sf["destination"]
            # Набор без объявлений не пропадает молча: слот без объявления, план откажет вслух.
            ad_idxs = ads_by_set[k] or [None]
            for a_idx in ad_idxs:
                slot = {
                    "name": name,
                    "targeting": sf["targeting"],
                    "os": sf["os"],
                    "age": sf["age"],
                    "budget": budget,
                    "asset": asset_val,
                    "tracking_link": tracking_link,
                    "tracking_link_provided": "tracking_link" in form,
                    "language": sf["language"],
                    "language_provided": sf["language_provided"],
                }
                if a_idx is None:
                    slot.update({"creative_type": "Image", "creative_id": "", "creative_file": "",
                                 "copy": "", "no_ad": True})
                else:
                    creative_type = (form.get(f"camp_{idx}_ad_{a_idx}_creative_type") or "Image").strip()
                    creative_file = (form.get(f"camp_{idx}_ad_{a_idx}_creative_file") or "").strip()
                    copy          = (form.get(f"camp_{idx}_ad_{a_idx}_copy") or "").strip()
                    # ACL-1344: необязательный второй вариант основного текста —
                    # название записи библиотеки текстов, как у `copy`.
                    copy2         = (form.get(f"camp_{idx}_ad_{a_idx}_copy2") or "").strip()
                    # Legacy single-ad fallback (forms submitted before multi-ad upgrade)
                    if not creative_file and a_idx == 0:
                        creative_file = (form.get(f"camp_{idx}_creative_file") or "").strip()
                    if not copy and a_idx == 0:
                        copy = (form.get(f"camp_{idx}_copy") or "").strip()
                    slot.update({
                        "creative_type": creative_type,
                        "creative_id": (form.get(f"camp_{idx}_creative_id") or "").strip(),
                        "creative_file": creative_file,
                        "copy": copy,
                    })
                    # ACL-1774 (только API-бэкенд): тексты 3-5 и вертикальный файл Stories/Reels.
                    copy_more = [(form.get(f"camp_{idx}_ad_{a_idx}_copy{n}") or "").strip()
                                 for n in (3, 4, 5)]
                    while copy_more and not copy_more[-1]:
                        copy_more.pop()
                    file_vertical = (form.get(f"camp_{idx}_ad_{a_idx}_creative_file_vertical") or "").strip()
                    # Ключ — только у объявлений со вторым текстом: остальные слоты
                    # (и записанные из них задания) остаются прежними.
                    if copy2:
                        slot["copy2"] = copy2
                    if copy_more:
                        slot["copy_more"] = copy_more
                    if file_vertical:
                        slot["creative_file_vertical"] = file_vertical
                    cta_val = (form.get(f"camp_{idx}_ad_{a_idx}_cta") or "").strip().upper()
                    if cta_val and cta_val != "LEARN_MORE":
                        slot["cta"] = cta_val
                    if destination == "instant_form":
                        slot["lead_form_id"] = (form.get(f"camp_{idx}_ad_{a_idx}_form_id") or "").strip()
                        # ACL-1776: шаблон формы (создаёт робот при публикации); ключ есть только при выборе шаблона.
                        template = (form.get(f"camp_{idx}_ad_{a_idx}_form_template") or "").strip()
                        if template:
                            slot["lead_form_template"] = template
                # ACL-1768: ключи набора только у кампаний с несколькими наборами - слоты
                # одного набора остаются прежними ни байтом.
                if adset_count > 1 or len(ads_by_set) > 1:
                    slot["adset_index"] = k
                    slot["adset_count"] = adset_count
                # Страница - на кампании: ключ есть только когда поле пришло в форме.
                if f"camp_{idx}_page_id" in form:
                    slot["page_id"] = str(form.get(f"camp_{idx}_page_id") or "").strip()
                if sf["adset_name"]:
                    slot["adset_name"] = sf["adset_name"]
                if sf["pixel"]:
                    slot["pixel_id"] = sf["pixel"]
                # ACL-1775: Instant Form. Ключи есть только у слотов с формой; значения
                # другого назначения (сайта) не уходят вовсе.
                if destination != "website":
                    # Незнакомое назначение не превращается в сайт молча: уходит как есть,
                    # и план откажет вслух ("Unknown conversion location").
                    slot["destination"] = destination
                for key in ("gender", "languages", "device", "os_min", "start_time"):
                    if sf[key]:
                        slot[key] = sf[key]
                slots.append(slot)
        camp_spans.append((name, first_slot, len(slots), form_errors))
    # ACL-1768: две карточки с одним именем склеили бы наборы друг друга - отказ вслух.
    for name, start, end, errs in camp_spans:
        if sum(1 for n, *_ in camp_spans if n == name) > 1 and start < end:
            errs.append(f"Two campaigns are named {name}. Give each campaign a distinct name.")
    for name, start, end, errs in camp_spans:
        if errs and start < end:
            slots[start]["form_errors"] = list(dict.fromkeys(errs))
    # Return the ISO code, not the raw form value ("Costa Rica"). Confirmed
    # live 2026-07-30: the full name leaked into draft.country, where both
    # the GCC/LatAm language rule and the Excel Countries lookup expect
    # 2-letter codes — so draft.language came out empty and the audience
    # Language step was silently SKIPPED on every dashboard run (while CLI
    # runs, always invoked with --country CR, kept setting it). Telemetry
    # made this visible: dashboard runs had no `languages` row at all.
    return country_val or country, account_label, slots


def _page_ready(date_from: str, date_to: str, span: str) -> bool:
    """Готова ли страница без ожидания: отчёт и отчёт за её период в памяти.

    Только дешёвые проверки (stat книги, словари) — вызывается из цикла.
    """
    try:
        warm = peek_cached_report()
        if warm is None:
            return False
        report, df, _warnings = warm
        date_from, date_to = resolve_period(df, date_from.strip(), date_to.strip(), span.strip())
        if not (date_from or date_to) or df is None:
            return True
        return _period_cached(report, date_from, date_to)
    except Exception:
        return False


def _warm_page(date_from: str, date_to: str, span: str, my_cm: str) -> None:
    """Фоновая сборка ровно того, что нужно странице: отчёт и её период."""
    progress = background_progress_callback()
    report, df, _warnings = get_cached_report(my_cm=my_cm, progress=progress)
    progress("preparing")
    date_from, date_to = resolve_period(df, date_from.strip(), date_to.strip(), span.strip())
    report_for_period(report, df, date_from, date_to)


def _start_warm(date_from: str, date_to: str, span: str) -> None:
    from src.web.buyer import get_selected_cm

    my_cm = get_selected_cm()
    start_background_build(
        functools.partial(_warm_page, date_from, date_to, span, my_cm),
        token=(date_from, date_to, span, my_cm),
    )


# ── ACL-2207: the last finished page on disk ───────────────────────────
#
# The report cache lives in memory, so every restart used to cost minutes of
# "getting your dashboard ready". The page that was last rendered is kept in
# the machine config folder (src/web/report_store.py) and shown at once after
# a restart - marked "from cache" when nothing changed, OUTDATED when the code,
# the data or the number-changing settings are not the ones it was built with.
# The live report is still rebuilt in the background; once it is ready the
# in-memory path below serves it, so template edits keep their no-restart
# behaviour.
_STORE_SETTING_ENV = (
    "BI_CURRENCY", "STATS_CURRENCY", "BI_TIMEZONE", "STATS_TIMEZONE",
    "BI_DAY_BOUNDARY", "STATS_DAY_BOUNDARY",
)


def _file_stamp(path) -> list:
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return []


def _store_code_key() -> dict:
    import hashlib

    release = _installed_release()
    digest = hashlib.sha256()
    try:
        for item in sorted(_TEMPLATES_DIR.rglob("*")):
            if item.is_file() and item.suffix in (".html", ".css", ".js"):
                digest.update(
                    f"{item.relative_to(_TEMPLATES_DIR).as_posix()}|{_file_stamp(item)}".encode()
                )
    except OSError:
        pass
    return {
        "version": release.get("sha") or release.get("tag") or "unknown",
        "templates": digest.hexdigest()[:16],
    }


def _store_settings_key() -> dict:
    import hashlib

    from config.paths import get_ad_account_map_path

    hidden: list[str] = []
    try:
        from src.web import account_links, hidden_accounts

        hidden = sorted(hidden_accounts.load()) + sorted(account_links.hidden_keys())
    except Exception:  # noqa: BLE001 - unreadable list: the digest below still changes
        hidden = ["unreadable"]
    try:
        account_map = _file_stamp(get_ad_account_map_path())
    except Exception:  # noqa: BLE001
        account_map = []
    return {
        "env": {name: (os.environ.get(name) or "").strip() for name in _STORE_SETTING_ENV},
        "hidden": hashlib.sha256(chr(10).join(hidden).encode("utf-8")).hexdigest()[:16],
        "account_map": account_map,
    }


def _store_key(source) -> dict:
    return {"code": _store_code_key(), "source": source, "settings": _store_settings_key()}


def _store_inputs(date_from: str, date_to: str, span: str, compare: str,
                  cmp_from: str, cmp_to: str) -> dict:
    from src.web.buyer import get_selected_cm

    return {
        "cm": get_selected_cm() or "", "date_from": date_from.strip(), "date_to": date_to.strip(),
        "span": span.strip(), "compare": compare.strip(),
        "cmp_from": cmp_from.strip(), "cmp_to": cmp_to.strip(),
    }


def _stored_page(inputs: dict):
    """The saved page for this view with what changed since, or None. Pool thread only."""
    try:
        if not report_store.enabled():
            return None
        return report_store.load(inputs, _store_key(verified_source_key()))
    except Exception:  # noqa: BLE001 - a damaged store must never block the page
        return None


def _render_and_remember(request: Request, inputs: dict, **kwargs) -> HTMLResponse:
    """Render the live page and keep it on disk if it was built from one state."""
    try:
        before = _store_key(entry_source_key())
    except Exception:  # noqa: BLE001
        before = None
    from src.web.buyer import rendering_cm

    with rendering_cm(inputs["cm"]):
        response = _render_report(request, **kwargs)
    try:
        template = getattr(response, "template", None)
        if (
            before is not None and response.status_code == 200
            and getattr(template, "name", "") == "report.html"
            and _store_key(entry_source_key()) == before  # the data did not move meanwhile
        ):
            body = bytes(response.body).decode("utf-8")
            threading.Thread(
                target=report_store.save, args=(inputs, before, body),
                name="acl-report-store", daemon=True,
            ).start()
    except Exception:  # noqa: BLE001 - the page is already rendered
        pass
    return response


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request, date_from: str = "", date_to: str = "", span: str = "",
    compare: str = "", cmp_from: str = "", cmp_to: str = "",
) -> HTMLResponse:
    inputs = _store_inputs(date_from, date_to, span, compare, cmp_from, cmp_to)
    # ACL-1399: холодный отчёт — не держать страницу (и весь сервер) минуту,
    # а сразу ответить и собрать его в фоне. ACL-2207: отвечаем последней
    # сохранённой страницей, если она есть, а не экраном ожидания.
    if not _page_ready(date_from, date_to, span):
        failure = forget_background_error()
        # Only a stat on the loop; the key work (git, BI) happens in the pool
        # and only when there is something to show.
        stored = await _in_report_pool(_stored_page, inputs) if report_store.exists(inputs) else None
        if failure is None:
            _start_warm(date_from, date_to, span)
            if stored is not None:
                return HTMLResponse(report_store.decorate(stored))
            return templates.TemplateResponse(request, "loading.html", {"request": request})
        if stored is not None:
            # The rebuild failed: keep the last report and say why. The next
            # load starts a new rebuild.
            return HTMLResponse(report_store.decorate(
                stored, error=bi_stats.redact(failure) or "the report could not be rebuilt."))
        # Фоновая сборка упала — рисуем страницу обычным путём: он покажет
        # ту же ошибку по-человечески (нет файла, не тот формат).
    return await _in_report_pool(
        _render_and_remember, request, inputs, date_from=date_from, date_to=date_to,
        span=span, compare=compare, cmp_from=cmp_from, cmp_to=cmp_to,
    )


@app.get("/report/status", include_in_schema=False)
async def report_status(date_from: str = "", date_to: str = "", span: str = "") -> JSONResponse:
    """Для экрана «цифры загружаются»: готово ли, идёт ли, чем кончилось.

    Отвечает сразу — ничего не собирает и не ждёт. Если ни готово, ни идёт
    (например, сборку начал другой период), запускает фоновую сборку сам.
    """
    from src.web.buyer import get_selected_cm

    token = (date_from, date_to, span, get_selected_cm())
    ready = _page_ready(date_from, date_to, span)
    state = background_build_state()
    own_build = state["token"] == token
    error = state["error"] if own_build else None
    if not ready and not state["building"] and not error:
        _start_warm(date_from, date_to, span)
        state = background_build_state()
        own_build = state["token"] == token
        error = state["error"] if own_build else None
    return JSONResponse({
        "ready": ready,
        "building": state["building"],
        "error": error,
        "built_at": cache_built_at(),
        "stage": "ready" if ready else state["stage"] if own_build else None,
        "waiting": not ready and state["building"] and not own_build,
    })


@app.get("/details", response_class=HTMLResponse)
@_off_loop
def details(request: Request) -> HTMLResponse:
    """Return a single <tr><td colspan> fragment of underlying rows.

    HTMX swaps it as `afterend` of the clicked <tr>, so the response MUST
    be a single <tr> with one <td colspan="N"> child or the browser's
    HTML parser foster-parents it out of the table.
    """
    params = dict(request.query_params)
    section = params.pop("section", "")
    country = params.pop("country", "")
    # Раскрытие обязано резать те же дни, что и строка над ним. Пока период
    # менял только страницу, а /details читал весь файл, строка говорила про
    # выбранные шесть дней, а панель под ней — про все тридцать семь, и обе
    # выглядели одинаково достоверно. Для строки, которой в другом отрезке
    # нет, ответ вовсе приходил пустым — и это читалось как «не открывается».
    _p_from = params.pop("date_from", "")
    _p_to = params.pop("date_to", "")
    _p_span = params.pop("span", "")
    # Optional ncols override (used for nested drill-downs, e.g. ads inside CM campaigns)
    ncols_override = params.pop("ncols", None)
    _DRILL_ENABLED = {"optimization", "cm_comparison", "account"}
    if section not in _DRILL_ENABLED:
        ncols = 6
        return templates.TemplateResponse(
            request,
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
        if section == "account" and params.get("account"):
            # Use the same full-workbook identity as the map, then slice dates.
            params["account"] = workbook_alias_for(df, country, params["account"])
        df = _filter_by_period(df, *resolve_period(df, _p_from, _p_to, _p_span))
    except (FileNotFoundError, ValueError):
        # Cache build failed — return a no-op fragment so the table layout
        # stays intact and the user is invited to fix the / page first.
        return templates.TemplateResponse(
            request,
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
        request,
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
@_off_loop
def export_assets() -> Response:
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
                if col_idx in (1, 2):
                    # ACL-1342 S10: country/offer приходят из отчёта, не от
                    # нас. Если строка начинается с '=' (например, имя офера
                    # "=1+1"), openpyxl сам решает, что это формула — ровно
                    # по этому признаку, — и Excel её посчитает. Явно
                    # помечаем ячейку текстовой, чтобы значение осталось
                    # буквальной строкой независимо от того, с чего оно
                    # начинается. Числовых столбцов (3–6) это не касается.
                    cell.data_type = "s"
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


@app.get("/api/source-vs-account")
@_off_loop
def api_source_vs_account(date_from: str = "", date_to: str = "") -> JSONResponse:
    """ACL-103d: spend from the data source (BI / workbook) and spend read from
    the account, side by side and never merged. The difference is the signal."""
    from src.analysis_agent import bi_compare

    _report, df, _warnings = get_cached_report()
    date_from, date_to = resolve_period(df, date_from.strip(), date_to.strip(), "")
    info = bi_compare.provenance(df.attrs.get("stats_source", "excel"),
                                 bi_compare.period_rows(df, date_from, date_to))
    rows = bi_compare.compare_spend(
        bi_compare.source_spend_by_account(df, date_from, date_to),
        _live_spend(date_from, date_to) if date_from and date_to else {},
        source_context=bi_compare.source_context_by_account(df, date_from, date_to),
    )
    return JSONResponse({"ok": True, "date_from": date_from, "date_to": date_to,
                         "source": info, "rows": rows})


@app.get("/api/machine-paths")
def api_machine_paths() -> JSONResponse:
    """Where this computer keeps the Stats workbook and the Creatives folder."""
    from src.web import machine_paths
    return JSONResponse({"ok": True, **machine_paths.current()})


@app.post("/api/machine-paths")
async def api_save_machine_paths(request: Request) -> JSONResponse:
    """Change the Stats workbook or the Creatives folder without editing .env."""
    from src.web import machine_paths
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(data, dict) or any(
            k in data and not isinstance(data[k], str)
            for k in ("stats_workbook", "creatives_folder", "manager_name")):
        return JSONResponse({"ok": False, "error": "Expected text values"}, status_code=400)
    try:
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(machine_paths.save, data.get("stats_workbook"),
                                         data.get("creatives_folder"), data.get("manager_name"))
    except machine_paths.MachinePathError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if result["stats_changed"]:
        cache_clear()  # отчёт строился по прежней книге
    return JSONResponse({"ok": True, **result})


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
@_off_loop
def creative_brief(request: Request, creative: str = "") -> HTMLResponse:
    """Analyze a creative by name — search recursively in FB_CREATIVES_ROOT.

    If found: run full analysis and render brief.
    If not found: render upload form.
    """
    creative_name = creative.strip()
    if not creative_name:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"request": request, "title": "No creative name", "message": "Pass ?creative=<name>"},
        )

    found_path = find_creative_file(creative_name)

    if found_path is None:
        # File not found — render upload form
        return templates.TemplateResponse(
            request,
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
            request,
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
            request,
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
@_off_loop
def api_creatives() -> JSONResponse:
    """Scan FB_CREATIVES_ROOT recursively and return all creative files.

    Returns [{filename, rel_path, folder, type}] sorted by filename.
    Used by Campaign Builder for direct file search (no Excel lookup).
    """
    root = Path(get_creatives_root())
    if not root.is_dir():
        return JSONResponse({"error": f"Creatives folder not found: {root}", "files": []})

    files = []
    # Directory enumeration identifies file entries without a separate Path.stat
    # per entry. An unreadable size must not abort the whole picker on Windows.
    paths = (Path(folder) / name for folder, _dirs, names in os.walk(root) for name in names)
    for p in sorted(paths):
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
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        files.append({
            "filename": p.name,
            "rel_path": str(rel).replace("\\", "/"),
            "folder": folder.replace("\\", "/"),
            "type": ftype,
            # Вес виден при выборе, а не через десять минут загрузки. В той же
            # папке 10 сентября лежали файлы от 30 КБ до 1.5 ГБ, и по имени
            # они не отличались никак.
            "size_bytes": size,
        })
    _add_story_labels(files)
    return JSONResponse({"files": files})


def _add_story_labels(files: list[dict]) -> None:
    """ACL-1426: what goes on the Vertical (story) card of each file.

    Same rules as the robot (`creative_pairing.plan`): the pair is looked up by
    creative number among the files of the same folder. The Builder shows
    `story_label` next to the ad ("Story: 2403_story.mp4" / "Story: same file,
    Original").
    """
    from src.fb_ads_rpa import creative_pairing

    by_folder: dict[str, list[str]] = {}
    for f in files:
        by_folder.setdefault(f.get("folder") or "", []).append(f["filename"])
    for f in files:
        try:
            plan = creative_pairing.plan(f["filename"], by_folder.get(f.get("folder") or "", []))
            f["story_label"] = plan.builder_label()
            if plan.square_file != f["filename"]:
                f["square_file"] = plan.square_file
            elif plan.replace_vertical and plan.vertical_file != f["filename"]:
                # ACL-1774: на пути API это готовая вертикальная пара к квадратному файлу -
                # конструктор подставляет её в пустое поле Stories / Reels.
                folder = f.get("folder") or ""
                f["vertical_partner"] = (folder + "/" if folder else "") + plan.vertical_file
        except Exception:
            f["story_label"] = ""


def _ad_copy_rows() -> list[dict]:
    """Read the same local library used by RPA; only new machines import once."""
    from src.analysis_agent import ad_text_registry as ar

    return [{"ad_title": t.name, "headline": t.headline, "body": t.body,
             "description": t.description, "asset": t.asset,
             "language": t.language} for t in ar.builder_texts(force=True)]


@app.get("/api/ad-copy")
async def api_ad_copy() -> JSONResponse:
    """Тексты объявлений для билдера.

    Returns [{ad_title, headline, body, description, asset, language}].
    """
    from starlette.concurrency import run_in_threadpool

    from src.analysis_agent.ad_text_registry import AdTextError
    try:
        return JSONResponse({"rows": await run_in_threadpool(_ad_copy_rows)})
    except AdTextError as exc:
        return JSONResponse({"rows": [], "error": str(exc)}, status_code=503)


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
@_off_loop
def api_buyers() -> JSONResponse:
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
        await _in_report_pool(get_cached_report, my_cm=selected)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc), "selected": selected}, status_code=200)
    return JSONResponse({"ok": True, "selected": selected})



def _owner_hint(owner: str, selected_cm: str) -> str:
    """ACL-1588: подсказка, когда CM в книге статистики — не выбранный байер."""
    owner = (owner or "").strip()
    if owner and selected_cm and owner != selected_cm:
        return f"owner in stats: {owner}"
    return ""


@app.get("/api/accounts-overview")
@_off_loop
def api_accounts_overview() -> JSONResponse:
    """Every account this buyer has, and whether this machine can reach it.

    Two halves that were never shown together: the shared sheet says which
    accounts exist and what state they are in; the machine-local map says how
    to reach them (ad account id, Multilogin profile). An account can be
    perfectly healthy in the sheet and unusable here because the local map has
    no profile for it — which is exactly the case the operator needs to see.
    """
    from src.analysis_agent.account_country import (
        country_from_stats,
        linked_countries,
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
            {"ok": False, "error": f"Could not read the accounts sheet: {exc}",
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
    #   2. operator links, when the sheet has no country;
    #   3. recent stats, by latest date, fill the remaining gaps and break ties;
    # Never the suffix in the account label or the legacy map column.
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
    # Третий источник: то, что оператор соединил руками на дешборде.
    #
    # До 16.09 обзор его не читал вовсе, и аккаунт, подключённый к стране на
    # этом же экране, мог показываться как «страны нет». Дальше он падал в
    # «Available accounts», где у строки нет ни ячейки спенда, ни кнопки Read —
    # то есть прочитать его было нельзя никак. Так вёл себя Doc19: связь на
    # диске была, обзор её не спрашивал (ACL-1196).
    try:
        from src.web import account_links as _links

        by_link = linked_countries(_links.all_pairs("country"))
    except Exception:
        by_link = {}

    _resolved = resolve_countries(country_by_alias, sheet_rows, by_link)
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
    other_owner_labels: list[str] = []
    for row in sheet_rows:
        label = (row.get("account_label") or row.get("alias") or "").strip()
        entry = resolve_account_entry(account_map, label) if label else None
        ad_id = (getattr(entry, "ad_account_id", "") or "").strip() if entry else ""
        profile_id = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
        missing = [n for n, v in (("ad_account_id", ad_id), ("profile_id", profile_id)) if not v]
        owner = owner_by_alias.get(normalize_account_alias(label), "")
        # ACL-1588: кто назван CM в книге статистики, не прячет аккаунт, который
        # подключён на этой машине. Чужой аккаунт без строки в карте тут не
        # показываем (его место — подсказки для подключения), но он остаётся
        # доступным для поиска.
        if selected_cm and owner and owner != selected_cm and entry is None:
            other_owner_labels.append(label)
            continue
        accounts.append({
            "label": label,
            "owner": owner,
            "owner_hint": _owner_hint(owner, selected_cm),
            "country_name": country_by_alias.get(normalize_account_alias(label), ""),
            # An account can belong to two countries at once — five of fourteen
            # do. Showing only one hides the other, and the hidden one is a
            # market someone might build a campaign for.
            "countries": _resolved.get(normalize_account_alias(label), {}).get("countries", []),
            "country_ambiguous": _resolved.get(normalize_account_alias(label), {}).get("ambiguous", False),
            "country": country_by_alias.get(normalize_account_alias(label), ""),
            "stock": row.get("stock") or "",
            "status": row.get("account_status") or "",
            "label_key": profile_name_key((label or "").split(" ")[0]),
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
        # ACL-1588: подключён здесь — виден всегда, чьим бы ни назвала его книга.
        ad_id = (getattr(entry, "ad_account_id", "") or "").strip()
        profile_id = (getattr(entry, "profile_id", "") or "").strip()
        missing = [n for n, v in (("ad_account_id", ad_id), ("profile_id", profile_id)) if not v]
        accounts.append({
            "label": label,
            "owner": owner,
            "owner_hint": _owner_hint(owner, selected_cm),
            "country_name": country_by_alias.get(normalize_account_alias(label), ""),
            # An account can belong to two countries at once — five of fourteen
            # do. Showing only one hides the other, and the hidden one is a
            # market someone might build a campaign for.
            "countries": _resolved.get(normalize_account_alias(label), {}).get("countries", []),
            "country_ambiguous": _resolved.get(normalize_account_alias(label), {}).get("ambiguous", False),
            "country": country_by_alias.get(normalize_account_alias(label), ""),
            "stock": "",
            "status": "",
            "label_key": profile_name_key((label or "").split(" ")[0]),
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
        }
        # ACL-1588: подсказки для подключения — все аккаунты, а не только
        # выбранного байера: любой менеджер может подключить любой аккаунт.
        | {normalize_account_alias(l) for l in other_owner_labels
           if normalize_account_alias(l) and normalize_account_alias(l) not in listed_norm}
    )

    # Спрятанное помечается, а не вырезается: счётчик спрятанных и кнопка
    # «показать» должны на что-то опираться, а список, молча глотающий
    # строки, хуже длинного списка.
    from src.web import hidden_accounts as _hidden

    hidden_keys = _hidden.load()
    for acc in accounts:
        acc["hidden"] = _hidden.is_hidden(acc.get("label", ""), hidden_keys)

    return JSONResponse({
        "ok": True,
        "map_path": map_path,
        "accounts": accounts,
        "hidden_count": sum(1 for a in accounts if a["hidden"]),
        "suggestions": suggestions,
        "open_profiles": [
            {
                "profile_id": p["profile_id"],
                # The Multilogin profile NAME, as Multilogin itself reports it.
                # A dropdown of bare UUIDs told the operator nothing, and the
                # older way of getting a name — off the browser window title —
                # gave nothing at all for names containing a space.
                "name": p.get("name") or "",
                "key": profile_name_key(p.get("name") or ""),
                # Multilogin's own answer, falling back to the profile's path
                # on disk. Either way the operator never has to find or type it.
                "folder_id": p.get("folder_id") or folder_id_for(p["profile_id"]),
                # The ad account this profile actually works in, read from its
                # own browser history — the last field a person had to copy.
                "ad_account_id": ad_account_id_for(p["profile_id"]),
                "taken_by": taken.get(p["profile_id"], ""),
            }
            for p in open_detail
        ],
    })


@app.get("/api/team-library")
def api_team_library_state() -> JSONResponse:
    """Подключена ли общая библиотека на этой машине (ACL-2038). Токен не отдаётся."""
    from src.analysis_agent import team_library_connect as tlc

    return JSONResponse({"ok": True, **tlc.state()})


@app.post("/api/team-library/connect")
async def api_team_library_connect(request: Request) -> JSONResponse:
    """Проверить токен, сохранить в настройках машины, добавить недостающее (ACL-2038).

    Значение токена нигде не логируется и в ответ не возвращается: ни при успехе,
    ни при ошибке. Любая неожиданная ошибка отвечает фиксированным текстом.
    """
    from src.analysis_agent import team_library_connect as tlc

    try:
        data = await request.json()
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str):
            raise tlc.ConnectError("Paste the library token first.")
        result = await asyncio.to_thread(tlc.connect, token)
    except tlc.ConnectError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"ok": False, "error": "Could not connect the team library. Try again."},
                            status_code=500)
    return JSONResponse({**result, "message": tlc.summary(result.get("added") or {}, bool(result.get("seed_error")))})


@app.post("/api/team-library/disconnect")
async def api_team_library_disconnect() -> JSONResponse:
    """Убрать токен из настроек машины; локальные цели и тексты остаются."""
    from src.analysis_agent import team_library_connect as tlc

    try:
        await asyncio.to_thread(tlc.disconnect)
    except tlc.ConnectError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/api/targeting-profiles")
def api_targeting_profiles(force: bool = False) -> JSONResponse:
    """Refresh the read copy from the team library, then answer from it."""
    from src.analysis_agent import targeting_registry as tr
    try:
        library = tr.pull(force=True)
        targets = tr.all_targets()
        return JSONResponse({"ok": True, "can_add": True, "library": library,
                             "duplicates": tr.duplicates(targets),
                             "profiles": [t.to_dict() for t in targets],
                             "deleted": tr.recently_deleted()})
    except tr.TargetingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


def _edit_targeting_profile(data: dict) -> JSONResponse:
    from src.analysis_agent import targeting_registry as tr
    if not isinstance(data, dict) or any(not isinstance(data.get(k, ""), str)
            for k in ("action", "name", "interests", "narrow", "was")):
        return JSONResponse({"ok": False, "error": "Expected an object with string fields"}, status_code=400)
    action = (data.get("action") or "add").strip().lower()
    name, interests, narrow = (data.get(k, "") for k in ("name", "interests", "narrow"))
    try:
        if action == "delete":
            tr.remove(name)
            return JSONResponse({"ok": True, "removed": name, "scope": "team" if tr.team_library.configured() else "local"})
        if action == "update":
            target = tr.update(data.get("was") or name, name, interests, narrow)
        elif action == "add":
            target = tr.add(name, interests, narrow)
        elif action == "restore":
            target = tr.restore(name)
        else:
            raise tr.TargetingError(f"Unknown action: {action}")
    except tr.TargetingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "profile": target.to_dict(), "scope": "team" if tr.team_library.configured() else "local"})


@app.post("/api/targeting-profiles")
async def api_edit_targeting_profile(request: Request) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    return await run_in_threadpool(_edit_targeting_profile, data)


@app.get("/api/ad-texts")
def api_ad_texts(force: bool = False) -> JSONResponse:
    """Refresh the read copy from the team library, then answer from it."""
    from src.analysis_agent import ad_text_registry as ar
    try:
        library = ar.pull(force=True)
        texts = ar.all_texts()
        return JSONResponse({"ok": True, "library": library,
                             "duplicates": ar.duplicates(texts),
                             "texts": [t.to_dict() for t in texts],
                             "deleted": ar.recently_deleted()})
    except ar.AdTextError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


_AD_TEXT_FIELDS = ("action", "name", "headline", "body", "description",
                   "asset", "language", "was")


def _edit_ad_text(data: dict) -> JSONResponse:
    from src.analysis_agent import ad_text_registry as ar
    if not isinstance(data, dict) or any(not isinstance(data.get(k, ""), str) for k in _AD_TEXT_FIELDS):
        return JSONResponse({"ok": False, "error": "Expected an object with string fields"}, status_code=400)
    action = (data.get("action") or "add").strip().lower()
    name, headline, body, description, asset, language = (
        data.get(k, "") for k in ("name", "headline", "body", "description", "asset", "language"))
    try:
        if action == "delete":
            ar.remove(name)
            return JSONResponse({"ok": True, "removed": name, "scope": "team" if ar.team_library.configured() else "local"})
        if action == "update":
            text = ar.update(data.get("was") or name, name, headline, body, description, asset, language)
        elif action == "add":
            text = ar.add(name, headline, body, description, asset, language)
        elif action == "restore":
            text = ar.restore(name)
        else:
            raise ar.AdTextError(f"Unknown action: {action}")
    except ar.AdTextError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "text": text.to_dict(), "scope": "team" if ar.team_library.configured() else "local"})


@app.post("/api/ad-texts")
async def api_edit_ad_text(request: Request) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    return await run_in_threadpool(_edit_ad_text, data)


@app.get("/api/parse-adset")
async def api_parse_adset(name: str = "") -> JSONResponse:
    """Parse a raw Meta ad set name into (country_code, device, age, targeting)
    and resolve its interest token via the same KB alias lookup as
    /api/resolve-targeting. Used for the "Send to Builder" popup when the only
    field available is a real ad set's free-text name (CM Comparison drill).
    """
    return JSONResponse(parse_adset_for_builder(name))


def _sniff_creative_media_type(path: Path) -> str | None:
    """Тип — по первым байтам файла, а не по расширению.

    ACL-1342 S02: расширение подделывается переименованием (`evil.html` ->
    `evil.jpg`), первые байты — нет. Разрешён только список из спеки аудита:
    jpg/jpeg/png/gif/webp и mp4/mov/webm; всё остальное — не отдаём вовсе.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return None

    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        # MP4 и большинство современных .mov — общий ISO-BMFF контейнер;
        # различаются только заявленным брендом в ftyp-атоме.
        return "video/quicktime" if head[8:12] in (b"qt  ", b"qt\x20\x20") else "video/mp4"
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
        # Старые QuickTime .mov без ftyp-атома в самом начале файла.
        return "video/quicktime"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


# ACL-1615: предпросмотры Builder-а — десятки запросов подряд. Свой небольшой
# пул, а не отчётный: шторм превью не должен занять потоки отчёта и слотов.
_CREATIVE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="acl-creative")

# Файл креатива по одному и тому же пути не меняется, браузеру незачем
# перезапрашивать превью (ETag/Last-Modified проставляет FileResponse).
_CREATIVE_CACHE_CONTROL = "private, max-age=3600"


def _resolve_creative_for_serving(path: str) -> tuple[Path | None, str | None, JSONResponse | None]:
    """Блокирующая часть /creative-file (обход индекса, realpath, чтение первых
    байт) — выполняется в потоке, а не в цикле событий."""
    try:
        resolved = _find_creative_file_scoped(get_creatives_root(), path)
    except CreativeNotFoundError:
        return None, None, JSONResponse({"error": "Creative not found"}, status_code=404)
    except AmbiguousCreativeError as e:
        return None, None, JSONResponse({"error": str(e)}, status_code=409)

    media_type = _sniff_creative_media_type(resolved)
    if media_type is None:
        # ACL-1342 S02: не картинка и не видео из разрешённого списка — файл
        # существует (find_creative_file его нашёл), но отдавать его как есть
        # нельзя независимо от имени и расширения.
        return None, None, JSONResponse({"error": "Unsupported creative type"}, status_code=415)
    return resolved, media_type, None


def _creative_validators(st: os.stat_result) -> tuple[str, str]:
    """(ETag, Last-Modified) — те же, что строит FileResponse."""
    import hashlib
    from email.utils import formatdate

    etag = '"' + hashlib.md5(f"{st.st_mtime}-{st.st_size}".encode(), usedforsecurity=False).hexdigest() + '"'
    return etag, formatdate(st.st_mtime, usegmt=True)


def _creative_not_modified(request: Request | None, resolved: Path) -> Response | None:
    """304, если браузер уже держит эту версию файла (If-None-Match /
    If-Modified-Since)."""
    if request is None:
        return None
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    from email.utils import parsedate_to_datetime

    etag, last_modified = _creative_validators(st)
    headers = {"etag": etag, "last-modified": last_modified, "cache-control": _CREATIVE_CACHE_CONTROL}
    inm = request.headers.get("if-none-match")
    if inm:
        tags = {t.strip().removeprefix("W/") for t in inm.split(",")}
        if etag in tags or "*" in tags:
            return Response(status_code=304, headers=headers)
        return None
    ims = request.headers.get("if-modified-since")
    if ims:
        try:
            since = parsedate_to_datetime(ims).timestamp()
        except (TypeError, ValueError):
            return None
        if int(st.st_mtime) <= since:
            return Response(status_code=304, headers=headers)
    return None


def _creative_range_response(request: Request | None, resolved: Path, media_type: str) -> Response | None:
    """Range для видео. Исторически (Starlette 0.36) FileResponse Range не умел и
    отдавал файл целиком, а без 206 браузер не перематывает ролик; Starlette 1.x
    умеет, но свой обработчик оставлен. Поддержан
    один диапазон (`bytes=a-b`, `a-`, `-n`); остальное — обычный 200."""
    if request is None:
        return None
    header = request.headers.get("range")
    if not header or not header.lower().startswith("bytes="):
        return None
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    size = st.st_size
    etag, last_modified = _creative_validators(st)
    if_range = request.headers.get("if-range")
    if if_range and if_range.strip() != etag and if_range.strip() != last_modified:
        return None  # файл изменился — отдаём целиком
    spec = header[6:].strip()
    if "," in spec or "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    try:
        if first == "":
            n = int(last)
            if n < 0:
                raise ValueError
            if n == 0:  # `bytes=-0` — пустой суффикс, RFC 9110: не удовлетворить
                return Response(status_code=416, headers={"accept-ranges": "bytes", "content-range": f"bytes */{size}"})
            start, end = max(size - n, 0), size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
            end = min(end, size - 1)
    except ValueError:
        return None
    base = {"accept-ranges": "bytes", "etag": etag, "last-modified": last_modified,
            "cache-control": _CREATIVE_CACHE_CONTROL}
    if size == 0 or start >= size or start > end:
        return Response(status_code=416, headers={**base, "content-range": f"bytes */{size}"})

    def chunks():
        remaining = end - start + 1
        with open(resolved, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                data = fh.read(min(1024 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        chunks(),
        status_code=206,
        media_type=media_type,
        headers={**base, "content-range": f"bytes {start}-{end}/{size}", "content-length": str(end - start + 1)},
    )


@app.get("/creative-file")
async def creative_file(path: str = "", request: Request = None) -> Response:
    """Serve creative bytes (image/video) for the Campaign Builder's visual
    ad preview — the only route that returns actual file content; /api/creatives
    only lists metadata. `path` is whatever the client already knows (a
    rel_path from /api/creatives, or a bare filename) and is resolved the same
    root-scoped way publish-time enrichment does, so it can never escape
    get_creatives_root().

    ACL-1615: поиск файла идёт в пуле потоков, не в цикле событий (раньше
    каждый предпросмотр блокировал все остальные запросы на 10-30 с). Range
    для видео отдаёт сам FileResponse.
    """
    if not path.strip():
        return JSONResponse({"error": "path is required"}, status_code=400)
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    resolved, media_type, error = await loop.run_in_executor(
        _CREATIVE_POOL, functools.partial(ctx.run, _resolve_creative_for_serving, path)
    )
    if error is not None:
        return error
    not_modified = await loop.run_in_executor(_CREATIVE_POOL, _creative_not_modified, request, resolved)
    if not_modified is not None:
        return not_modified
    ranged = _creative_range_response(request, resolved, media_type)
    if ranged is not None:
        return ranged
    return FileResponse(
        resolved,
        media_type=media_type,
        headers={"Cache-Control": _CREATIVE_CACHE_CONTROL, "Accept-Ranges": "bytes"},
    )


@app.get("/api/account-notes")
def api_account_notes_get() -> JSONResponse:
    """Все заметки менеджера: аккаунты и страны (ACL-1620, ACL-1633)."""
    return JSONResponse({"ok": True, "notes": _account_notes.load(),
                         "countries": _account_notes.load_countries()})


@app.post("/api/account-notes")
async def api_account_notes_set(request: Request) -> JSONResponse:
    """Сохранить заметку к одному аккаунту или одной стране; пустой текст
    удаляет её. Заметки остаются в файле этой машины и никуда не уходят.

    Body JSON: {account, text} или {country, text}; необязательное if_empty: true —
    записать, только если заметки сейчас нет (Undo).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Expected JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Expected a JSON object"}, status_code=400)
    kind = "country" if "country" in body else "account"
    label, text = body.get(kind), body.get("text", "")
    if not isinstance(label, str) or not label.strip():
        return JSONResponse({"ok": False, "error": f"{kind} is required"}, status_code=400)
    if not isinstance(text, str):
        return JSONResponse({"ok": False, "error": "text must be a string"}, status_code=400)
    aside: dict = {}
    try:
        notes = _account_notes.set_note(label, text, kind, if_empty=body.get("if_empty") is True, report=aside)
    except _account_notes.NoteChanged:
        # Undo не затирает свежий текст: заметку успели написать заново.
        return JSONResponse({"ok": False, "error": "The note changed meanwhile"}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError:
        return JSONResponse({"ok": False, "error": "Could not save the note. Try again."}, status_code=500)
    key = _account_notes.country_key(label) if kind == "country" else label.strip()
    out = {"ok": True, "kind": kind, kind: label.strip(), "key": key, "text": notes.get(key, "")}
    if aside.get("set_aside"):
        # Файл заметок был испорчен и отложен в сторону: скажем об этом на странице.
        out["set_aside"] = aside["set_aside"]
        out["warning"] = f"Notes file was damaged; a copy was kept at {aside['set_aside']}"
    return JSONResponse(out)


@app.get("/api/builder-state")
def api_builder_state_get() -> JSONResponse:
    """Серверная копия черновиков билдера (ACL-1681): {name: {saved_at, data}}."""
    return JSONResponse({"ok": True, "docs": _builder_drafts.load()})


@app.post("/api/builder-state")
async def api_builder_state_set(request: Request) -> JSONResponse:
    """Сохранить один документ черновиков; побеждает более новый `saved_at`.

    Body JSON: {name: keep|jobs|ui, saved_at: ms, data: {...}, base: ms|null}.
    Ответ: {ok, accepted, doc}; accepted=false — на сервере копия новее, она в doc.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Expected JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Expected a JSON object"}, status_code=400)
    try:
        accepted, doc = _builder_drafts.save(body.get("name"), body.get("saved_at"),
                                             body.get("data"), body.get("base"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError:
        return JSONResponse({"ok": False, "error": "Could not save the draft. Try again."}, status_code=500)
    return JSONResponse({"ok": True, "accepted": accepted, "doc": doc})


@app.post("/api/accounts-hidden")
async def api_accounts_hidden(request: Request) -> JSONResponse:
    """Убрать аккаунт с глаз на этой машине или вернуть его обратно.

    Настройка вида и только она: аккаунт остаётся в общей таблице, сохраняет
    свои цифры и работает везде, где работал. Строки, которые прячут, —
    именно те, что удалить нельзя: они пришли из таблицы, а на машине про
    них ничего не записано, и удалять попросту нечего.

    Body JSON: {label, hidden}
    """
    from src.web import hidden_accounts as _hidden

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Expected JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Expected a JSON object"}, status_code=400)
    label = body.get("label")
    hide = body.get("hidden", True)
    if not isinstance(label, str) or not label.strip():
        return JSONResponse({"ok": False, "error": "label is required"}, status_code=400)
    if not isinstance(hide, bool):
        return JSONResponse({"ok": False, "error": "hidden must be a boolean"}, status_code=400)
    label = label.strip()
    try:
        keys = _hidden.set_hidden(label, hide)
    except OSError:
        return JSONResponse({"ok": False, "error": "Could not save this machine's hidden accounts. Try again."}, status_code=500)
    return JSONResponse({"ok": True, "label": label, "hidden": hide, "hidden_count": len(keys)})



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
        existing_columns: list[str] = []
        if map_path.is_file():
            with open(map_path, encoding="utf-8-sig", newline="") as f:
                reader = _csv.DictReader(f)
                rows = list(reader)
                existing_columns = list(reader.fieldnames or [])

        # Keep every column the file already has. This list is what the writer
        # emits, and anything missing from it is dropped from EVERY row in the
        # file — so linking one Multilogin profile used to erase `tracking_link`
        # for all accounts at once, because the column was added elsewhere
        # (_save_link_to_machine_map) and never added here. The account then
        # failed to publish with "Link empty in Upload FB Accounts sheet",
        # nowhere near the click that caused it.
        fieldnames = list(_FIELDNAMES) + [c for c in existing_columns if c not in _FIELDNAMES]

        # Upsert: find existing row by account_name (case-insensitive) or append
        name_lower = account_name.lower()
        existing_idx: int | None = None
        for i, row in enumerate(rows):
            if (row.get("account_name") or "").strip().lower() == name_lower:
                existing_idx = i
                break

        new_row: dict = {f: "" for f in fieldnames}
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
                     "error": "Could not read the ad_account_id. Paste the link from "
                              "Ads Manager, or the ad account number."},
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
                     "error": f"This ad account is already linked to \"{clash}\". "
                              f"Check that you pasted it in the right row."},
                    status_code=400,
                )
            new_row["ad_account_id"] = act

        if existing_idx is not None:
            rows[existing_idx] = new_row
        else:
            rows.append(new_row)

        write_csv_atomic(map_path, fieldnames, rows)
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



def _spend_period(date_from: str, date_to: str, span: str = "") -> tuple[str, str]:
    from datetime import date

    if span == "all" or not date_from.strip():
        raise ValueError("Live account spend requires a date range. Select dates instead of Maximum.")
    start = date.fromisoformat(date_from.strip())
    end = date.fromisoformat(date_to.strip() or date_from.strip())
    if end < start:
        raise ValueError("End date must be on or after start date.")
    return start.isoformat(), end.isoformat()


#: «Не передали» и «передали пустое» — разные вещи: у аккаунта, который
#: ещё ни разу не читали по странам, разбивка равна None, и принять это за
#: «возьми из хранилища» значило бы лишний раз читать диск на каждом опросе.
_NOT_GIVEN = object()


def _split_plan(account: str, date_from: str, date_to: str, *,
                reading=_NOT_GIVEN, split=_NOT_GIVEN) -> dict:
    """Нужна ли пострановая разбивка этому аккаунту за эти даты (ACL-1282).

    Считается по **дочитанному** показанию спенда — то есть после того, как
    чтение закончилось, а не до него (ACL-1288). До правки решение принимали
    в момент запуска, и сравнивали с прошлой суммой: нажатие, на котором
    деньги как раз изменились, разбивку не перечитывало, это делало только
    следующее. На закрытых датах незаметно, на сегодняшних экран показывал
    свежий спенд рядом с разбивкой от старого — и выглядел согласованным.

    Пока чтение идёт, сравнивать не с чем: запись «читаю» суммы не несёт.
    Поэтому решение и спрашивают там же, где узнают, что чтение кончилось.

    Вопрос без ответа — не повод не читать: любая поломка здесь отвечает
    «нужна». Лишний отчёт в кабинете дороже, но молча потерянная разбивка
    выглядит как «страны не читаются вовсе», а это неправда, которую с
    экрана не отличить.
    """
    from src.analysis_agent import account_spend, country_split
    from src.web import account_links

    try:
        # Страны считаем так же, как их показывает карта: «KW» и «Kuwait» —
        # одна страна. Разными их считало бы только это место, и аккаунт
        # навсегда оказался бы «в нескольких странах».
        countries = len({
            str(v).strip().casefold()
            for v in account_links.values_for(account, "country") if str(v).strip()
        })
        # Показание и разбивку берём те самые, которые уже прочитаны рядом.
        # Перечитать хранилище значило бы решать про другое чтение, чем то,
        # что уедет на экран: между двумя чтениями успевает начаться новое, и
        # решение приняли бы по записи «читаю» — без суммы (ревью 23.09).
        if split is _NOT_GIVEN:
            split = country_split.get_split(account, date_from, date_to)
        if reading is _NOT_GIVEN:
            reading = account_spend.get_reading(account, date_from, date_to)
        ok = getattr(reading, "state", "") == "ok"
        why = country_split.why_split_is_needed(
            countries=countries, split=split,
            spend_finished_at=str(getattr(reading, "finished_at", "") or "") if ok else "",
            spend=getattr(reading, "spend", None) if ok else None,
            currency=str(getattr(reading, "currency", "") or "") if ok else "",
        )
    except Exception:  # pragma: no cover - решение не должно ронять чтение
        return {"needed": True, "why": "could not tell whether the split is fresh"}
    return {"needed": bool(why), "why": why}


def _mlx_batch_refusal(form, account: str) -> JSONResponse | None:
    """ACL-1826: пачка уже потеряла общую сессию Multilogin слота — этот старт пропускаем.

    Страница шлёт `batch` (один id на нажатие Read all / Publish all). Без него
    поведение прежнее: одиночные нажатия не пропускаются.
    """
    batch = str(form.get("batch") or "").strip()
    if not batch:
        return None
    from src.fb_api import mlx_session

    skip = mlx_session.refusal(batch, mlx_session.instance_of(account), account)
    if not skip:
        return None
    return JSONResponse({"ok": False, "error": skip["message"], "code": skip["code"], "skipped": True},
                        status_code=409)


@app.post("/account-spend/read")
async def account_spend_read(request: Request) -> JSONResponse:
    """Прочитать спенд с аккаунта. Только по нажатию человека.

    Никакого расписания: обход живых аккаунтов по таймеру уже однажды
    предшествовал чекпоинту на телефон, и это не повторяется.
    """
    from src.analysis_agent import account_spend

    form = await read_form(request)
    account = (form.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    skipped = _mlx_batch_refusal(form, account)
    if skipped is not None:
        return skipped
    # Период приходит с экрана: число, снятое не за тот отрезок, который
    # показан рядом, выглядело бы расхождением источников, а было бы ответом
    # на другой вопрос.
    try:
        date_from, date_to = _spend_period(str(form.get("date_from") or ""), str(form.get("date_to") or ""), str(form.get("span") or ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        reading = account_spend.start_read(account, date_from, date_to)
    except Exception:
        return JSONResponse({"ok": False, "error": "Could not start or save the reading. Check disk space and retry."}, status_code=503)
    # Нужно ли после этого спрашивать кабинет про страны, отвечает опрос
    # состояния: там видно дочитанную сумму (ACL-1288).
    return JSONResponse({"ok": True, "reading": reading.to_dict()})


@app.get("/account-spend/status-shot/{shot_id}")
async def account_spend_status_shot(shot_id: str) -> Response:
    """Снимок проблемного состояния кабинета (ACL-1430) — только по его id.

    Путь с экрана не принимается: `status_shot_path` знает одну папку
    снимков и строгий вид имени, всё прочее — 404.
    """
    from src.analysis_agent import account_spend

    path = account_spend.status_shot_path(shot_id)
    if path is None:
        return JSONResponse({"error": "Screenshot not found"}, status_code=404)
    return FileResponse(str(path), media_type="image/png")


@app.get("/account-spend/status-shot/{shot_id}/view")
async def account_spend_status_shot_view(shot_id: str) -> Response:
    """Запасной путь кнопки Copy screenshot: браузер не дал положить картинку
    в буфер — открываем её во вкладке с подсказкой, как скопировать руками."""
    from html import escape

    from src.analysis_agent import account_spend

    if account_spend.status_shot_path(shot_id) is None:
        return JSONResponse({"error": "Screenshot not found"}, status_code=404)
    src = escape(f"/account-spend/status-shot/{shot_id}")
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Account status screenshot</title>"
        "<style>body{font:14px system-ui,sans-serif;margin:16px;background:#fff;color:#222}"
        "img{max-width:100%;border:1px solid #ccc}</style></head><body>"
        "<p>Right-click → Copy image</p>"
        f"<img src=\"{src}\" alt=\"Account status screenshot\"></body></html>"
    )


@app.post("/account-spend/read-countries")
async def account_spend_read_countries(request: Request) -> JSONResponse:
    """Прочитать пострановую разбивку аккаунта. Только по нажатию человека.

    То же правило, что и у чтения спенда: никакого расписания. Обход живых
    кабинетов по таймеру уже однажды предшествовал чекпоинту на телефон.

    Чтение дорогое — оно создаёт в кабинете отчёт и выставляет в нём период и
    разрезы, — поэтому запускается в фоне и сразу отвечает «running». Ответ
    смотрят через `/account-spend/countries`.
    """
    from src.analysis_agent import country_split

    form = await read_form(request)
    account = (form.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    skipped = _mlx_batch_refusal(form, account)
    if skipped is not None:
        return skipped
    try:
        date_from, date_to = _spend_period(
            str(form.get("date_from") or ""), str(form.get("date_to") or ""),
            str(form.get("span") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        split = country_split.start_split(account, date_from, date_to)
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Could not start or save the country reading. "
                                   "Check disk space and retry."},
            status_code=500,
        )
    return JSONResponse({"ok": True, "split": split.to_dict()})


@app.get("/account-spend/countries")
@_off_loop
def account_spend_countries(
    date_from: str = "", date_to: str = "", span: str = ""
) -> JSONResponse:
    """Сколько потрачено на каждую страну — по прочитанным аккаунтам.

    **Страны берутся из показаний, а не из карты аккаунтов.** В этом весь
    смысл: деньги нередко уходят на страну, к которой аккаунт не привязан, и
    показывать их под привязанной страной значит показывать неправду.
    Страна, которой в отчёте ещё нет, приходит с пометкой `known: false` —
    дешборду остаётся открыть под неё раздел, а не умолчать о ней.

    Ошибка чтения отчёта не должна прятать уже снятые показания: страны
    отдаются и тогда, когда книга не читается, просто без пометок.
    """
    from src.analysis_agent import country_split

    try:
        date_from, date_to = _spend_period(date_from, date_to, span)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    totals = country_split.totals_by_country(date_from, date_to)

    known: set[str] = set()
    note = ""
    try:
        report, _df, _warnings = get_cached_report()
        _enrich_country_overview(report)
        for section in report.get("countries") or []:
            name = str((section or {}).get("country") or "").strip()
            if name:
                known.add(name.casefold())
    except Exception as exc:  # книга не прочиталась — показания всё равно наши
        note = f"Could not tell which countries are already on the report: {exc}"

    for row in totals:
        row["known"] = row["country"].casefold() in known if known else None

    return JSONResponse({
        "ok": True,
        "date_from": date_from,
        "date_to": date_to,
        "running": country_split.running_now(),
        "countries": totals,
        "splits": country_split.all_splits(date_from, date_to),
        "note": note,
    })


@app.post("/report-problem")
async def report_problem(request: Request) -> JSONResponse:
    """Пожаловаться из лаунчера: человек пишет, машина прикладывает факты.

    Сегодня о поломке узнают из сообщения в переписке и фотографии экрана —
    пересказанных, с опозданием и без фактов самой машины (ACL-354).

    Окружение собирается здесь, а не человеком: версия потока, метка машины,
    период на экране и последние отказавшие шаги. Всё это уже вычищено от
    номеров кабинетов и профилей — тем же вычистителем, что и телеметрия.

    **Неудача отправки не выдаётся за удачу.** Задача требует этого прямо:
    «not falsely reported after a delivery failure». Пустой номер события —
    это «не ушло», и так и сказано.
    """
    from src import acl_sentry

    form = await read_form(request)
    text = (form.get("text") or "").strip()
    if not text:
        return JSONResponse(
            {"ok": False, "error": "Write what went wrong — even one line helps."},
            status_code=400,
        )

    if len(text) > 4000:
        return JSONResponse({"ok": False, "error": "Keep the report within 4000 characters."}, status_code=400)

    context: dict[str, Any] = {
        "where": (form.get("where") or "").strip()[:200],
        "period": {k: str(form.get(k) or "")[:40] for k in ("from", "to", "span")},
    }
    try:
        from src.fb_ads_rpa import run_telemetry, telemetry_sync

        context["version"] = telemetry_sync.flow_version()
        context["machine"] = telemetry_sync.machine_identity()[0]
        failed = [
            {k: r.get(k) for k in ("ts", "step", "campaign", "diag") if k in r}
            for r in run_telemetry.load()[-400:]
            if r.get("ok") is False
        ]
        context["last_failures"] = failed[-8:]
    except Exception as exc:  # окружение не собралось — обращение всё равно уходит
        context["context_error"] = str(exc)[:200]

    event_id = ""
    try:
        event_id = acl_sentry.report_problem(text, context=context)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": "Report submission was not confirmed. Your text is still available; retry when ready."}, status_code=502
        )
    if not event_id:
        return JSONResponse(
            {"ok": False,
             "error": "Reporting is switched off on this machine, so nothing was sent."},
            status_code=503,
        )
    # An SDK event ID confirms local queue acceptance, not network delivery.
    return JSONResponse({"ok": True, "queued": True, "event_id": event_id}, status_code=202)


@app.post("/account-spend/turn-off")
async def account_spend_turn_off(request: Request) -> JSONResponse:
    """Выключить одну кампанию или одно объявление на живом аккаунте.

    Первый маршрут ACL, который меняет чужой кабинет, а не читает его.
    Поэтому здесь **только выключение**: включить отсюда нельзя (решение
    владельца 12.09). Выключение денег не тратит, включение тратит, и обратный
    ход человек делает руками в Ads Manager, где видит последствия.

    Как и чтение, это только по нажатию человека: никакого расписания и
    никаких списков «выключить всё подряд».
    """
    from src.analysis_agent import account_spend

    form = await read_form(request)
    account = (form.get("account") or "").strip()
    level = (form.get("level") or "").strip()
    object_id = (form.get("object_id") or "").strip()
    if not account or not object_id:
        return JSONResponse({"ok": False, "error": "account and object_id are required"}, status_code=400)
    if level not in ("campaign", "ad"):
        return JSONResponse({"ok": False, "error": "level must be campaign or ad"}, status_code=400)
    from src.analysis_agent import account_spend_api

    if account_spend_api.api_enabled():
        # ACL-1945: под `ACL_FB_BACKEND=api` выключаем по id через Graph API из профиля,
        # а не кликом по тумблеру; период тут ни к чему — таблицу никто не читает.
        return await _turn_off_via_api(form, account, level, object_id)
    try:
        date_from, date_to = _spend_period(str(form.get("date_from") or ""), str(form.get("date_to") or ""), str(form.get("span") or ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # В отдельный поток, а не здесь. Playwright тут синхронный, и вызов его из
    # работающего цикла asyncio кончается `RuntimeError: Cannot enter into
    # task ... while another task is being executed` — то есть выключение
    # падает, ни разу не тронув страницу. Чтение по той же причине живёт в
    # своём потоке (`start_read`); я про это забыл, и первое же живое нажатие
    # 12.09 упёрлось ровно в это.
    from starlette.concurrency import run_in_threadpool

    try:
        result = await run_in_threadpool(
            account_spend.turn_off,
            account, level, object_id,
            parent_id=(form.get("parent_id") or "").strip(),
            expected_name=(form.get("name") or "").strip(),
            date_from=date_from, date_to=date_to,
            # Кто нажал. Берём ту же подпись, которой дешборд всюду называет
            # хозяина машины; пустая — честнее выдуманной.
            requester=(os.environ.get("MY_CM_NAME") or "").strip(),
        )
    except Exception:
        return JSONResponse({"ok": False, "error": "Could not reach the account. Check the Adopy Campaign Manager window."}, status_code=503)
    return JSONResponse({"ok": True, "result": result.to_dict()})


async def _turn_off_via_api(form, account: str, level: str, object_id: str) -> JSONResponse:
    """Выключение по id через Graph API (ACL-1945). Тот же ответ, что у RPA-пути."""
    from datetime import datetime, timezone
    from starlette.concurrency import run_in_threadpool

    from src.analysis_agent import account_spend, account_spend_api
    from src.web import publish_jobs

    requester = (os.environ.get("MY_CM_NAME") or "").strip()
    name = str(form.get("name") or "").strip()
    parent_id = (form.get("parent_id") or "").strip()
    # Робот строит кампании в этом профиле — отказ сразу, а не очередь за замком профиля.
    # Под замком `turn_off` спросит ещё раз: `since` помнит и задание, успевшее
    # закончиться, пока мы ждали.
    since = datetime.now(timezone.utc).isoformat()
    try:
        busy = publish_jobs.profile_in_use(account_spend._profile_key(account))
    except Exception:
        busy = True
    if busy:
        result = account_spend_api._api_result(account, level, object_id, name, requester)
        result.error = account_spend_api.PROFILE_BUSY_ERROR
        account_spend._record_turn_off(result)
        return JSONResponse({"ok": False, "busy": True, "error": result.error,
                             "result": result.to_dict()}, status_code=409)
    try:
        result = await run_in_threadpool(
            account_spend_api.turn_off, account, level, object_id,
            parent_id=parent_id, expected_name=name, requester=requester,
            profile_busy=lambda key: publish_jobs.profile_in_use(key, since=since),
        )
    except Exception:
        return JSONResponse({"ok": False, "error": "Could not reach the account. Check the launcher window."}, status_code=503)
    body = {"ok": True, "result": result.to_dict()}
    if result.error == account_spend_api.PROFILE_BUSY_ERROR:
        body["busy"] = True
    return JSONResponse(body)


@app.get("/account-spend/turn-off-history")
async def account_spend_turn_off_history(request: Request) -> JSONResponse:
    """Что дешборд менял в этом кабинете — и чем это кончилось.

    Журнал писался с первого же живого нажатия, но прочитать его было негде:
    файл рос, а на экране от него не было ничего. Запись, которую никто не
    может открыть, не отвечает на вопрос «почему это выключилось вчера» — она
    только позволяет сказать, что ответ где-то есть.

    Отдаём как есть, включая отказы: попытка, которая не сработала, — это тоже
    то, что человек делал с живым аккаунтом, и прятать её значило бы
    показывать журнал, в котором всё всегда получалось.
    """
    from src.analysis_agent import account_spend

    account = (request.query_params.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    try:
        limit = max(1, min(200, int(request.query_params.get("limit") or 20)))
    except ValueError:
        limit = 20
    try:
        rows = account_spend.turn_off_history(limit=limit, account=account)
    except Exception:
        # Журнал — рассказ о работе, а не сама работа: его поломка не повод
        # ронять страницу, на которой человек останавливает кампании. Но и
        # выдавать её за пустой журнал нельзя. Пустой список значит «здесь
        # ничего не меняли» — самый успокаивающий ответ из возможных, и
        # отдавать его вместо «прочитать не удалось» хуже, чем промолчать:
        # человек уходит уверенным в том, чего никто не проверял.
        return JSONResponse(
            {"ok": False, "error": "The record could not be read."},
            status_code=500,
        )
    return JSONResponse({"ok": True, "entries": rows})


@app.post("/account-spend/open-profile")
async def account_spend_open_profile(request: Request) -> JSONResponse:
    """Поднять профиль аккаунта автоматизацией и оставить окно открытым.

    Чтение умеет только цепляться к уже открытому окну — перезапуск ради
    одного числа разлогинивает аккаунт. Поднять профиль так, чтобы к нему
    можно было прицепиться, до сих пор умел только путь публикации.
    """
    from src.analysis_agent import account_spend

    form = await read_form(request)
    account = (form.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    # to_thread, а не starlette: свежая установка не должна зависеть от
    # пакета, которого нет в requirements.txt
    ok, message = await asyncio.to_thread(account_spend.open_for_automation, account)
    # ACL-1518: окно, открытое руками, теперь переоткрывается путём Publish.
    # ACL-1562: сразу, без условий. Мягкая остановка не закрыла окно — это
    # не конфликт, а простой ответ строке, как у чтения: не 409.
    if not ok and getattr(message, "code", "") == account_spend.HAND_OPEN_CODE:
        return JSONResponse({"ok": False, "message": str(message), "code": account_spend.HAND_OPEN_CODE})
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)


_OPEN_INFLIGHT: "dict[str, asyncio.Future]" = {}
# Короче клиентских 75 с: тогда человек видит ответ сервера, а не свой таймаут.
OPEN_WAIT_S = 70.0
OPEN_TIMEOUT_MESSAGE = ("The profile opened but did not answer \u2014 check its window in Multilogin "
                        "(login, checkpoint), then press Connect again.")
OPEN_ALREADY_MESSAGE = ("This profile is already being opened \u2014 wait for it to finish, "
                        "then press Connect again.")


@app.post("/mlx-profile-open")
async def mlx_profile_open(request: Request) -> JSONResponse:
    """Поднять найденный в Multilogin профиль — ещё до подключения аккаунта.

    Порядок работы человека: нашёл профиль в поиске, открыл, посмотрел в самом
    кабинете что нужно, подключил. Прежде дешборд соглашался открыть профиль
    только у аккаунта, который уже в карте, — а чтобы попасть в карту, он же
    требовал номер кабинета, который знает открытый профиль. Круг замкнутый, и
    выйти из него можно было только вписав номер руками.
    """
    from src.analysis_agent import account_spend

    form = await read_form(request)
    profile_id = (form.get("profile_id") or "").strip()
    if not profile_id:
        return JSONResponse({"ok": False, "error": "profile_id is required"}, status_code=400)
    # ACL-1588: один открывающий вызов на profile_id. Клиент перестаёт ждать по
    # таймауту, а поток открытия живёт дальше; без этой защиты «press Connect
    # again» запускал второе открытие того же профиля.
    existing = _OPEN_INFLIGHT.get(profile_id)
    if existing is not None and not existing.done():
        return JSONResponse({"ok": False, "already_opening": True,
                             "message": OPEN_ALREADY_MESSAGE}, status_code=409)
    task = asyncio.ensure_future(asyncio.to_thread(
        account_spend.open_profile_for_automation,
        profile_id,
        (form.get("folder_id") or "").strip(),
        (form.get("instance") or "A").strip(),
        (form.get("name") or "").strip(),
    ))
    _OPEN_INFLIGHT[profile_id] = task

    def _release(done, key=profile_id):
        if _OPEN_INFLIGHT.get(key) is done:
            _OPEN_INFLIGHT.pop(key, None)
        if not done.cancelled():
            done.exception()  # прочитано, чтобы asyncio не ругался на «забытое»

    task.add_done_callback(_release)
    try:
        # Ожидание ограничено, но поток не отменяется и профиль не убивается и
        # не перезапускается: просто отвечаем раньше, чем клиент потеряет терпение.
        ok, message = await asyncio.wait_for(asyncio.shield(task), OPEN_WAIT_S)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "timeout": True,
                             "message": OPEN_TIMEOUT_MESSAGE}, status_code=409)
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)


@app.post("/account-probe/ad-account")
async def account_probe_ad_account(request: Request) -> JSONResponse:
    """Спросить у открытого профиля, какой у него рекламный кабинет.

    Подключая аккаунт, дешборд до сих пор просил человека вставить номер, если
    профиль на этой машине не открывали. Номер знает сам кабинет — Ads Manager
    называет его в своём адресе.
    """
    from src.analysis_agent import account_spend

    form = await read_form(request)
    profile_id = (form.get("profile_id") or "").strip()
    if not profile_id:
        return JSONResponse({"ok": False, "error": "profile_id is required"}, status_code=400)
    found = await asyncio.to_thread(
        account_spend.read_ad_account,
        profile_id,
        (form.get("folder_id") or "").strip(),
        (form.get("instance") or "A").strip(),
    )
    return JSONResponse(found)


@app.get("/account-spend/status")
async def account_spend_status(
    account: str = "", date_from: str = "", date_to: str = "", span: str = ""
) -> JSONResponse:
    from src.analysis_agent import account_spend, country_split

    try:
        date_from, date_to = _spend_period(date_from, date_to, span)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if account.strip():
        # Пострановое чтение отдаётся здесь же, а не отдельным маршрутом.
        # Оба показания живут в одной клетке строки и снимаются за один и тот
        # же период; второй опрос ходил бы за тем же аккаунтом в те же
        # секунды и удваивал бы запросы ради разделения, которого на экране
        # нет. `split` отсутствует, пока страны у этого аккаунта не читали.
        reading = account_spend.get_reading(account.strip(), date_from, date_to)
        answer = {
            "ok": True,
            "reading": reading.to_dict(),
            "split": country_split.get_split(account.strip(), date_from, date_to),
            # ACL-1518/1562: строка «окно открыто руками — переоткрываем», пока
            # оно переоткрывается (чтение спенда, стран или «open profile»).
            # Пусто — не переоткрываем. Ключ прежний, его читает страница.
            "waiting": account_spend.hand_wait_note(account.strip()),
        }
        # ACL-1288: решение о странах едет вместе с готовым чтением — по нему
        # его и принимают. Пока чтение идёт, решать не по чему: у записи
        # «читаю» нет суммы, с которой сравнивают разбивку.
        if reading.state == "ok":
            answer["plan"] = _split_plan(account.strip(), date_from, date_to,
                                         reading=reading, split=answer["split"])
        return JSONResponse(answer)
    return JSONResponse(
        {
            "ok": True,
            "date_from": date_from,
            "date_to": date_to,
            "running": account_spend.running_now(),
            "readings": {
                a: r.to_dict()
                for a, r in account_spend.all_readings(date_from, date_to).items()
            },
        }
    )


# ───────────────────────── обновление структуры, не зависящее от дат (ACL-2063) ─────────────────────────


@app.post("/account-refresh/page-open")
async def account_refresh_page_open(request: Request) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool
    from src.analysis_agent import account_refresh as refresh, campaign_inventory as ci

    if not ci.backend_ready():
        return JSONResponse({"ok": False, "code": ci.NEEDS_API_CODE, "error": ci.NEEDS_API_MESSAGE}, status_code=409)
    form = await read_form(request)
    accounts = refresh.dashboard_accounts()
    if accounts:
        refused = _mlx_batch_refusal(form, accounts[0])
        if refused is not None:
            return refused
    try:
        q = refresh.queue()
        result = await run_in_threadpool(q.page_open, port=request.url.port or 80,
                                        view_only=str(form.get("view_only") or "").lower() in {"1", "true"})
        q.start()
    except (OSError, RuntimeError, ValueError):
        return JSONResponse({"ok": False, "error": "Account refresh could not start. Check saved settings and retry."}, status_code=503)
    return JSONResponse({"ok": True, **result})


@app.post("/account-refresh/request")
async def account_refresh_request(request: Request) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool
    from src.analysis_agent import account_refresh as refresh, campaign_inventory as ci

    form = await read_form(request)
    trigger = str(form.get("trigger") or "").strip()
    if trigger not in {"attention", "retry"}:
        return JSONResponse({"ok": False, "error": "trigger must be attention or retry"}, status_code=400)
    if not ci.backend_ready():
        return JSONResponse({"ok": False, "code": ci.NEEDS_API_CODE, "error": ci.NEEDS_API_MESSAGE}, status_code=409)
    account = refresh.resolve_account(str(form.get("account") or ""))
    if account is None:
        return JSONResponse({"ok": False, "error": "This account is no longer on the dashboard."}, status_code=404)
    refused = _mlx_batch_refusal(form, account)
    if refused is not None:
        return refused
    try:
        q = refresh.queue()
        result = await run_in_threadpool(q.request, account, trigger, force=trigger == "retry")
        q.start()
    except (OSError, RuntimeError, ValueError):
        return JSONResponse({"ok": False, "error": "Account refresh could not be queued. Check saved settings and retry."}, status_code=503)
    return JSONResponse({"ok": True, **result})


@app.get("/account-refresh/state")
@_off_loop
def account_refresh_state() -> JSONResponse:
    from src.analysis_agent import account_refresh as refresh, campaign_inventory as ci

    try:
        accounts = refresh.dashboard_accounts()
        state = refresh.queue().state()
    except (OSError, RuntimeError, ValueError):
        return JSONResponse({"ok": False, "error": "Saved account refresh state is unavailable. Check the settings backup."}, status_code=503)
    state["rows"] = {a: value for a, value in state["rows"].items() if a in accounts}
    return JSONResponse({"ok": True, **state, "accounts": ci.summaries(accounts),
                         "needs_api": not ci.backend_ready()})


# ───────────────────────── опись кампаний для Home (ACL-1798) ─────────────────────────
# Только чтение. Чтение Meta запускает ТОЛЬКО POST /campaign-inventory/read (по
# нажатию человека, через ту же очередь, что Read spend). Остальные маршруты —
# локальные: берут сохранённый снимок и книгу статистики, Meta не трогают.


def _inventory_bi(account: str, countries: list[str], date_from: str, date_to: str, span: str):
    """BI из книги для аккаунта: `(данные | None, пометка)`. Сбой книги — не сбой описи."""
    from src.analysis_agent.campaign_inventory import merge_attribution
    from src.analysis_agent.morning_report import build_tree_attribution

    names = [c.strip() for c in countries if c and c.strip()]
    if not names:
        return None, "This account is not placed in a country, so no workbook rows can be matched"
    try:
        _report, df, _warnings = get_cached_report()
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the workbook: {exc}"
    if df is None:
        return None, "The stats workbook has no data"
    date_from, date_to, span = date_from.strip(), date_to.strip(), span.strip()
    if not date_from and not date_to and span != "all":
        date_from, date_to = _month_to_date(_workbook_dates(df))
    frame = _filter_by_period(df, date_from, date_to) if (date_from or date_to) else df
    parts = []
    for country in dict.fromkeys(names):
        try:
            alias = workbook_alias_for(df, country, account)
            got = build_tree_attribution(frame, country, alias)
        except Exception as exc:  # noqa: BLE001
            return None, f"Workbook numbers failed: {exc}"
        if got.get("has_data"):
            parts.append(got)
    if not parts:
        return {"campaigns": {}, "ads": {}}, "The workbook has no rows for this account in the period"
    return merge_attribution(parts), ""


def _inventory_goal_context(countries: list[str]) -> dict:
    """Цель CPA (My Goals) и бенчмарк CPA страны — вход для колонок Goal/Benchmark CPA и подсказок.

    Только чтение локальных настроек, Meta и книгу не трогает. Нет значения — `None` и
    причина словами: таблица пишет её рядом с тире, а не выдумывает число.
    """
    country = next((c.strip() for c in countries if c and c.strip()), "")
    out: dict = {"country": country, "goal_cpa": None, "goal_ftd": None, "benchmark_cpa": None,
                 "goal_note": "", "benchmark_note": ""}
    if not country:
        out["goal_note"] = out["benchmark_note"] = "This account is not placed in a country"
        return out
    try:
        from src.web import goals as goals_store
        from src.web.buyer import default_cm

        who = (os.environ.get("MY_CM_NAME") or default_cm() or "").strip()
        goal = next((g for g in goals_store.for_cm(who, []) if (g.get("country") or "").strip().casefold()
                     == country.casefold()), None)
        cpa = float(goal.get("cpa") or 0) if goal else 0.0
        if cpa > 0:
            out["goal_cpa"] = cpa
            out["goal_ftd"] = int(goal.get("ftd") or 0) or None
        else:
            out["goal_note"] = f"No CPA goal is set for {country} in My Goals"
    except Exception:  # noqa: BLE001 — цели не должны ронять опись
        out["goal_note"] = "My Goals could not be read"
    try:
        from config.benchmarks import get_benchmark

        cap = get_benchmark(country).get("max_cpa")
        if cap is not None and float(cap) > 0:
            out["benchmark_cpa"] = float(cap)
        else:
            out["benchmark_note"] = f"No benchmark CPA is set for {country}"
    except Exception:  # noqa: BLE001
        out["benchmark_note"] = "The benchmark could not be read"
    return out


def _inventory_context(request: Request, account: str):
    """Снимок аккаунта, BI и сводка соединения — для маршрутов описи (без Meta)."""
    from src.analysis_agent import campaign_inventory as ci

    q = request.query_params
    snap = ci.get_snapshot(account)
    if snap is None:
        return None, {}, {"available": False, "note": ""}, {}
    bi, note = _inventory_bi(account, q.getlist("country"), q.get("date_from", ""),
                             q.get("date_to", ""), q.get("span", ""))
    joined, bi_summary = ci.join_bi(snap, bi)
    bi_summary["note"] = note
    return snap, joined, bi_summary, {}


@app.post("/campaign-inventory/read")
async def campaign_inventory_read(request: Request) -> JSONResponse:
    """Прочитать опись кампаний аккаунта. Только по нажатию, только `ACL_FB_BACKEND=api`.

    Тот же принцип, что у /account-spend/read: никакого расписания и опроса
    Meta. С бэкендом `rpa` отвечает отказом и не запускает ничего.
    """
    from src.analysis_agent import campaign_inventory as ci

    form = await read_form(request)
    account = (form.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    skipped = _mlx_batch_refusal(form, account)
    if skipped is not None:
        return skipped
    try:
        date_from, date_to = _spend_period(str(form.get("date_from") or ""), str(form.get("date_to") or ""),
                                           str(form.get("span") or ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        job = ci.start_read(account, date_from, date_to)
    except ci.NeedsApiBackend:
        return JSONResponse({"ok": False, "code": ci.NEEDS_API_CODE, "error": ci.NEEDS_API_MESSAGE},
                            status_code=409)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "Could not start the campaign read. Retry."},
                            status_code=503)
    return JSONResponse({"ok": True, **job})


@app.get("/campaign-inventory/status")
@_off_loop
def campaign_inventory_status(request: Request) -> JSONResponse:
    """Какие аккаунты читаются сейчас и что сохранено по каждому (сводки, без объектов)."""
    from src.analysis_agent import campaign_inventory as ci

    backend_ok = ci.backend_ready()
    accounts = [a for a in request.query_params.getlist("account") if a.strip()]
    return JSONResponse({
        "ok": True,
        "backend": "api" if backend_ok else "rpa",
        "needs_api": not backend_ok,
        "message": "" if backend_ok else ci.NEEDS_API_MESSAGE,
        "running": ci.running_now(),
        "accounts": ci.summaries(accounts),
    })


@app.get("/campaign-inventory/tree")
@_off_loop
def campaign_inventory_tree(request: Request) -> JSONResponse:
    """Кампании аккаунта из снимка с BI по точному имени. Дети — отдельным запросом."""
    from src.analysis_agent import campaign_inventory as ci

    account = request.query_params.get("account", "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    snap, joined, bi_summary, _x = _inventory_context(request, account)
    if snap is None:
        return JSONResponse({"ok": True, "has_snapshot": False, "summary": ci.summary(None, ci.get_attempt(account)),
                             "rows": []})
    return JSONResponse({
        "ok": True,
        "has_snapshot": True,
        "summary": ci.summary(snap, ci.get_attempt(account)),
        "bi": bi_summary,
        "context": _inventory_goal_context(request.query_params.getlist("country")),
        "rows": ci.campaign_rows(snap, joined),
    })


@app.get("/campaign-inventory/children")
@_off_loop
def campaign_inventory_children(request: Request) -> JSONResponse:
    """Дети одного объекта из снимка: ad sets кампании или объявления ad set."""
    from src.analysis_agent import campaign_inventory as ci

    q = request.query_params
    account, level, parent = q.get("account", "").strip(), q.get("level", "").strip(), q.get("parent", "").strip()
    if not account or not parent or level not in ("adset", "ad"):
        return JSONResponse({"ok": False, "error": "account, parent and level (adset|ad) are required"},
                            status_code=400)
    snap, joined, _bi_summary, _x = _inventory_context(request, account)
    if snap is None:
        return JSONResponse({"ok": True, "state": "not_read", "rows": [],
                             "note": "This account has not been read yet"})
    return JSONResponse({"ok": True, **ci.child_rows(snap, level, parent, joined)})


@app.get("/account-attribution")
def account_attribution(
    account: str = "", country: str = "", date_from: str = "", date_to: str = "", span: str = ""
) -> JSONResponse:
    """Числа книги по кампаниям и объявлениям одного аккаунта (ACL-1256).

    Та же выборка, что у строки аккаунта на карте: страна, имя из книги
    (`workbook_alias_for`) и период — пустой период значит «месяц по текущее
    число», как у карты. Значения отформатированы здесь, тем же `fmt_usd`, что
    и строка аккаунта: одна сумма не должна выглядеть по-разному в двух
    строках одной таблицы.
    """
    from src.analysis_agent.morning_report import build_tree_attribution

    account, country = account.strip(), country.strip()
    if not account or not country:
        return JSONResponse({"ok": False, "error": "account and country are required"}, status_code=400)
    try:
        _report, df, _warnings = get_cached_report()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Could not read the workbook: {exc}"}, status_code=503)
    if df is None:
        return JSONResponse({"ok": True, "has_data": False, "campaigns": {}, "ads": {}})
    date_from, date_to, span = date_from.strip(), date_to.strip(), span.strip()
    if not date_from and not date_to and span != "all":
        date_from, date_to = _month_to_date(_workbook_dates(df))
    frame = _filter_by_period(df, date_from, date_to) if (date_from or date_to) else df
    alias = workbook_alias_for(df, country, account)
    try:
        got = build_tree_attribution(frame, country, alias)
    except Exception as exc:  # pragma: no cover - кривая книга не должна ронять дерево
        return JSONResponse({"ok": False, "error": f"Workbook numbers failed: {exc}"}, status_code=500)

    from src.analysis_agent import bi_compare
    source = bi_compare.provenance(frame=frame) if bi_compare.SOURCE_COLUMN in frame else _report.get("source_info")

    def _shown(m: dict) -> dict:
        # ACL-1652: те же шесть значений, что в таблицах; прочерк — это null,
        # его рисует сам экран (`nodata`). CR 0.0% при лидах — настоящее число.
        spend, _l, _f, cpl, cpa, cr = metric_columns.metric_values(m["spend"], m["leads"], m["deposits"])
        ratios = metric_columns.ratios(m["spend"], m["leads"], m["deposits"])
        spend, cpl, cpa = (source_display.money(value, source) for value in (m["spend"], ratios["cpl"], ratios["cpa"]))
        dash = metric_columns.DASH
        return {
            "spend": spend,
            "leads": m["leads"],
            "deposits": m["deposits"],
            "cpl": None if cpl == dash else cpl,
            "cpa": None if cpa == dash else cpa,
            "cr": None if cr == dash else cr,
        }

    return JSONResponse({
        "ok": True,
        "has_data": got["has_data"],
        "date_from": date_from,
        "date_to": date_to,
        "alias": alias,
        "source": source,
        "campaigns": {k: _shown(v) for k, v in got["campaigns"].items()},
        "ads": {c: {a: _shown(v) for a, v in per.items()} for c, per in got["ads"].items()},
    })


@app.get("/account-map")
@_off_loop
def account_map_fragment(
    request: Request, date_from: str = "", date_to: str = "", span: str = "",
    compare: str = "", cmp_from: str = "", cmp_to: str = "",
) -> Response:
    """The account map alone, recomputed for a period.

    A fragment rather than a page reload: rebuilding the whole report to change
    a date range costs six seconds and throws away the reader's scroll position
    for a question that only concerns one block.
    """
    try:
        report, df, _warnings = get_cached_report()
    except Exception as exc:
        return HTMLResponse(f'<div class="amap-empty">Could not read the workbook: {exc}</div>')

    from copy import deepcopy

    base_report = report
    period_cmp, compare_note = None, ""
    if compare.strip():
        date_from, date_to = resolve_period(df, date_from.strip(), date_to.strip(), span.strip())
        try:
            report = report_for_period(base_report, df, date_from, date_to)
        except PeriodReportError:
            return HTMLResponse('<div class="amap-empty">Could not calculate the selected period.</div>', status_code=503)
        period_cmp, compare_note = _build_compare(
            base_report, df, date_from, date_to, span.strip(),
            compare.strip(), cmp_from.strip(), cmp_to.strip())
    # Enrichment belongs to this response, never the shared workbook cache.
    report = deepcopy(report)
    _enrich_country_overview(report)
    return templates.TemplateResponse(
        request,
        "_account_map.html",
        {
            "request": request,
            "amap": _build_account_map_view(
                report, df, date_from.strip(), date_to.strip(), span.strip()
            ),
            "mlx": mlx_instance_labels(),
            "compare": period_cmp,
            "compare_fragment_note": compare_note,
        },
    )


@app.get("/account-map/available")
@_off_loop
def account_map_available_fragment(
    request: Request, date_from: str = "", date_to: str = "", span: str = ""
) -> Response:
    """Только блок Available accounts — после Remove/Restore (ACL-1319).

    Собирается тем же путём, что и Home: отчёт за период на экране и
    привязанные аккаунты. Фрагмент /account-map берёт отчёт без периода, и
    его список мог бы отличаться от того, что человек только что видел, —
    после «Remove» на месте списка появился бы другой.
    """
    import html as _html
    from copy import deepcopy

    try:
        report, df, _warnings = get_cached_report()
        date_from, date_to = resolve_period(df, date_from.strip(), date_to.strip(), span.strip())
        report = deepcopy(report_for_period(report, df, date_from, date_to))
        _add_linked_accounts_to_builder(report)
        _enrich_country_overview(report)
        view = _build_account_map_view(report, df, date_from, date_to, span.strip())
    except Exception as exc:
        return HTMLResponse(
            f'<div class="amap-empty">Could not rebuild Available accounts: {_html.escape(str(exc))}</div>',
            status_code=503,
        )
    return templates.TemplateResponse(
        request,
        "_account_map_available.html", {"request": request, "amap": view}
    )


def _goal_countries_now(who: str) -> list[str]:
    """Те же страны, что видит панель, — для чтения и правки целей по запросу.

    Только когда у человека ещё нет своих целей: иначе стартовый список не
    нужен и отчёт не трогаем. Отчёт полный (не за период) и про того, чьи
    цели правят. Синхронная — вызывать через run_in_threadpool: промах кэша
    пересобирает книгу.
    """
    from src.web import goals as goals_store

    if goals_store.has_stored(who):
        return []
    try:
        report, _df, _w = get_cached_report(my_cm=who) if who else get_cached_report()
    except Exception:
        report = None
    return _my_goal_countries(report, who)


@app.get("/goals")
async def goals_read(cm: str = "") -> JSONResponse:
    from src.web import goals as goals_store

    from starlette.concurrency import run_in_threadpool

    who = (cm or "").strip() or (os.environ.get("MY_CM_NAME") or "").strip()
    me = (os.environ.get("MY_CM_NAME") or "").strip()
    if who.casefold() != me.casefold() and not goals_store.is_team_lead():
        return JSONResponse(
            {"ok": False, "error": f"These are {who}'s goals. Only a team lead sees somebody else's."},
            status_code=403,
        )
    seed = await run_in_threadpool(_goal_countries_now, who)
    return JSONResponse({
        "ok": True,
        "cm": who,
        "goals": goals_store.for_cm(who, seed),
        "team_lead": goals_store.is_team_lead(),
    })


@app.get("/goals/overview")
async def goals_overview() -> JSONResponse:
    """Every manager's saved goals, read-only. Team lead only (ACL_TEAM_LEAD).

    Same agreement as editing: the flag keeps the UI honest, it is not a lock.
    """
    from src.web import goals as goals_store

    if not goals_store.is_team_lead():
        return JSONResponse(
            {"ok": False, "error": "Only a team lead sees everyone's goals."}, status_code=403)
    return JSONResponse({"ok": True, "managers": goals_store.overview()})


@app.post("/goals")
async def goals_write(request: Request) -> JSONResponse:
    """Add, change or drop one country's target.

    Body: {cm?, action: "save"|"delete", goal?: {...}, country?: str}

    Editing somebody else's goals needs the team-lead flag. That flag is an
    agreement, not a lock — the dashboard has no sign-in, so anyone who can
    reach the file can change any of it. It exists so the UI does not invite
    a mistake, not because it prevents one.
    """
    from src.web import goals as goals_store

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    me = (os.environ.get("MY_CM_NAME") or "").strip()
    who = (data.get("cm") or "").strip() or me
    if who.casefold() != me.casefold() and not goals_store.is_team_lead():
        return JSONResponse(
            {"ok": False, "error": f"These are {who}'s goals. Only a team lead edits somebody else's."},
            status_code=403,
        )

    action = (data.get("action") or "save").strip().lower()
    from starlette.concurrency import run_in_threadpool

    seed = await run_in_threadpool(_goal_countries_now, who)
    try:
        if action == "delete":
            country = (data.get("country") or "").strip()
            if not country:
                return JSONResponse({"ok": False, "error": "Which country?"}, status_code=400)
            return JSONResponse({"ok": True, "goals": goals_store.delete(who, country, seed)})
        goal = data.get("goal") or {}
        return JSONResponse({"ok": True, "goals": goals_store.upsert(who, goal, seed)})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse({"ok": False, "error": f"Could not save: {exc}"}, status_code=500)


@app.get("/mlx-profile-details")
async def mlx_profile_details(profile_id: str = "", name: str = "") -> JSONResponse:
    """What this machine already knows about a Multilogin profile.

    The ad account id is read from the profile's own browser history: every Ads
    Manager URL carries `act=<id>`, so the answer is on disk. Nothing is driven
    and nothing is fetched — polling a live account through its browser is what
    preceded a checkpoint on 2026-08-25.

    That also sets the limit: history exists only for a profile this machine has
    opened. A profile found by searching Multilogin's cloud but never opened
    here has no history, so the id comes back empty and somebody types it.
    """
    from src.fb_ads_rpa import mlx_profiles

    pid = (profile_id or "").strip()
    if not pid:
        return JSONResponse({"ok": False, "error": "profile_id is required"}, status_code=400)
    try:
        act = mlx_profiles.ad_account_id_for(pid)
    except Exception:
        act = ""
    try:
        folder = mlx_profiles.folder_id_for(pid)
    except Exception:
        folder = ""
    return JSONResponse({
        "ok": True,
        "ad_account_id": act,
        "folder_id": folder,
        "opened_here": bool(act or folder),
        "suggested_name": _account_name_from_profile(name),
    })


def _account_name_from_profile(profile_name: str) -> str:
    """Guess the ad account's name from the Multilogin profile's.

    The two are named by the same convention with different prefixes —
    `KLD_BFL_X01` is the profile for `FacebookMKTOBFLX01`. What ties them is the
    tail: a letter followed by digits. Matching on that is how the dashboard
    already pairs the two elsewhere, so a suggestion is worth offering — but only
    as a prefilled field somebody can correct, never as a silent decision.
    """
    import re as _re

    tail = _re.search(r"([A-Za-z]\d{2,4})\s*$", (profile_name or "").strip())
    if not tail:
        return ""
    map_path = get_ad_account_map_path()
    known = list(load_ad_account_map(map_path)) if os.path.isfile(map_path) else []
    wanted = tail.group(1).casefold()
    for label in known:
        if label.casefold().endswith(wanted):
            return label
    return ""


@app.post("/account-links")
async def account_links_edit(request: Request) -> JSONResponse:
    """Place an account in a country or an asset, or take it out again.

    Body: {action: "link"|"unlink"|"close", kind: "country"|"asset",
           value: str, account?: str, confirm?: bool}

    Nothing here deletes an account. "unlink" takes it out of one country;
    "close" retires the country and every account on it falls into Available
    accounts, keeping its profile, proxy and history. An account that is
    spending today is not moved without an explicit confirmation — the map is
    also how somebody notices that a country still has money running through it.
    """
    from src.web import account_links

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    action = (data.get("action") or "").strip().lower()
    kind = (data.get("kind") or "country").strip().lower()
    value = (data.get("value") or "").strip()
    account = (data.get("account") or "").strip()
    confirm = bool(data.get("confirm"))

    if kind not in account_links.KINDS:
        return JSONResponse({"ok": False, "error": f"Unknown kind {kind!r}"}, status_code=400)
    if not value:
        return JSONResponse({"ok": False, "error": "A country or asset name is required"}, status_code=400)

    try:
        if action == "add":
            # Страна без аккаунтов: сначала заводим место, потом подключаем в
            # него аккаунт из Multilogin. Обратный порядок требовал выбрать
            # аккаунт из уже известных — то есть до подключения нового.
            return JSONResponse({"ok": True, "changed": account_links.add_value(kind, value)})

        if action == "link":
            if not account:
                return JSONResponse({"ok": False, "error": "Pick an account first"}, status_code=400)
            added = account_links.link(account, kind, value)
            return JSONResponse({"ok": True, "changed": added})

        if action in ("unlink", "close"):
            spending = await _in_report_pool(
                _accounts_spending_today,
                [account] if action == "unlink" else account_links.accounts_for(kind, value),
            )
            if spending and not confirm:
                names = ", ".join(spending[:4]) + ("…" if len(spending) > 4 else "")
                return JSONResponse({
                    "ok": False,
                    "needs_confirm": True,
                    "error": f"{names} spent money today. Taking {'it' if len(spending) == 1 else 'them'} "
                             f"out does not stop anything — the campaigns keep running, "
                             f"they just stop being listed here.",
                }, status_code=409)

            if action == "unlink":
                if not account:
                    return JSONResponse({"ok": False, "error": "Pick an account first"}, status_code=400)
                return JSONResponse({"ok": True, "changed": account_links.unlink(account, kind, value)})

            # «Изменилось» — про страну, а не про освобождённые аккаунты:
            # у пустой страны их нет, и ответ «ничего не поменялось» после
            # её удаления был бы неправдой.
            existed = any(v.lower() == value.lower() for v in account_links.all_values(kind))
            freed = account_links.unlink_all_for(kind, value)
            return JSONResponse({"ok": True, "changed": existed, "freed": freed})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse({"ok": False, "error": f"Could not save: {exc}"}, status_code=500)

    return JSONResponse({"ok": False, "error": f"Unknown action {action!r}"}, status_code=400)


#: Больше, чем бывает аккаунтов у одного байера, и достаточно мало, чтобы
#: случайный огромный запрос не переписывал файл десятки тысяч раз.
_AVAILABLE_HIDE_MAX = 1000


@app.post("/api/available-accounts/hidden")
async def api_available_accounts_hidden(request: Request) -> JSONResponse:
    """Убрать аккаунты из Available accounts на этой машине или вернуть их.

    ACL-1319. Настройка вида одного списка: строка карты, профиль Multilogin,
    размещения и история не трогаются. Аккаунт, стоящий в стране, отсюда не
    убирается — сначала его надо вынуть из страны; такие приходят в `skipped`.

    Body JSON: {accounts: [str, ...], hidden: bool}
    """
    from src.web import account_links
    from src.web.available_accounts import alias_key

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Expected JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Expected a JSON object"}, status_code=400)
    accounts = body.get("accounts")
    hide = body.get("hidden", True)
    if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
        return JSONResponse({"ok": False, "error": "accounts must be a list of names"}, status_code=400)
    if not isinstance(hide, bool):
        return JSONResponse({"ok": False, "error": "hidden must be a boolean"}, status_code=400)
    names = [" ".join(a.split()) for a in accounts if a and a.strip()]
    if not names:
        return JSONResponse({"ok": False, "error": "Pick at least one account"}, status_code=400)
    if len(names) > _AVAILABLE_HIDE_MAX:
        return JSONResponse({"ok": False, "error": "Too many accounts in one request"}, status_code=400)

    skipped: list[str] = []
    try:
        if hide:
            placed = {alias_key(a) for a, _v in account_links.all_pairs("country") if alias_key(a)}
            skipped = [n for n in names if alias_key(n) in placed]
            changed = account_links.hide_accounts([n for n in names if n not in skipped])
        else:
            changed = account_links.restore_accounts(names)
        count = len(account_links.hidden_accounts())
    except OSError:
        return JSONResponse(
            {"ok": False, "error": "Could not save this machine's hidden accounts. Try again."},
            status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "hidden": hide,
        "changed": changed,
        "skipped": skipped,
        "hidden_count": count,
    })


def _accounts_spending_today(accounts: list[str]) -> list[str]:
    """Which of these accounts has spend in today's workbook."""
    try:
        report, _df, _warnings = get_cached_report()
    except Exception:
        return []
    wanted = {a.strip().casefold() for a in accounts if a and a.strip()}
    spending: set[str] = set()
    for country in report.get("countries", []):
        for row in country.get("account_rows") or []:
            label = (row.get("account_label") or "").strip()
            if label.casefold() not in wanted:
                continue
            excel = row.get("account_excel") or {}
            if (excel.get("spend") or 0) > 0:
                spending.add(label)
    return sorted(spending)


@app.post("/account-map-remove")
async def account_map_remove(request: Request) -> JSONResponse:
    """Disconnect an account from THIS machine. Body JSON: {account_name}.

    Removes the row from the machine-local map only. The account keeps
    existing in the company's sheet, keeps its history and its campaigns —
    this machine simply stops knowing how to reach it. Nothing here can or
    should delete a shared record.

    ACL-142: with `also_unlink: true` the removed accounts are also taken out
    of every country and asset they stand in on this machine ("Remove from
    ACL"). Without it only the map row goes, which is what the Accounts tab
    has always done. Taking an account out of ONE country is a different
    action: `/account-links` with `unlink`.
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
            {"ok": False, "error": "account_name or account_names is required"},
            status_code=400,
        )

    also_unlink = data.get("also_unlink") is True
    map_path = Path(get_ad_account_map_path())
    if not map_path.is_file() and not also_unlink:
        return JSONResponse({"ok": False, "error": "Account map not found"}, status_code=404)

    removed: list[str] = []
    kept: list = []
    unlinked: list[dict] = []

    def account_key(name: str) -> str:
        # Full removal uses the same account identity as the local placements.
        # Ordinary map-only removals retain their exact-name matching.
        return name.lower() if also_unlink else name

    try:
        if map_path.is_file():
            with open(map_path, encoding="utf-8-sig", newline="") as f:
                reader = _csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                rows = list(reader)

            target = {account_key(name) for name in wanted}
            kept = [r for r in rows if account_key((r.get("account_name") or "").strip()) not in target]
            removed = [
                (r.get("account_name") or "").strip() for r in rows
                if account_key((r.get("account_name") or "").strip()) in target
            ]
            if removed:
                write_csv_atomic(map_path, fieldnames, kept)

        # "Remove from ACL" must not depend on a map row: country rows come
        # from the placements, and an account can be placed with no row (a
        # workbook-only account, one already removed in the Accounts tab, or a
        # different letter case). Doing this independently also lets a retry
        # finish a removal whose placement write failed after the row was gone.
        if also_unlink:
            from src.web import account_links

            for name in dict.fromkeys(wanted):
                for kind in account_links.KINDS:
                    for place in account_links.values_for(name, kind):
                        if account_links.unlink(name, kind, place):
                            unlinked.append({"account": name, "kind": kind, "value": place})

        if not removed and not unlinked:
            shown = "«" + "», «".join(wanted[:3]) + "»"
            return JSONResponse(
                {"ok": False, "error": f"{shown} is not in the map of this machine"},
                status_code=404,
            )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # `missing` is reported rather than treated as failure: the rest were
    # removed, and pretending otherwise would invite a confused retry.
    removed_keys = {account_key(name) for name in removed}
    missing = sorted(name for name in set(wanted) if account_key(name) not in removed_keys)
    return JSONResponse(
        {"ok": True, "removed": removed, "missing": missing, "left": len(kept),
         "unlinked": unlinked}
    )


@app.post("/account-link-patch")
async def account_link_patch(request: Request) -> JSONResponse:
    """Save a local link, with an Excel fallback for workbook-only accounts.

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

    # Local settings win on reads, so Save must write that same source first.
    try:
        saved = _save_link_to_machine_map(account_name, link)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Could not save the link on this machine: {exc}"}, status_code=409)
    if saved:
        return JSONResponse({"ok": True, "account": account_name,
                             "link_status": classify_link(link), "stored_in": "machine"})

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
        # The workbook has no row for this account. That used to be the end of
        # it — a refusal, long after the account looked fully configured, for a
        # file the operator had not been told to edit. Save into the machine's
        # own account map instead: the workbook stays the shared source of
        # truth wherever it HAS a row, and an account connected on the
        # dashboard stops depending on someone editing a spreadsheet by hand.
        try:
            saved = _save_link_to_machine_map(account_name, link)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"Could not save the link on this machine: {exc}"},
                status_code=500,
            )
        if saved:
            return JSONResponse({
                "ok": True,
                "account": account_name,
                "link_status": classify_link(link),
                "stored_in": "machine",
                "note": "The account is not in Upload FB.xlsx — the link was saved in the map of this machine.",
            })
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Account \"{account_name}\" is in neither the Accounts sheet of "
                    f"{excel_path} nor the account map of this machine. "
                    "Connect the account in the Accounts section and try again."
                ),
            },
            status_code=404,
        )

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


# ── ACL-1434: здоровье робота ───────────────────────────────────────────────
# Только чтение локальной телеметрии и лога дешборда; к аккаунтам не ходит.
from datetime import datetime as _rh_datetime, timezone as _rh_timezone  # noqa: E402

_ROBOT_HEALTH_STARTED = _rh_datetime.now(_rh_timezone.utc).isoformat()


def _robot_health_output_dir() -> Path:
    from config.paths import get_runtime_workspace
    return get_runtime_workspace() / "output"


def _robot_health_current_build() -> tuple[str, str] | None:
    """Сборка этого процесса — для прогонов, которых лог ещё не назвал."""
    try:
        from src.fb_ads_rpa import telemetry_sync
        return telemetry_sync.flow_version(), _ROBOT_HEALTH_STARTED
    except Exception:
        return None


def _robot_health_logs_dir() -> Path:
    """Логи лаунчера установленной сборки (stdout/stderr сервера)."""
    from src.web import robot_health
    return robot_health.launcher_logs_dir()


# Обработчики синхронные намеренно: чтение всей телеметрии и логов плюс сводка
# идут в пуле потоков FastAPI и не держат event loop (опрос хода прогонов).
@app.get("/robot-health", response_class=HTMLResponse)
def robot_health_page(n: int = 10) -> HTMLResponse:
    from src.web import robot_health
    ctx = robot_health.page_context(_robot_health_output_dir(), n,
                                    _robot_health_current_build(),
                                    _robot_health_logs_dir())
    return HTMLResponse(templates.get_template("robot_health.html").render(**ctx))


@app.get("/robot-health/summary")
def robot_health_summary(n: int = 10) -> JSONResponse:
    """ACL-1827: данные для всплывающей панели «Robot health» в шапке (только чтение)."""
    from src.web import robot_health
    ctx = robot_health.popover_context(_robot_health_output_dir(), n,
                                       _robot_health_current_build(),
                                       _robot_health_logs_dir())
    return JSONResponse(ctx)


@app.get("/robot-health/run/{run_id}", response_class=HTMLResponse)
def robot_health_run(run_id: str, n: int = 10) -> HTMLResponse:
    from src.web import robot_health
    ctx = robot_health.run_context(_robot_health_output_dir(), run_id, n,
                                   _robot_health_current_build(),
                                   _robot_health_logs_dir())
    status = 404 if ctx["error"] else 200
    return HTMLResponse(templates.get_template("robot_health.html").render(**ctx),
                        status_code=status)


@app.get("/robot-health/capture/{run_id}/{name}")
def robot_health_capture(run_id: str, name: str) -> Response:
    from src.web import robot_health
    path = robot_health.capture_file(_robot_health_output_dir(), run_id, name)
    if path is None:
        return Response(status_code=404)
    return FileResponse(str(path))


def _teach_with_accounts(teach: dict) -> dict:
    """Name the account each waiting job belongs to (ACL-1142)."""
    from src.web import publish_jobs

    jobs = []
    for item in teach.get("jobs") or []:
        job = publish_jobs.get_job(item["job_id"]) or {}
        jobs.append({**item, "account_label": job.get("account_label") or ""})
    return {**teach, "jobs": jobs}


@app.get("/rpa-takeover")
async def rpa_takeover_status() -> JSONResponse:
    from src.fb_ads_rpa.pacing import is_takeover_active, get_teach_state
    teach = _teach_with_accounts(get_teach_state())
    return JSONResponse({"active": is_takeover_active(), "teach": teach})


@app.post("/rpa-teach/resume")
async def rpa_teach_resume(job_id: str = "") -> JSONResponse:
    """The user has clicked the wanted element by hand — resume ONE job.

    ACL-1142: the wait is per job. `job_id` names the job to release; without
    it the call works only while exactly one job waits, so that "continue" can
    never release the wrong account.
    """
    from src.fb_ads_rpa.pacing import end_teach_wait, get_teach_state
    state = get_teach_state()
    waiting = [j["job_id"] for j in state.get("jobs") or []]
    if not job_id:
        if len(waiting) > 1:
            return JSONResponse(
                {"ok": False, "error": "Several accounts are waiting - say which one (job_id)"},
                status_code=409,
            )
        job_id = waiting[0] if waiting else ""
    if not job_id or job_id not in waiting:
        return JSONResponse({"ok": False, "error": "Not in teach-mode wait"}, status_code=400)
    end_teach_wait(job_id=job_id)
    return JSONResponse({"ok": True, "message": "RPA resumed — capturing locator"})


@app.get("/rpa-teach/status")
async def rpa_teach_status(job_id: str = "") -> JSONResponse:
    from src.fb_ads_rpa.pacing import get_teach_state
    if job_id:
        return JSONResponse(_teach_with_accounts({**get_teach_state(job_id), "job_id": job_id}))
    return JSONResponse(_teach_with_accounts(get_teach_state()))


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


INSTANT_FORM_RPA_ERROR = ("Instant Forms are available only with the Graph API backend "
                          "(ACL_FB_BACKEND=api). Switch the ad set back to Website or use the API backend.")


def _instant_form_refusal(slots: list[dict]) -> JSONResponse | None:
    """ACL-1775: слот с Instant Form при RPA-бэкенде - отказ вслух, а не молча сайтом."""
    from src.web import publish_api

    if publish_api.api_enabled():
        return None
    if any(s.get("destination", "website") != "website" for s in slots):
        return JSONResponse({"error": INSTANT_FORM_RPA_ERROR}, status_code=409)
    return None


MULTI_ADSET_RPA_ERROR = ("Several ad sets need the API backend (ACL_FB_BACKEND=api). "
                         "The RPA backend builds one ad set per campaign; remove the extra ad sets "
                         "or use the API backend.")


def _several_adsets(slots: list[dict]) -> list[str]:
    """Имена кампаний, у которых больше одного набора (ACL-1768)."""
    seen: dict[str, set] = {}
    for slot in slots:
        k = slot.get("adset_index") or 0
        seen.setdefault(str(slot.get("name") or ""), set()).add(k if isinstance(k, int) else str(k))
    return [n for n, idx in seen.items() if len(idx) > 1]


def _multi_adset_refusal(slots: list[dict]) -> JSONResponse | None:
    """ACL-1768: больше одного набора при RPA-бэкенде - отказ вслух, лишний набор не отбрасывается молча."""
    from src.web import publish_api

    if publish_api.api_enabled():
        return None
    names = _several_adsets(slots)
    if names:
        return JSONResponse({"error": MULTI_ADSET_RPA_ERROR, "campaigns": names}, status_code=409)
    return None


#: Поля билдера, которые умеет только API-бэкенд (ACL-1767).
API_ONLY_SLOT_KEYS = {"adset_name": "Ad set name", "pixel_id": "Pixel of the ad set",
                      "gender": "Gender", "languages": "Several languages", "device": "Devices",
                      "os_min": "Minimum OS version", "start_time": "Start time", "cta": "CTA other than Learn more",
                      "copy_more": "Primary text 3-5 (with their headlines and descriptions)",
                      "creative_file_vertical": "Stories / Reels file"}


def _api_only_fields_refusal(slots: list[dict]) -> JSONResponse | None:
    """ACL-1767: заполненное поле только для API при бэкенде RPA - отказ вслух, не молча."""
    from src.web import publish_api

    if publish_api.api_enabled():
        return None
    names: list[str] = []
    for slot in slots:
        for key, title in API_ONLY_SLOT_KEYS.items():
            if slot.get(key) and title not in names:
                names.append(title)
        if str(slot.get("os") or "").strip().lower() == "android" and "Android only" not in names:
            names.append("Android only")
    if names:
        return JSONResponse({"error": "These fields work only with the Graph API backend "
                             "(ACL_FB_BACKEND=api): " + ", ".join(names) + ". Clear them or use the API backend."},
                            status_code=409)
    return None


@app.post("/api/account-pages")
async def api_account_pages(request: Request) -> JSONResponse:
    """Read available Pages under the existing profile read lock; no Meta writes."""
    from src.web.account_pages import cached_only, read_pages

    form = await request.form()
    account = str(form.get("account_label") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "state": "failed", "message": "account is required"}, status_code=400)
    if str(form.get("cached") or "") == "1":
        # Отрисовка билдера: только кэш на машине, профиль не открывается.
        return JSONResponse(await asyncio.to_thread(cached_only, account))
    # Без force — ответ из кэша на машине (12 ч): лишний запуск профиля ради списка Страниц не нужен.
    force = str(form.get("force") or "") == "1"
    return JSONResponse(await asyncio.to_thread(read_pages, account, force=force))


@app.post("/api/page-forms")
async def api_page_forms(request: Request) -> JSONResponse:
    """ACL-1775: формы Страницы аккаунта для выбора в билдере. Только по нажатию человека.

    Чтение без права записи; ни опроса, ни расписания. Только при `ACL_FB_BACKEND=api`.
    """
    from src.web import page_forms, publish_api

    form = await read_form(request, BUILDER_FORM)
    account = (form.get("account_label") or form.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    if not publish_api.api_enabled():
        return JSONResponse({"ok": False, "error": "Instant Forms need the Graph API backend (ACL_FB_BACKEND=api)."},
                            status_code=409)
    if "page_id" in form:
        return JSONResponse(await asyncio.to_thread(page_forms.read_forms, account, str(form.get("page_id") or "").strip()))
    return JSONResponse(await asyncio.to_thread(page_forms.read_forms, account))


@app.get("/api/form-templates")
def api_form_templates_list(force: bool = False) -> JSONResponse:
    """ACL-1776/2054: шаблоны Instant Form из общей библиотеки (в Meta не ходит).

    `templates` - годные к публикации (выбор в билдере); `rows` - все записи для
    вкладки Forms, в том числе invalid; `deleted`, `library`, `migration` - для неё же.
    `force` перечитывает библиотеку: вкладка открывается с ним, билдер - без.
    """
    from src.fb_api import form_templates

    try:
        view = form_templates.listing(force=force)
    except form_templates.TemplateError as exc:
        return JSONResponse({"ok": False, "errors": exc.errors}, status_code=503)
    except OSError:
        return JSONResponse({"ok": False, "errors": ["The form template library could not be read on this computer. Try again."]},
                            status_code=503)
    valid = {r["id"]: r["template"] for r in view["rows"] if not r["invalid"]}
    return JSONResponse({"ok": True, "templates": sorted(valid.values(), key=lambda t: t["name"].casefold()),
                         "rows": view["rows"], "deleted": view["deleted"], "library": view["library"],
                         "migration": view["migration"],
                         "limits": {"questions": form_templates.MAX_QUESTIONS}})


def _form_templates_failure(exc) -> JSONResponse:
    from src.fb_api import form_templates

    code = 503 if isinstance(exc, form_templates.LibraryUnavailable) else (
        409 if isinstance(exc, form_templates.TemplateConflict) else 400)
    return JSONResponse({"ok": False, "errors": exc.errors}, status_code=code)


async def _form_templates_call(request: Request, fn):
    """Общий каркас действий над шаблоном: тело -> id -> вызов в потоке -> ответ."""
    from src.fb_api import form_templates

    try:
        raw = await request.json()
    except ValueError:
        raw = {}
    tid = str((raw or {}).get("id") or "") if isinstance(raw, dict) else ""
    try:
        out = await asyncio.to_thread(fn, tid)
    except form_templates.TemplateError as exc:
        return _form_templates_failure(exc)
    except OSError:
        return JSONResponse({"ok": False, "errors": ["The template could not be saved on this computer"]},
                            status_code=500)
    return JSONResponse({"ok": True, **out})


@app.post("/api/form-templates")
async def api_form_templates_save(request: Request) -> JSONResponse:
    """ACL-1776: сохранить шаблон (ошибки проверки - 400, по-английски). Ничего в Meta."""
    from src.fb_api import form_templates

    try:
        raw = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "errors": ["The template could not be read"]}, status_code=400)
    try:
        saved = await asyncio.to_thread(form_templates.save, raw)
    except form_templates.TemplateError as exc:
        return _form_templates_failure(exc)
    except OSError:
        return JSONResponse({"ok": False, "errors": ["The template could not be saved on this computer"]},
                            status_code=500)
    return JSONResponse({"ok": True, "template": saved})


@app.post("/api/form-templates/delete")
async def api_form_templates_delete(request: Request) -> JSONResponse:
    """ACL-1776/2054: убрать шаблон в Recently deleted. Формы на Страницах не трогаются никогда."""
    from src.fb_api import form_templates

    resp = await _form_templates_call(request, lambda tid: {"removed": form_templates.delete(tid)})
    body = json.loads(resp.body)
    if resp.status_code == 200:
        return JSONResponse({"ok": bool(body.get("removed"))})
    return resp


@app.post("/api/form-templates/restore")
async def api_form_templates_restore(request: Request) -> JSONResponse:
    from src.fb_api import form_templates

    return await _form_templates_call(request, lambda tid: {"template": form_templates.restore(tid)})


@app.post("/api/form-templates/duplicate")
async def api_form_templates_duplicate(request: Request) -> JSONResponse:
    from src.fb_api import form_templates

    return await _form_templates_call(request, lambda tid: {"template": form_templates.duplicate(tid)})


@app.post("/api/form-templates/migrate")
async def api_form_templates_migrate(request: Request) -> JSONResponse:
    """ACL-2054: загрузить локальные шаблоны машины в общую библиотеку (явное действие)."""
    from src.fb_api import form_templates

    return await _form_templates_call(request, lambda _tid: form_templates.migrate_local())


@app.post("/preview-publish")
async def preview_publish(request: Request) -> JSONResponse:
    """Dry-run: convert form slots → PublishJob plan, return JSON (no Meta API calls)."""
    form = await read_form(request, BUILDER_FORM)
    country, account_label, slots = _parse_build_upload_form(form)
    refused = (_multi_adset_refusal(slots) or _instant_form_refusal(slots)
               or _api_only_fields_refusal(slots))
    if refused is not None:
        return refused
    if not country:
        return JSONResponse({"error": "Country not set — check form."}, status_code=400)
    if not account_label:
        return JSONResponse({"error": "Account label not set — check form."}, status_code=400)
    if not slots:
        return JSONResponse({"error": "No campaign slots found. Make sure the form has at least one campaign."}, status_code=400)
    def plan_for() -> list:
        if use_rpa_publish():
            pub = RpaPublisher(dry_run=True)
            drafts = pub.build_drafts(
                slots,
                account_label=account_label,
                country=country,
                preview_mode=True,
            )
            return preview_campaign_drafts(drafts)
        return preview_jobs(slots, account_label=account_label, country=country)

    try:
        # ACL-1399: превью тянет библиотеки и книгу — в потоке, не в цикле.
        plan = await asyncio.to_thread(plan_for)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"plan": plan, "account": account_label, "country": country})


@app.post("/publish-jobs")
async def publish_jobs_start(request: Request) -> JSONResponse:
    """Launch a background publish job. Returns job_id for polling.

    `request_id` (ACL-1399): ключ отправки со страницы. Повтор с тем же
    ключом — после «сервер не ответил» — возвращает то же задание.
    """
    form = await read_form(request, BUILDER_FORM)
    country, account_label, slots = _parse_build_upload_form(form)
    refused = (_multi_adset_refusal(slots) or _instant_form_refusal(slots)
               or _api_only_fields_refusal(slots))
    if refused is not None:
        return refused
    via_ml = (form.get("via_multilogin") or "1").strip() not in ("0", "false", "no")
    activate = (form.get("activate") or "0").strip() in ("1", "true", "yes")
    dry_run = (form.get("dry_run") or "0").strip() in ("1", "true", "yes")
    user_confirmed = (form.get("user_confirmed") or "0").strip() in ("1", "true", "yes")
    request_id = (form.get("request_id") or "").strip()
    if activate:
        return JSONResponse(
            {"error": "Live activation is disabled: ACM creates draft-only campaigns."},
            status_code=403,
        )
    if not user_confirmed:
        return JSONResponse(
            {"error": "Publish blocked: confirm in the dashboard UI first (user_confirmed=0)."},
            status_code=403,
        )
    if not country or not account_label or not slots:
        return JSONResponse({"error": "Missing country, account or slots."}, status_code=400)
    skipped = _mlx_batch_refusal(form, account_label)
    if skipped is not None:
        body = json.loads(skipped.body)
        return JSONResponse({"error": body["error"], "code": body["code"], "skipped": True}, status_code=409)

    def launch() -> tuple[str, int]:
        # Черновики собираются из библиотек и книги — не в цикле событий:
        # иначе запуск публикации ждал бы (и держал бы) весь сервер.
        job_id = start_job(
            account_label,
            country,
            slots,
            via_multilogin=via_ml,
            activate=False,
            dry_run=dry_run,
            # The server owns this safety decision; a client cannot opt out.
            hand_off=True,
            request_id=request_id,
        )
        # Сколько кампаний — из самого задания: start_job уже собрал черновики
        # (ревью PR #408, R8 — второй сборки не нужно).
        job = get_job(job_id) or {}
        return job_id, int(job.get("total") or len(slots))

    try:
        job_id, total = await asyncio.to_thread(launch)
    except DuplicateJobError as exc:
        # ACL-1603: у аккаунта уже есть идущее или ждущее задание — второго нет.
        return JSONResponse({"error": str(exc), "duplicate": True, "account": exc.account_label,
                             "job_id": exc.job_id}, status_code=409)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"job_id": job_id, "total": total})


@app.post("/publish-plan")
async def publish_plan(request: Request) -> JSONResponse:
    """ACL-1761: «Check plan (no writes)» — предпроверка и план, ни одной записи.

    Только при `ACL_FB_BACKEND=api` и только с явным `dry_run=1`. Задания
    публикации не заводит: путь чтения ACL-1760 (свой замок чтения, профиль —
    как у Read spend), клиент без права записи.
    """
    from src.web import publish_api

    form = await read_form(request, BUILDER_FORM)
    if (form.get("dry_run") or "").strip() != "1":
        return JSONResponse({"error": "Check plan only runs as a dry run (dry_run=1)."}, status_code=400)
    if not publish_api.api_enabled():
        return JSONResponse({"error": "Check plan needs the Graph API backend (ACL_FB_BACKEND=api)."},
                            status_code=409)
    country, account_label, slots = _parse_build_upload_form(form)
    if not country or not account_label or not slots:
        return JSONResponse({"error": "Missing country, account or slots."}, status_code=400)
    result = await asyncio.to_thread(publish_api.check_plan, slots, account_label=account_label,
                                     country=country)
    return JSONResponse(result)


# ACL-1771: подтверждённые соответствия «термин библиотеки -> id Meta» (локальный кэш резолвера).
# Только файл на этой машине: в Meta ничего не уходит. Тот же вход, что у /publish-plan:
# локальный адрес (общий middleware) и бэкенд API; подтвердить можно только кандидата,
# показанного в последнем Check plan для этого термина.
async def _targeting_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


@app.get("/publish-plan/waiting")
async def publish_plan_waiting(request: Request) -> JSONResponse:
    """Пока Check plan идёт: какие аккаунты ждут свободного места профиля.

    Строка та же, что у Read spend («Waiting for a free profile slot», «Opening …»):
    её ставит очередь профилей (`account_spend_api._open_gated`). Только память
    сервера — ни MLX, ни браузера, ни Meta.
    """
    from src.analysis_agent import account_spend

    labels = [a.strip() for a in request.query_params.getlist("account") if a.strip()][:50]
    notes = {label: account_spend.hand_wait_note(label) for label in labels}
    return JSONResponse({"waiting": {k: v for k, v in notes.items() if v}})


# ACL-1800: команды над существующими кампаниями (статус/бюджет/ставка) — только
# при ACL_FB_BACKEND=api, только по клику человека и с подтверждением превью.
@app.post("/campaign-commands/preview")
async def campaign_commands_preview(request: Request) -> JSONResponse:
    from src.web import campaign_commands

    body = await _campaign_commands_body(request)
    data, code = await asyncio.to_thread(campaign_commands.run_preview, body)
    return JSONResponse(data, status_code=code)


@app.post("/campaign-commands/execute")
async def campaign_commands_execute(request: Request) -> JSONResponse:
    from src.web import campaign_commands

    body = await _campaign_commands_body(request)
    data, code = await asyncio.to_thread(campaign_commands.run_execute, body)
    return JSONResponse(data, status_code=code)


@app.get("/campaign-commands/availability")
async def campaign_commands_availability() -> JSONResponse:
    from src.web import campaign_commands

    return JSONResponse(campaign_commands.availability())


@app.get("/campaign-commands/history")
async def campaign_commands_history(request: Request) -> JSONResponse:
    """Журнал команд аккаунта; объявлен до `/campaign-commands/{op_id}`."""
    from src.web import campaign_commands

    try:
        limit = int(request.query_params.get("limit") or 20)
    except ValueError:
        limit = 20
    data, code = await asyncio.to_thread(
        campaign_commands.account_history, request.query_params.get("account") or "", limit)
    return JSONResponse(data, status_code=code)


@app.get("/campaign-commands/{op_id}")
async def campaign_commands_status(op_id: str) -> JSONResponse:
    from src.web import campaign_commands

    data, code = campaign_commands.get_status(op_id)
    return JSONResponse(data, status_code=code)


async def _campaign_commands_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _targeting_gate() -> JSONResponse | None:
    from src.web import publish_api

    if not publish_api.api_enabled():
        return JSONResponse({"error": "Targeting confirmation needs the Graph API backend (ACL_FB_BACKEND=api)."},
                            status_code=409)
    return None


@app.get("/targeting-resolver/cache")
async def targeting_resolver_cache() -> JSONResponse:
    from src.fb_api.targeting_resolver import ConfirmedStore

    blocked = _targeting_gate()
    if blocked:
        return blocked
    return JSONResponse({"entries": ConfirmedStore().entries()})


@app.post("/targeting-resolver/confirm")
async def targeting_resolver_confirm(request: Request) -> JSONResponse:
    """Человек подтвердил кандидата Check plan: запомнить (тип, термин) -> id + имя."""
    from src.fb_api import targeting_resolver as tr

    blocked = _targeting_gate()
    if blocked:
        return blocked
    body = await _targeting_body(request)
    term, cid, ctype = str(body.get("term") or ""), str(body.get("id") or ""), str(body.get("type") or "")
    cand = tr.candidate_for(term, cid, ctype)
    if cand is None:
        return JSONResponse({"error": "That pick was not among the candidates of the last Check plan "
                                      "for this term. Run Check plan and choose from its list."},
                            status_code=400)
    try:
        # Имя и состав берутся у кандидата, который показал сервер, а не из запроса.
        row = tr.ConfirmedStore().put(term, id=cand["id"], type=cand["type"], name=cand["name"],
                                      picks=cand.get("picks"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "entry": row})


@app.post("/targeting-resolver/clear")
async def targeting_resolver_clear(request: Request) -> JSONResponse:
    """Забыть один подтверждённый термин (`term`) или все (`all: true`)."""
    from src.fb_api.targeting_resolver import ConfirmedStore

    blocked = _targeting_gate()
    if blocked:
        return blocked
    body = await _targeting_body(request)
    store = ConfirmedStore()
    if body.get("all") is True:
        return JSONResponse({"ok": True, "removed": store.clear()})
    term = str(body.get("term") or "")
    if not term.strip():
        return JSONResponse({"error": "Give a term, or all=true."}, status_code=400)
    return JSONResponse({"ok": True, "removed": 1 if store.remove(term) else 0})


# ACL-1812: тёплые сессии. Состояние строк — только реестр и процессы на машине
# (ни одного вызова к Meta); запуск и закрытие профилей — по клику человека.
@app.get("/warm/state")
async def warm_state(request: Request) -> JSONResponse:
    from src.web import warm_api

    labels = request.query_params.getlist("account")
    return JSONResponse(await asyncio.to_thread(warm_api.state, labels))


@app.post("/warm/open")
async def warm_open(request: Request) -> JSONResponse:
    from src.web import warm_api

    body = await _campaign_commands_body(request)
    data, code = await asyncio.to_thread(warm_api.start_open, body.get("accounts"))
    return JSONResponse(data, status_code=code)


@app.get("/warm/job/{job_id}")
async def warm_job(job_id: str) -> JSONResponse:
    from src.web import warm_api

    data, code = warm_api.job_status(job_id)
    return JSONResponse(data, status_code=code)


@app.post("/warm/close")
async def warm_close(request: Request) -> JSONResponse:
    from src.web import warm_api

    body = await _campaign_commands_body(request)
    data, code = await asyncio.to_thread(warm_api.close_profiles, body.get("accounts"),
                                         body.get("confirm") is True)
    return JSONResponse(data, status_code=code)


@app.get("/publish-jobs/slots")
async def publish_jobs_slots() -> JSONResponse:
    """Сколько профилей публикуют разом и почему (ACL-1321).

    Объявлен до `/publish-jobs/{job_id}`: иначе «slots» ушло бы туда номером
    задания. Решение — по свободной памяти или по `ACL_MAX_PARALLEL_PROFILES`.
    """
    from src.web.publish_jobs import slots_snapshot

    return JSONResponse(slots_snapshot())


@app.get("/api/publish-preflight")
async def api_publish_preflight(accounts: str = "") -> JSONResponse:
    """Кто из аккаунтов не сможет работать — до постановки в очередь (ACL-1604).

    Один разбор на нажатие «Publish all», только по перечисленным аккаунтам.
    Профили не открываются и не трогаются: читаются карта, срок токена, статус
    кабинета, журнал прошлых прогонов и список открытых профилей (см.
    `publish_preflight`).
    """
    from src.web import publish_preflight as pf

    names = [a.strip() for a in (accounts or "").split(",") if a.strip()][:60]

    def run() -> dict:
        map_path = get_ad_account_map_path()
        amap = load_ad_account_map(map_path) if os.path.isfile(map_path) else {}
        result = pf.summary(pf.preflight(names, amap=amap))
        pf.record(result)
        return result

    return JSONResponse(await asyncio.to_thread(run))


@app.get("/api/meta-rules/capabilities")
async def api_meta_rules_capabilities() -> JSONResponse:
    """Допустимые цели, цели оптимизации, billing events, CTA и destination (ACL-1807).

    Только значения со статусом `ga` из `config/meta_rules/`; сети нет. Решение
    «пускать ли кампанию» остаётся за `check_job` на сервере. Реестр не читается —
    503, билдер тогда ничего не ограничивает.
    """
    from src.campaign_bridge import meta_rules

    try:
        return JSONResponse(await asyncio.to_thread(meta_rules.capabilities))
    except Exception as e:  # реестр не прочитался/не соответствует схеме — билдер ничего не режет
        return JSONResponse({"error": str(e)}, status_code=503)


@app.post("/api/publish-preflight/decision")
async def api_publish_preflight_decision(request: Request) -> JSONResponse:
    """Что владелец решил по списку «Not ready»: в телеметрию, для разбора потом."""
    from src.web import publish_preflight as pf

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}

    def names(key: str) -> list[str]:
        raw = body.get(key)
        return [str(x)[:80] for x in raw][:60] if isinstance(raw, list) else []

    queued, skipped, anyway = names("queued"), names("skipped"), names("run_anyway")
    pf.record({"accounts": [], "ready": queued, "not_ready": skipped + anyway},
              step="publish_preflight_decision",
              extra={"queued": len(queued), "skipped": skipped, "run_anyway": anyway})
    return JSONResponse({"ok": True})


@app.post("/publish-jobs/kept/release")
async def publish_jobs_release_kept(request: Request) -> JSONResponse:
    """«Release» у оставленного окна в очереди: снять с него место (ACL-1603).

    Не зависит от панели задания. Окно не закрывается — только перестаёт
    считаться в потолке профилей. Объявлен до `/publish-jobs/{job_id}/…`.
    """
    form = await read_form(request)
    from src.web.publish_jobs import release_kept

    got = release_kept(str(form.get("account") or ""))
    return JSONResponse(got, status_code=200 if got.get("ok") else 404)


@app.post("/publish-jobs/{job_id}/cancel")
async def publish_jobs_cancel(job_id: str) -> JSONResponse:
    """Stop a running publish/RPA job; Multilogin window stays open."""
    result = cancel_job(job_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404 if result.get("error") == "Job not found" else 400)
    return JSONResponse(result)


@app.post("/publish-jobs/{job_id}/close-profile")
async def publish_jobs_close_profile(job_id: str) -> JSONResponse:
    """«Close profile» on a finished job's panel (ACL-1444).

    Graceful MLX API stop of that account's profile (never kill); refused
    with 409 while a run or a read is using the profile. Runs off the event
    loop: the stop waits for Multilogin.
    """
    from starlette.concurrency import run_in_threadpool

    from src.web.publish_jobs import close_profile

    result = await run_in_threadpool(close_profile, job_id)
    if result.get("ok"):
        return JSONResponse(result)
    status = {"not_found": 404, "busy": 409}.get(str(result.get("code")), 502)
    return JSONResponse(result, status_code=status)


@app.get("/publish-jobs/{job_id}")
async def publish_jobs_status(job_id: str) -> JSONResponse:
    """Poll publish job progress. Returns status + results so far."""
    job = get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return JSONResponse(job)


@app.get("/publish-jobs/{job_id}/progress")
async def publish_jobs_progress(job_id: str) -> JSONResponse:
    """Где каждая кампания задания: итог, последний шаг робота, тишина (ACL-1315).

    Читает хвост телеметрии — ящик билдера ставит по этому ответу точку у
    имени кампании. 404, как и у статуса: после перезапуска дешборда задания
    нет, и страница перестаёт показывать его идущим.
    """
    from src.web.publish_progress import job_progress

    job = get_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    return JSONResponse(job_progress(job))


@app.post("/build-upload")
async def build_upload(request: Request) -> Response:
    """Build Upload FB xlsx from campaign builder slots for one account."""
    form = await read_form(request, BUILDER_FORM)
    country, account_label, slots = _parse_build_upload_form(form)
    if _several_adsets(slots):
        # ACL-1768: таблица Upload FB знает один набор на кампанию - не отбрасывать лишние молча.
        return await _in_report_pool(
            _render_report, request,
            upload_errors={(form.get("country") or "").strip() or country:
                           {account_label: [MULTI_ADSET_RPA_ERROR.split(" (")[0] + ": Upload FB cannot hold them."]}},
        )
    custom = [s for s in slots if s.get("adset_name") or s.get("pixel_id")]
    if custom:
        # ACL-1768: имя набора и пиксель набора таблица Upload FB не несёт - не отбрасывать молча.
        return await _in_report_pool(
            _render_report, request,
            upload_errors={(form.get("country") or "").strip() or country:
                           {account_label: ["Upload FB cannot hold a custom ad set name or pixel; "
                                            "use Publish with the API backend."]}},
        )
    if any(s.get("destination") == "instant_form" for s in slots):
        # ACL-1775: таблица Upload FB формы не умеет - не собирать сайтовую молча.
        return await _in_report_pool(
            _render_report, request,
            upload_errors={(form.get("country") or "").strip() or country:
                           {account_label: ["Instant Forms cannot be exported to Upload FB; use Publish with the API backend."]}},
        )

    if any((s.get("page_id") or "").strip() for s in slots):
        # ACL-1880: Upload FB собирается без выбора Page - выбранную явно не терять молча.
        return await _in_report_pool(
            _render_report, request,
            upload_errors={(form.get("country") or "").strip() or country:
                           {account_label: ["A Facebook Page chosen for a campaign cannot be exported to Upload FB; "
                                            "use Publish, or set the campaign's Page to Automatic."]}},
        )

    # The parser returns the country CODE ("MX"), which the RPA needs — without
    # it the audience Language step was silently skipped. But the template looks
    # errors up by the country NAME it renders the section under ("Mexico"), so
    # keying them by the code meant no error was EVER displayed: a build that
    # failed validation came back as the plain report page with nothing to say
    # why. Key by what the reader's section is keyed by.
    err_key = (form.get("country") or "").strip() or country

    if not country or not account_label:
        return await _in_report_pool(
            _render_report, request,
            upload_errors={"": {"": ["Missing country or account."]}},
        )
    if not slots:
        return await _in_report_pool(
            _render_report, request,
            upload_errors={err_key: {account_label: ["Add at least one campaign slot."]}},
        )

    errors, xlsx_bytes, filename = assemble_from_form_slots(
        country=country,
        account_label=account_label,
        slots=slots,
    )
    if errors:
        return await _in_report_pool(
            _render_report, request,
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
                         "expires_at": expires_at, "email": email,
                         # ACL-1441: в каком пространстве выдан токен слота.
                         "workspace": (os.environ.get(f"MULTILOGIN_WORKSPACE_{instance}") or "").strip()})


@app.get("/multilogin-token-expiry")
async def multilogin_token_expiry() -> JSONResponse:
    """ACL-2185: когда истекают сохранённые токены Multilogin (оба слота).

    Только офлайн-разбор `exp` у сохранённого токена — ни одного вызова
    Multilogin. Токен в ответ не попадает: слот, состояние, время истечения.
    Страница рисует по этому плашку за час до срока и после него.
    """
    from src.fb_api import mlx_session

    slots = [mlx_session.token_expiry(inst) for inst in ("A", "B")]
    return JSONResponse({"ok": True, "warn_seconds": int(mlx_session.EXPIRY_WARN_S),
                         "slots": [s for s in slots if s["has_token"]]},
                        headers={"Cache-Control": "no-store"})


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

    # A 401 here means the saved automation token is dead; anything else is
    # Multilogin refusing, and it says why. Both used to arrive as "0 profiles".
    profiles, error = _mlx_search_all(_req, H, search_text)
    if error:
        if error.startswith("HTTP 401"):
            return JSONResponse(
                {"ok": False, "error": "The token has expired — sign in again through Connect"}
            )
        # Multilogin отвечает и HTML-страницей ошибки. Вывалить её в подпись
        # под полем — значит показать человеку разметку вместо причины.
        clean = _re.sub(r"<[^>]+>", " ", error)
        clean = _re.sub(r"\s+", " ", clean).strip()
        if len(clean) > 160:
            clean = clean[:160].rstrip() + "…"
        return JSONResponse({"ok": False, "error": f"Multilogin refused the profile list: {clean}"})

    # ACL-1202: the account key (the same rule the overview uses for its
    # suggestion) rides along, so a picker can recognise "this is the one"
    # in a search answer without a second copy of the rule in the page.
    from src.fb_ads_rpa.mlx_profiles import profile_name_key

    for _p in profiles:
        if isinstance(_p, dict):
            _p.setdefault("key", profile_name_key(_p.get("name") or ""))
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
    """Save a manually pasted token to machine-local configuration.

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
    try:
        update_machine_env({env_key: token})
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"Failed to save machine settings: {exc}"},
            status_code=500,
        )

    return JSONResponse({"ok": True, "instance": instance, "env_key": env_key})


@app.post("/multilogin-signin")
async def multilogin_signin(request: Request) -> JSONResponse:
    """Sign in to Multilogin, return folders + profiles list.

    Body JSON: {email, password, save_token?: bool, instance?, workspace_id?}
    Credentials are used for a single sign-in and never persisted.
    ACL-1441: when the login has several workspaces with automation and none
    is chosen or remembered, answers {choose_workspace: true, workspaces} and
    the page re-sends with workspace_id.
    If save_token is true, updates the token in machine-local settings.
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

    import requests as _req

    from src.web.multilogin_auth import MLX_API, sign_in

    # Вход живёт одним местом на дешборд и мастер установки (ACL-1303):
    # мастер работает до того, как дешборд существует, а две копии одного
    # входа однажды разъедутся — починят одну и забудут вторую.
    #
    # ACL-1441: токен выдаётся в выбранном рабочем пространстве. Слот помнит
    # своё (имя и id в машинном .env) — повторное подключение не спрашивает.
    # Пространств с автоматизацией несколько и ни одно не названо — ответ
    # `choose_workspace`, ничего не сохраняется, страница присылает выбор.
    ws_key, ws_id_key = f"MULTILOGIN_WORKSPACE_{instance}", f"MULTILOGIN_WORKSPACE_ID_{instance}"
    workspace_id = str(data.get("workspace_id") or "").strip()
    remembered = (os.environ.get(ws_id_key) or os.environ.get(ws_key) or "").strip()
    signed = sign_in(email, password, workspace_id=workspace_id,
                     remembered_workspace=remembered, requests_module=_req)
    password = ""
    if not signed.ok:
        return JSONResponse({"ok": False, "error": signed.error})
    workspaces = [{"workspace_id": w["workspace_id"], "name": w["name"], "role": w["role"],
                   "automation": w.get("automation")} for w in signed.workspaces]
    if signed.needs_choice:
        return JSONResponse({
            "ok": True, "instance": instance, "choose_workspace": True,
            "token_saved": False, "token_error": signed.token_error,
            "workspaces": workspaces,
        })
    bearer = signed.bearer
    auto_token = signed.automation_token
    token_error = signed.token_error
    H = {"Accept": "application/json", "Content-Type": "application/json"}

    working_token = auto_token or bearer

    # Step 3 — save token outside the checkout. Never claim success when the
    # token works for this request but could not be persisted for next launch.
    env_key = f"MULTILOGIN_AUTOMATION_TOKEN_{instance}"
    if save_token and auto_token:
        values = {env_key: auto_token}
        if signed.workspace_id:
            values.update({ws_key: signed.workspace_name, ws_id_key: signed.workspace_id})
        try:
            update_machine_env(values)
        except Exception as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"Signed in, but failed to save machine settings: {exc}",
                },
                status_code=500,
            )

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

    # Step 5 — list profiles.
    #
    # One request, paged. The three fallbacks that used to live here were built
    # around a mystery — "sub-accounts may need a search term", "some MLX
    # versions" — and none of them was the answer: every attempt asked for 200
    # profiles, and Multilogin refuses anything over 100 with a flat
    # "limit invalid". So all three failed identically, for the same reason, and
    # the panel reported "No profiles found" for workspaces that were full.
    profiles, profiles_error = _mlx_search_all(_req, auth_h2, "")
    profile_debug: list[str] = []
    if profiles_error:
        profile_debug.append(f"search -> {profiles_error}")

    return JSONResponse({
        "ok": True,
        "instance": instance,
        "token_saved": save_token and bool(auto_token),
        # Why there is no token, in the account's own words. Without this the
        # page could only say "not saved", which reads as a detail rather than
        # as the reason the next Publish will fail.
        "token_error": token_error if (save_token and not auto_token) else "",
        "workspace": signed.workspace_name,
        "workspace_role": signed.workspace_role,
        # С выбранным не вышло — список остаётся, чтобы выбрать другое.
        "workspaces": workspaces if not auto_token else [],
        "folders": folders,
        "profiles": profiles,
        "debug": profile_debug,
    })
