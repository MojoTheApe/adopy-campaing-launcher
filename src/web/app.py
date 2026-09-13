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
import ipaddress
import json
import math
import os
import re as _re
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode, urlsplit

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
from src.analysis_agent.money_format import fmt_usd, fmt_currency
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
    get_draft_status_path,
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
    """Load the machine's last explicit ready/blocked campaign verdict."""
    path = Path(get_draft_status_path())
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
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


def _build_goals_view(report: dict) -> dict:
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
    for goal in goals_store.for_cm(cm):
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
        days = pd.to_datetime(df["Date"], errors="coerce").dt.date.dropna()
        return sorted({d.isoformat() for d in days})
    except Exception:
        return []


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

    key = (
        report.get("source_excel"),
        report.get("_cache_revision"),
        (report.get("my_cm") or get_selected_cm() or ""),
        date_from,
        date_to,
    )
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
        if len(_PERIOD_CACHE) >= _PERIOD_CACHE_MAX:
            _PERIOD_CACHE.pop(next(iter(_PERIOD_CACHE)))
        _PERIOD_CACHE[key] = scoped
    return scoped


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

    Numbers come from the workbook, per account per country — leads, deposits,
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
            if not excel.get("has_data"):
                # Тот же разрешатель, что и у раскрытия объявлений: два способа
                # отвечать на один вопрос однажды разойдутся, и разойдутся молча.
                # Resolve identity against the whole workbook. A date filter
                # must not hide a collision or an older exact match.
                resolved = workbook_alias_for(df, country_name, label)
                if resolved and resolved.casefold() != label.casefold():
                    alias = resolved
                    excel = _numbers(country_name, alias) or excel
            rows.append({
                "account": label,
                # чем именно подставлены числа — чтобы совпадение по хвосту
                # никогда не выглядело как имя самого аккаунта
                "workbook_alias": alias,
                # страны, под которыми числа этого аккаунта лежат на самом деле
                "elsewhere": {} if excel.get("has_data") else _elsewhere(country_name, label),
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
                # Unknown units cannot be added to one another.
                unknown_sums.append({"currency": "", "spend": amount, "account": r["account"]})

        countries.append({
            # `country` is what the reader sees, `key` is what the store knows.
            # They differ whenever the account map holds a code ("KW") and the
            # report holds a name ("Kuwait") — and a button that sends the label
            # instead of the key returns HTTP 200 having changed nothing.
            "country": country_name,
            "key": stored,
            "rows": rows,
            "spend": sum(r["spend"] or 0 for r in rows),
            "leads": sum(r["leads"] or 0 for r in rows),
            "deposits": sum(r["deposits"] or 0 for r in rows),
            "live_read": live_read,
            "live_sums": [
                {"currency": c, "spend": s} for c, s in sorted(live_sums.items())
            ] + unknown_sums,
        })

    available = [
        {"account": label, "assets": account_links.values_for(label, "asset")}
        for label in account_links.available_accounts(all_known)
    ]
    return {
        "countries": countries,
        "available": available,
        "all_accounts": all_known,
        "dates": dates,
        "live": live,
        "date_from": date_from,
        "date_to": date_to,
        "span": span,
    }



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
            }
        )
    # Сначала живые — по расходу, потом остальные по алфавиту. Страна,
    # открытая вчера и ещё без доллара, стоит выше двадцати мёртвых:
    # именно её сегодня и открывают.
    rows.sort(
        key=lambda r: (not r["active"], -r["spend"], (r["c"].get("country") or "").lower())
    )
    return rows


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
        }
    return result


def _load_sheet_targets() -> list[dict]:
    """Local library plus refreshed shared entries, including narrowing."""
    from src.analysis_agent import targeting_registry as tr
    tr.refresh()
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
templates.env.filters["fmt_usd"] = fmt_usd
templates.env.filters["fmt_currency"] = fmt_currency
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


def _secure_headers(response: Response) -> Response:
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

    return _secure_headers(await call_next(request))


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


