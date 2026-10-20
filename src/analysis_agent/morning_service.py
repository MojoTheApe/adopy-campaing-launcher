"""Shared orchestration for the morning report.

Both the CLI (`run_morning.py`) and the web app (`src/web/app.py`) call
`load_morning_report` so env handling, GSheet fetching, error reporting,
and the call into `build_morning_report` live in one place.

Returns the report dict, the enriched DataFrame (needed by the web app's
drilldown endpoint), and a list of human-readable warnings (e.g. GSheet
unavailable). No exceptions raised for "soft" failures like a missing
GSheet credential — the caller decides how to surface warnings.
"""
from __future__ import annotations

import concurrent.futures
import csv
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from config.paths import get_ad_account_map_path, get_excel_path
from src.analysis_agent.analyze_fb_stats import (
    compute_metrics,
    enrich_structure,
)
from src.creative_variant import apply_backfill as apply_variant_backfill
from src.analysis_agent.gsheet_accounts import fetch_account_rows
from src.analysis_agent.morning_report import build_morning_report
from src.analysis_agent.stats_workbook import read_stats_workbook
from src.analysis_agent import bi_stats
from src.analysis_agent.news_context import NewsFetcher, build_news_context


_RE_TWO_UPPER = re.compile(r"^[A-Z]{2}$")


def _alias_from_label(label: str) -> str:
    """Strip trailing short suffix (country code, etc.) to get account alias."""
    parts = label.strip().rsplit(" ", 1)
    if len(parts) == 2 and len(parts[1]) <= 4 and parts[1].isalpha():
        return parts[0].strip()
    return label.strip()


def _country_from_label(label: str) -> str:
    """Return first 2-uppercase-letter token from account name (e.g. 'MX' from 'FacebookElf161 MX')."""
    for token in label.split():
        if _RE_TWO_UPPER.match(token):
            return token
    return ""


def _fallback_accounts_from_csv(cm_name: str) -> List[Dict[str, Any]]:
    """Load all accounts from ad_account_map.csv as fallback when GSheet returns no rows.

    Returns minimal account-row dicts compatible with gsheet_accounts format,
    all marked is_ready=True so they appear in the campaign builder.

    Goes through the canonical resolver rather than building the path here.
    The map moved out of the checkout to the machine-local ACL folder on
    2026-08-25, and
    this line still pointed at the old location — so during a Sheets outage the
    buyer's accounts would have vanished from the campaign builder even though
    a perfectly good map was sitting on the machine (audit finding F-07).
    """
    csv_path = Path(get_ad_account_map_path())
    rows: List[Dict[str, Any]] = []
    if not csv_path.exists():
        return rows
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                label = (row.get("account_name") or "").strip()
                if not label:
                    continue
                # Explicit 'country' column (set via dashboard or CSV) wins over
                # guessing from the account name — a name suffix like "SA" is just
                # a legacy label, not necessarily where the account is actually
                # running today (that can change day to day).
                country_code = (row.get("country") or "").strip().upper() or _country_from_label(label)
                rows.append({
                    "cm": cm_name,
                    "country_code": country_code,
                    "account_label": label,
                    "alias": _alias_from_label(label),
                    "cost": "",
                    "leads": "",
                    "ftds": "",
                    "stock": "",
                    "to_do": "",
                    "to_do_extra": "",
                    "is_ready": True,
                    "account_status": None,
                })
    except Exception as e:  # noqa: BLE001
        print(f"[morning_service] Failed to load fallback accounts from CSV: {e}", file=sys.stderr)
    return rows


def _resolve_excel_path(excel_path: Optional[str]) -> str:
    """env var / arg / config default precedence — same as the CLI uses."""
    return (excel_path or "").strip() or get_excel_path()


def _resolve_my_cm(my_cm: Optional[str]) -> str:
    return (my_cm or "").strip() or os.environ.get("MY_CM_NAME", "Evgeni Sedin").strip()