def _mlx_search_all(requests_mod, headers: dict, search_text: str) -> tuple[list[dict], str]:
    """Every profile the token can see, paged. Returns (profiles, error)."""
    found: list[dict] = []
    offset = 0
    while offset < _MLX_MAX_PROFILES:
        r = requests_mod.post(
            f"{MLX_API_BASE}/profile/search",
            headers=headers,
            json={"limit": _MLX_PAGE, "offset": offset, "search_text": search_text},
            timeout=20,
        )
        if r.status_code != 200:
            detail = ""
            try:
                detail = ((r.json().get("status") or {}).get("message") or "").strip()
            except Exception:
                detail = (r.text or "")[:200]
            return found, f"HTTP {r.status_code}" + (f": {detail}" if detail else "")
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
    """Fast identity check for the Windows launcher; never loads workbooks."""
    return JSONResponse({"app": "ACL", "ok": True})


@app.get("/acl/update/check")
async def acl_update_check(request: Request) -> JSONResponse:
    """Is a newer promoted release waiting? Answers the header's Update button.

    Reached on every page load, so it must never raise and never block for long:
    a machine with no network still has a dashboard to run.
    """
    from src.web import acl_update

    skip_fetch = (request.query_params.get("fetch") or "1").strip().lower() in {"0", "false", "no"}
    try:
        return JSONResponse(acl_update.check(fetch=not skip_fetch))
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

    if not acl_update.is_supervised():
        return JSONResponse(
            {
                "ok": False,
                "error": "ACL was not started by its launcher, so nothing would "
                         "bring it back. Start it from the desktop shortcut and "
                         "try again.",
            },
            status_code=409,
        )

    busy = active_job_count()
    if busy:
        return JSONResponse(
            {
                "ok": False,
                "error": f"{busy} campaign job(s) still running. Updating restarts "
                         "ACL, which would abandon them half-built.",
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
    date_from: str = "",
    date_to: str = "",
    span: str = "",
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

    # Период выбирается один раз на всю страницу: карта аккаунтов, обзор
    # стран и рекомендации должны говорить об одних и тех же днях. Раньше он
    # менял только карту, и две секции рядом отвечали на разные вопросы,
    # выглядя одинаково достоверно.
    date_from, date_to = resolve_period(_df, date_from.strip(), date_to.strip(), span.strip())
    try:
        report = report_for_period(report, _df, date_from, date_to)
    except PeriodReportError as exc:
        return templates.TemplateResponse(
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

    return templates.TemplateResponse(
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
            "account_map": _account_map_view,
            "sidebar_countries": _sidebar_countries(report, _account_map_view),
            "period": {"date_from": date_from, "date_to": date_to, "span": span},
            "goals": _build_goals_view(report),
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


def _alias_by_tail(label: str, known: dict) -> str:
    """Имя из книги для подключённого аккаунта — или пусто.

    Совпадение принимается только единственное. Два имени с одним хвостом —
    это не «выберем первое»: подставленное наугад число выглядит точно так же,
    как правильное, и проверить его человеку будет нечем.
    """
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
    return hits[0] if len(hits) == 1 else ""


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
        from src.analysis_agent.analyze_fb_stats import alias_matches

        sub = df[df["Country Final"] == country] if "Country Final" in df.columns else df
        if sub.empty:
            return label
        if bool(alias_matches(sub["Account Alias"], label).any()):
            return label
        names = sorted(
            {str(a).strip() for a in sub["Account Alias"].dropna() if str(a).strip()}
        )
        return _alias_by_tail(label, names) or label
    except Exception:  # pragma: no cover - перевод имени не должен ронять страницу
        return label


def _workbook_has_account(df, country: str, label: str) -> bool:
    if df is None or "Account Alias" not in df.columns:
        return False
    from src.analysis_agent.analyze_fb_stats import alias_matches
    sub = df[df["Country Final"] == country] if "Country Final" in df.columns else df
    return bool(alias_matches(sub["Account Alias"], workbook_alias_for(df, country, label)).any())


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
        # Пусто — это ответ, а не пропуск: язык сняли намеренно, и правило по
        # стране подставится при обогащении только если поле не трогали.
        language  = (form.get(f"camp_{idx}_language") or "").strip()
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
                    "tracking_link_provided": "tracking_link" in form,
                    "language": language,
                    "language_provided": f"camp_{idx}_language" in form,
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
async def index(
    request: Request, date_from: str = "", date_to: str = "", span: str = ""
) -> HTMLResponse:
    return _render_report(request, date_from=date_from, date_to=date_to, span=span)


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
    return JSONResponse({"files": files})


def _ad_copy_rows() -> list[dict]:
    """Read the same local library used by RPA; only new machines import once."""
    from src.analysis_agent import ad_text_registry as ar

    return [{"ad_title": t.name, "headline": t.headline, "body": t.body,
             "description": t.description, "asset": t.asset,
             "language": t.language} for t in ar.builder_texts()]


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
            # An account can belong to two countries at once — five of fourteen
            # do. Showing only one hides the other, and the hidden one is a
            # market someone might build a campaign for.
            "countries": _resolved.get(normalize_account_alias(label), {}).get("countries", []),
            "country_ambiguous": _resolved.get(normalize_account_alias(label), {}).get("ambiguous", False),
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
            # An account can belong to two countries at once — five of fourteen
            # do. Showing only one hides the other, and the hidden one is a
            # market someone might build a campaign for.
            "countries": _resolved.get(normalize_account_alias(label), {}).get("countries", []),
            "country_ambiguous": _resolved.get(normalize_account_alias(label), {}).get("ambiguous", False),
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
def api_targeting_profiles(force: bool = False) -> JSONResponse:
    """Run Sheets and disk I/O in FastAPI's worker pool."""
    from src.analysis_agent import targeting_registry as tr
    try:
        sheet = tr.refresh(force=force)
        targets = tr.all_targets()
        return JSONResponse({"ok": True, "can_add": True, "sheet": sheet,
                             "duplicates": tr.duplicates(targets),
                             "profiles": [t.to_dict() for t in targets]})
    except tr.TargetingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


def _edit_targeting_profile(data: dict) -> JSONResponse:
    from src.analysis_agent import targeting_registry as tr
    if not isinstance(data, dict) or any(not isinstance(data.get(k, ""), str)
            for k in ("action", "name", "interests", "narrow", "was", "confirmation")):
        return JSONResponse({"ok": False, "error": "Expected an object with string fields"}, status_code=400)
    action = (data.get("action") or "add").strip().lower()
    name, interests, narrow = (data.get(k, "") for k in ("name", "interests", "narrow"))
    try:
        if action == "delete":
            tr.remove(name)
            return JSONResponse({"ok": True, "removed": name, "scope": "local"})
        if action == "export":
            if data.get("confirmation") != "replace_shared_library":
                raise tr.TargetingError("Подтвердите замену всей общей таблицы локальной библиотекой.")
            written, backup = tr.push_all()
            return JSONResponse({"ok": True, "exported": written, "backup": backup})
        if action == "update":
            target = tr.update(data.get("was") or name, name, interests, narrow)
        elif action == "add":
            target = tr.add(name, interests, narrow)
            tr.share(target)
        elif action == "share":
            target = tr.lookup(name)
            if target is None:
                raise tr.TargetingError("Таргет не найден.")
            tr.share(target)
        else:
            raise tr.TargetingError(f"Неизвестное действие: {action}")
    except tr.TargetingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "shared": target.shared,
                         "shared_error": target.shared_error, "profile": target.to_dict()})


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
    """Библиотека рекламных текстов. Чтение вкладки — в рабочем пуле FastAPI."""
    from src.analysis_agent import ad_text_registry as ar
    try:
        sheet = ar.refresh(force=True) if force else ar.ensure_loaded()
        texts = ar.all_texts()
        return JSONResponse({"ok": True, "sheet": sheet,
                             "duplicates": ar.duplicates(texts),
                             "texts": [t.to_dict() for t in texts]})
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
            return JSONResponse({"ok": True, "removed": name})
        if action == "update":
            text = ar.update(data.get("was") or name, name, headline, body, description, asset, language)
        elif action == "add":
            text = ar.add(name, headline, body, description, asset, language)
        else:
            raise ar.AdTextError(f"Неизвестное действие: {action}")
    except ar.AdTextError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "text": text.to_dict()})


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

    label = str((body or {}).get("label") or "").strip()
    if not label:
        return JSONResponse({"ok": False, "error": "label is required"}, status_code=400)
    hide = bool((body or {}).get("hidden", True))

    keys = _hidden.set_hidden(label, hide)
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


@app.post("/account-spend/read")
async def account_spend_read(request: Request) -> JSONResponse:
    """Прочитать спенд с аккаунта. Только по нажатию человека.

    Никакого расписания: обход живых аккаунтов по таймеру уже однажды
    предшествовал чекпоинту на телефон, и это не повторяется.
    """
    from src.analysis_agent import account_spend

    form = await request.form()
    account = (form.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
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
    return JSONResponse({"ok": True, "reading": reading.to_dict()})


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

    form = await request.form()
    account = (form.get("account") or "").strip()
    level = (form.get("level") or "").strip()
    object_id = (form.get("object_id") or "").strip()
    if not account or not object_id:
        return JSONResponse({"ok": False, "error": "account and object_id are required"}, status_code=400)
    if level not in ("campaign", "ad"):
        return JSONResponse({"ok": False, "error": "level must be campaign or ad"}, status_code=400)
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
        )
    except Exception:
        return JSONResponse({"ok": False, "error": "Could not reach the account. Check the launcher window."}, status_code=503)
    return JSONResponse({"ok": True, "result": result.to_dict()})


@app.post("/account-spend/open-profile")
async def account_spend_open_profile(request: Request) -> JSONResponse:
    """Поднять профиль аккаунта автоматизацией и оставить окно открытым.

    Чтение умеет только цепляться к уже открытому окну — перезапуск ради
    одного числа разлогинивает аккаунт. Поднять профиль так, чтобы к нему
    можно было прицепиться, до сих пор умел только путь публикации.
    """
    from src.analysis_agent import account_spend

    form = await request.form()
    account = (form.get("account") or "").strip()
    if not account:
        return JSONResponse({"ok": False, "error": "account is required"}, status_code=400)
    # to_thread, а не starlette: свежая установка не должна зависеть от
    # пакета, которого нет в requirements.txt
    ok, message = await asyncio.to_thread(account_spend.open_for_automation, account)
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)


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

    form = await request.form()
    profile_id = (form.get("profile_id") or "").strip()
    if not profile_id:
        return JSONResponse({"ok": False, "error": "profile_id is required"}, status_code=400)
    ok, message = await asyncio.to_thread(
        account_spend.open_profile_for_automation,
        profile_id,
        (form.get("folder_id") or "").strip(),
        (form.get("instance") or "A").strip(),
        (form.get("name") or "").strip(),
    )
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)