def _load_enriched_df(
    excel_path: str, *, progress: Callable[[str], None] | None = None
) -> pd.DataFrame:
    """Read Excel + run the same enrichment pipeline.

    The web layer's drilldown filters need the enriched DataFrame
    (Country Final, Offer, Device, Interest, Age, Creative, TextSegment
    columns), not the raw Excel rows.
    """
    if progress is not None:
        progress("reading")
    df_raw, source, note = _read_stats_rows(excel_path)
    if progress is not None:
        progress("processing")
    df = compute_metrics(df_raw)
    # ACL-1593: вариант для объявлений, построенных до имени с вариантом.
    df = apply_variant_backfill(enrich_structure(df))
    df.attrs["stats_source"] = source
    df.attrs["stats_note"] = note
    return df


def _read_stats_rows(excel_path: str) -> Tuple[pd.DataFrame, str, Optional[str]]:
    """(строки в схеме книги, источник, предупреждение).

    ACL-103: BI первым, если настроен; при его ошибке — книга, с
    предупреждением на странице. Без книги ошибка BI поднимается как есть.
    """
    if bi_stats.bi_enabled():
        try:
            return bi_stats.read_bi_stats(), "bi", None
        except Exception as e:  # noqa: BLE001 — фолбэк на книгу ниже
            if not os.path.isfile(excel_path):
                raise RuntimeError(f"BI unavailable and no Stats workbook: {type(e).__name__}: {e}") from e
            note = f"BI unavailable ({type(e).__name__}: {e}) — showing the Stats workbook instead."
            print(f"[morning_service] {note}", file=sys.stderr)
            return read_stats_workbook(excel_path), "excel", note
    return read_stats_workbook(excel_path), "excel", None


def load_morning_report(
    *,
    excel_path: Optional[str] = None,
    my_cm: Optional[str] = None,
    trend_days: int = 7,
    country_filter: Optional[str] = None,
    skip_gsheet: bool = False,
    with_news: bool = False,
    news_fetcher: Optional[NewsFetcher] = None,
    enriched_df: Optional[pd.DataFrame] = None,
    account_rows_cached: Optional[List[Dict[str, Any]]] = None,
    progress: Callable[[str], None] | None = None,
) -> Tuple[Dict[str, Any], pd.DataFrame, List[str]]:
    """Build the full morning report from current Excel + GSheet state.

    Excel is parsed + enriched ONCE. The enriched DataFrame is returned
    alongside the report dict so the caller (web app drilldown) doesn't
    re-parse on every drill request.

    Pass ``enriched_df`` to skip re-reading Excel (Refresh reuses the DF when
    the file mtime is unchanged). GSheet fetch runs in parallel with Excel
    load when both are needed.

    ``progress`` observes actual reading, processing and preparation boundaries.
    The Excel callback runs on the loader's worker thread when Sheets is fetched
    concurrently; callers own synchronization and job identity.

    Returns:
        (report_dict, enriched_df, warnings)

    Raises:
        FileNotFoundError: if the Excel file is missing.
        ValueError: if the Excel is missing required columns.
    """
    warnings: List[str] = []

    resolved_excel = _resolve_excel_path(excel_path)
    if enriched_df is None and not bi_stats.bi_enabled() and not os.path.isfile(resolved_excel):
        raise FileNotFoundError(
            f"Excel file not found: {resolved_excel}. "
            f"Set FB_STATS_EXCEL or pass excel_path."
        )

    resolved_cm = _resolve_my_cm(my_cm)

    account_rows: List[Dict[str, Any]] = []
    fallback_mode = False
    gsheet_error: Exception | None = None

    def _fetch_gsheet() -> List[Dict[str, Any]]:
        return fetch_account_rows(my_cm=resolved_cm)

    # GSheet + Excel are independent — run in parallel when Excel must load.
    if skip_gsheet:
        warnings.append("GSheet skipped (skip_gsheet=True)")
        if enriched_df is None:
            enriched_df = _load_enriched_df(resolved_excel, progress=progress)
    elif account_rows_cached is not None:
        # Строки GSheet не зависят от дат. Пересобирая тот же отчёт за другой
        # период, ходить за ними второй раз незачем — а с пропуском GSheet
        # целиком погасли бы секции 6-9, что выглядело бы как «данных нет».
        account_rows = list(account_rows_cached)
    elif enriched_df is not None:
        try:
            account_rows = _fetch_gsheet()
        except Exception as e:  # noqa: BLE001 - graceful degradation
            gsheet_error = e
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_gsheet = pool.submit(_fetch_gsheet)
            fut_df = pool.submit(_load_enriched_df, resolved_excel, progress=progress)
            try:
                account_rows = fut_gsheet.result()
            except Exception as e:  # noqa: BLE001
                gsheet_error = e
            enriched_df = fut_df.result()

    if gsheet_error is not None:
        warnings.append(
            f"GSheet unavailable: {type(gsheet_error).__name__}: {gsheet_error}. "
            f"Sections 6-9 will be empty or limited."
        )

    # If GSheet returned no accounts for this CM, fetch ALL accounts from the same
    # tab (no CM filter) so the campaign builder shows every available account.
    if not account_rows and not skip_gsheet and gsheet_error is None:
        try:
            all_rows = fetch_account_rows(my_cm=None)
        except Exception as e:  # noqa: BLE001
            all_rows = []
            warnings.append(f"GSheet fallback fetch failed: {type(e).__name__}: {e}")
        if all_rows:
            account_rows = all_rows
            fallback_mode = True
            warnings.append(
                f"GSheet has no accounts for {resolved_cm!r} today — "
                f"showing all {len(all_rows)} account(s) from the sheet (free to open campaigns)."
            )

    # Supplement with ad_account_map.csv: accounts that are Multilogin-mapped for
    # RPA but aren't listed in today's GSheet tab (e.g. not scheduled for any CM
    # today) still need to be pickable in Campaign Builder — otherwise a mapped
    # account is invisible until someone adds it to the sheet for that day.
    existing_labels = {(r.get("account_label") or "").strip().lower() for r in account_rows}
    csv_rows = _fallback_accounts_from_csv(resolved_cm)
    new_from_csv = [r for r in csv_rows if r["account_label"].strip().lower() not in existing_labels]
    if new_from_csv:
        account_rows = account_rows + new_from_csv
        fallback_mode = True
        # Предупреждения здесь нет намеренно. Аккаунты из карты, которых нет в
        # сегодняшнем листе, добавляются каждый день — это обычная работа, а не
        # событие. Полоса «Added 13 account(s)…» висела над дешбордом всегда,
        # убрать её было нельзя, и она приучала не читать это место вовсе. Тогда
        # настоящее предупреждение, встав туда же, тоже осталось бы непрочитанным.
        # Сами аккаунты видны там, где они и нужны, — на карте аккаунтов.

    assert enriched_df is not None

    if progress is not None:
        progress("preparing")
    report = build_morning_report(
        df=enriched_df,
        account_rows=account_rows,
        my_cm=resolved_cm,
        trend_days=trend_days,
        country_filter=country_filter,
    )
    report["is_fallback_mode"] = fallback_mode

    if with_news:
        sections = report.get("countries", [])
        max_workers = max(1, int(os.environ.get("MORNING_NEWS_WORKERS", "6")))

        def _load_country_news(section: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any] | None, str | None]:
            country = section.get("country", "")
            try:
                assets = [row.get("offer", "") for row in section.get("asset_summary", [])]
                news_context = build_news_context(
                    country,
                    assets,
                    top_combos=section.get("top_combos", []),
                    fetcher=news_fetcher,
                )
                return section, news_context, None
            except Exception as e:  # noqa: BLE001 - report should degrade gracefully
                return section, None, (
                    f"News context unavailable for {country}: {type(e).__name__}: {e}"
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_load_country_news, section) for section in sections]
            for future in concurrent.futures.as_completed(futures):
                section, news_context, warning = future.result()
                if news_context is not None:
                    section["news_context"] = news_context
                if warning:
                    warnings.append(warning)
    # Provide the source Excel path on the report for the renderer header.
    report["source_excel"] = resolved_excel
    report["stats_source"] = enriched_df.attrs.get("stats_source", "excel")
    if report["stats_source"] == "bi":
        report["source_excel"] = "BI (MySQL)"
    note = enriched_df.attrs.get("stats_note")
    if note:
        warnings.append(note)
    # для пересборки за период: см. account_rows_cached выше
    report["_account_rows"] = account_rows
    return report, enriched_df, warnings