@app.post("/account-probe/ad-account")
async def account_probe_ad_account(request: Request) -> JSONResponse:
    """Спросить у открытого профиля, какой у него рекламный кабинет.

    Подключая аккаунт, дешборд до сих пор просил человека вставить номер, если
    профиль на этой машине не открывали. Номер знает сам кабинет — Ads Manager
    называет его в своём адресе.
    """
    from src.analysis_agent import account_spend

    form = await request.form()
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
    from src.analysis_agent import account_spend

    try:
        date_from, date_to = _spend_period(date_from, date_to, span)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if account.strip():
        return JSONResponse({
            "ok": True,
            "reading": account_spend.get_reading(account.strip(), date_from, date_to).to_dict(),
        })
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


@app.get("/account-map")
async def account_map_fragment(
    request: Request, date_from: str = "", date_to: str = "", span: str = ""
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

    _enrich_country_overview(report)
    return templates.TemplateResponse(
        "_account_map.html",
        {
            "request": request,
            "amap": _build_account_map_view(
                report, df, date_from.strip(), date_to.strip(), span.strip()
            ),
            "mlx": mlx_instance_labels(),
        },
    )


@app.get("/goals")
async def goals_read(cm: str = "") -> JSONResponse:
    from src.web import goals as goals_store

    who = (cm or "").strip() or (os.environ.get("MY_CM_NAME") or "").strip()
    return JSONResponse({
        "ok": True,
        "cm": who,
        "goals": goals_store.for_cm(who),
        "team_lead": goals_store.is_team_lead(),
    })


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
    try:
        if action == "delete":
            country = (data.get("country") or "").strip()
            if not country:
                return JSONResponse({"ok": False, "error": "Which country?"}, status_code=400)
            return JSONResponse({"ok": True, "goals": goals_store.delete(who, country)})
        goal = data.get("goal") or {}
        return JSONResponse({"ok": True, "goals": goals_store.upsert(who, goal)})
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
            spending = _accounts_spending_today(
                [account] if action == "unlink" else account_links.accounts_for(kind, value)
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

    map_path = Path(get_ad_account_map_path())
    if not map_path.is_file():
        return JSONResponse({"ok": False, "error": "Account map not found"}, status_code=404)

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
                {"ok": False, "error": f"{shown} is not in the map of this machine"},
                status_code=404,
            )

        write_csv_atomic(map_path, fieldnames, kept)
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
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"plan": plan, "account": account_label, "country": country})


@app.post("/publish-jobs")
async def publish_jobs_start(request: Request) -> JSONResponse:
    """Launch a background publish job. Returns job_id for polling."""
    form = await request.form()
    country, account_label, slots = _parse_build_upload_form(form)
    via_ml = (form.get("via_multilogin") or "1").strip() not in ("0", "false", "no")
    activate = (form.get("activate") or "0").strip() in ("1", "true", "yes")
    dry_run = (form.get("dry_run") or "0").strip() in ("1", "true", "yes")
    user_confirmed = (form.get("user_confirmed") or "0").strip() in ("1", "true", "yes")
    if activate:
        return JSONResponse(
            {"error": "Live activation is disabled: ACL creates draft-only campaigns."},
            status_code=403,
        )
    if not user_confirmed:
        return JSONResponse(
            {"error": "Publish blocked: confirm in the dashboard UI first (user_confirmed=0)."},
            status_code=403,
        )
    if not country or not account_label or not slots:
        return JSONResponse({"error": "Missing country, account or slots."}, status_code=400)
    try:
        job_id = start_job(
            account_label,
            country,
            slots,
            via_multilogin=via_ml,
            activate=False,
            dry_run=dry_run,
            # The server owns this safety decision; a client cannot opt out.
            hand_off=True,
        )
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    if use_rpa_publish():
        from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

        total = len(
            slots_to_campaign_drafts(
                slots,
                account_label=account_label,
                country=country,
                activate=False,
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

    Body JSON: {email, password, save_token?: bool}
    Credentials are used for a single API call and never persisted.
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
        return JSONResponse({"ok": False, "error": f"Sign-in failed [{r.status_code}]: {msg}"})

    bearer = ((r.json().get("data") or {}).get("token") or "").strip()
    if not bearer:
        return JSONResponse({"ok": False, "error": "Sign-in returned no token"})

    auth_h = {**H, "Authorization": f"Bearer {bearer}"}

    # Step 2 — get automation token (1 month).
    #
    # Signing in and being allowed to MINT an automation token are two different
    # permissions: only the workspace owner has the second one. A team member's
    # correct password therefore gets this far and no further. That distinction
    # has to survive to the page — the RPA cannot start a single profile without
    # this token, and "connected" with no token is the same as not connected.
    auto_token = ""
    token_error = ""
    try:
        r2 = _req.get(f"{MLX_API}/workspace/automation_token?expiration_period=1mo", headers=auth_h, timeout=20)
        if r2.status_code == 200:
            d2 = r2.json().get("data") or {}
            auto_token = d2.get("token") or d2.get("automation_token") or ""
            if not auto_token:
                token_error = "Multilogin returned no automation token for this account."
        else:
            detail = ""
            try:
                detail = ((r2.json().get("status") or {}).get("message") or "").strip()
            except Exception:
                detail = (r2.text or "")[:200]
            token_error = f"Multilogin refused to issue an automation token [{r2.status_code}]"
            if detail:
                token_error += f": {detail}"
    except Exception as exc:
        token_error = f"Could not reach Multilogin for the automation token: {exc}"

    working_token = auto_token or bearer

    # Step 3 — save token outside the checkout. Never claim success when the
    # token works for this request but could not be persisted for next launch.
    env_key = f"MULTILOGIN_AUTOMATION_TOKEN_{instance}"
    if save_token and auto_token:
        try:
            update_machine_env({env_key: auto_token})
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
        "folders": folders,
        "profiles": profiles,
        "debug": profile_debug,
    })
