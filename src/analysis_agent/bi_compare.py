"""ACL-103: происхождение, единицы и сопоставимость чисел источника/аккаунта."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from src.analysis_agent.account_names import account_key

SOURCE_COLUMN = "_stats_source"
METADATA_COLUMNS = {
    "currency_code": "_stats_currency_code",
    "timezone": "_stats_timezone",
    "day_boundary": "_stats_day_boundary",
}
BI_METADATA_COLUMNS = {
    "STATS_CURRENCY": "currency_code",
    "DATE_TIMEZONE": "timezone",
    "DAY_BOUNDARY": "day_boundary",
}
SOURCE_NAMES = {"bi": "BI", "workbook": "Stats workbook", "unknown": "Saved statistics"}
TOLERANCE = 0.01


def text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    value = str(value).strip()
    return "" if value.casefold() in {"unknown", "n/a", "none", "null", "nan", "not stated", "unspecified"} else value


def currency_code(value: Any) -> str:
    value = text(value).upper()
    # Знак $ сам по себе не отличает USD от других долларов.
    return value if re.fullmatch(r"[A-Z]{3}", value) else ""


def timezone_name(value: Any) -> str:
    value = text(value)
    if not value:
        return ""
    try:
        zone = ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    return "UTC" if value in {"UTC", "Etc/UTC", "GMT", "Etc/GMT"} else zone.key


def configured_metadata(source: str) -> Dict[str, str]:
    prefix = "BI" if source == "bi" else "STATS"
    return {
        "currency_code": currency_code(os.environ.get(prefix + "_CURRENCY")),
        "timezone": timezone_name(os.environ.get(prefix + "_TIMEZONE")),
        "day_boundary": text(os.environ.get(prefix + "_DAY_BOUNDARY")),
    }


def annotate_source(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Метка исходных строк; конвенция/confirmation flag не создаёт метаданные."""
    source = "workbook" if source == "excel" else source
    out = df.copy()
    out[SOURCE_COLUMN] = source
    configured = configured_metadata(source)
    for key, column in METADATA_COLUMNS.items():
        raw = next((name for name, field in BI_METADATA_COLUMNS.items()
                    if source == "bi" and field == key and name in out), None)
        clean = currency_code if key == "currency_code" else timezone_name if key == "timezone" else text
        if raw:
            out[column] = out[raw].map(clean)
        elif column not in out:
            out[column] = configured[key]
    return out


def period_rows(df: pd.DataFrame, date_from: str = "", date_to: str = "") -> pd.DataFrame:
    if df is None or df.empty or not (date_from or date_to):
        return df
    dates = pd.to_datetime(df["Date"], errors="coerce")
    selected = dates.notna()
    if date_from:
        selected &= dates >= pd.Timestamp(date_from)
    if date_to:
        selected &= dates <= pd.Timestamp(date_to)
    return df[selected]


#: Валюта BI / книги статистики, если источник её не объявил (ACL-2204).
DEFAULT_SOURCE_CURRENCY = "USD"


def _source_currency(frame: Any) -> tuple[str, bool]:
    """(валюта, подставлена ли по умолчанию). Строка без валюты считается USD;
    разные валюты по строкам дают пустую валюту (смесь), а не USD."""
    column = METADATA_COLUMNS["currency_code"]
    if frame is None or frame.empty or column not in frame:
        return DEFAULT_SOURCE_CURRENCY, True
    declared = [currency_code(v) for v in frame[column]]
    values = {v or DEFAULT_SOURCE_CURRENCY for v in declared}
    if len(values) != 1:
        return "", False
    return values.pop(), not any(declared)


def provenance(source: str = "bi", frame: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """Описание именно выбранных строк; архив — хранилище, а не их поставщик."""
    if frame is None:
        source = "workbook" if source == "excel" else source
        frame = annotate_source(pd.DataFrame({"_row": [1]}), source)
    if frame.empty:
        sources = []
    elif SOURCE_COLUMN in frame:
        sources = sorted({text(v) or "unknown" for v in frame[SOURCE_COLUMN]})
    else:
        sources = ["workbook" if source == "excel" else "unknown"]

    def uniform(key):
        column = METADATA_COLUMNS[key]
        clean = currency_code if key == "currency_code" else timezone_name if key == "timezone" else text
        if frame.empty or column not in frame:
            return ""
        values = {clean(v) for v in frame[column]}
        return values.pop() if len(values) == 1 and "" not in values else ""

    _, zone, boundary = (uniform(k) for k in METADATA_COLUMNS)
    # ACL-2204, решение владельца 26.10.2026: BI и книга статистики без
    # объявленной валюты — доллары. Явное объявление (BI_CURRENCY /
    # STATS_CURRENCY) по-прежнему главнее; смесь разных валют по строкам
    # остаётся «unknown» и в доллары не превращается.
    currency, currency_defaulted = _source_currency(frame)
    name = " + ".join(SOURCE_NAMES.get(s, SOURCE_NAMES["unknown"]) for s in sources) or "No source rows"
    incomplete = not (zone and boundary)
    shown_currency = f"{currency} (default)" if currency_defaulted else (currency or "unknown")
    note = (f"{name}; currency: {shown_currency}; time zone: {zone or 'unknown'}; "
            f"day boundary: {boundary or 'unknown'}.")
    if incomplete:
        note += " Missing source metadata is unknown; day boundaries are not assumed to match an account."
    if not currency:
        note += " Source rows declare different currencies; money is not assumed to match an account."
    return {"source": name, "sources": sources, "currency": currency or "unknown",
            "currency_code": currency, "currency_defaulted": currency_defaulted, "timezone": zone, "day_boundary": boundary,
            "assumed": incomplete, "note": note,
            "description": "the BI data" if sources == ["bi"] else
                           "the Stats workbook" if sources == ["workbook"] else
                           "the selected source data"}


def source_spend_by_account(df: pd.DataFrame, date_from: str = "", date_to: str = "") -> Dict[str, float]:
    if df is None or df.empty:
        return {}
    grouped = period_rows(df, date_from, date_to).groupby("Account Alias")["$ Spend"].sum()
    return {str(k): round(float(v), 2) for k, v in grouped.items()}


def source_context_by_account(df: pd.DataFrame, date_from: str = "", date_to: str = "") -> Dict[str, Dict[str, Any]]:
    if df is None or df.empty:
        return {}
    return {str(account): provenance(frame=rows)
            for account, rows in period_rows(df, date_from, date_to).groupby("Account Alias")}


def compare_spend(
    source_by_account: Dict[str, float],
    account_readings: Dict[str, Dict[str, Any]],
    *,
    source_currency: Optional[str] = None,
    source_context: Optional[Dict[str, Dict[str, Any]]] = None,
) -> list[Dict[str, Any]]:
    """Оба числа отдельно; неизвестные единицы/границы не становятся разницей."""
    # Only the established naming rules may join different labels. Multiple
    # records under one identity stay visible; choosing one would invent a gap.
    sources, readings = {}, {}
    for account in source_by_account:
        sources.setdefault(account_key(account), []).append(account)
    for account in account_readings:
        readings.setdefault(account_key(account), []).append(account)
    pairs = []
    for key in sorted(set(sources) | set(readings)):
        source_names, reading_names = sources.get(key, []), readings.get(key, [])
        ambiguous = len(source_names) > 1 or len(reading_names) > 1
        if not ambiguous:
            pairs.append((next(iter(source_names), ""), next(iter(reading_names), ""), False))
        else:
            pairs.extend((name if name in source_by_account else "",
                          name if name in account_readings else "", True)
                         for name in sorted(set(source_names) | set(reading_names)))

    rows = []
    for source_account, reading_account, ambiguous in sorted(pairs, key=lambda p: p[0] or p[1]):
        account = source_account or reading_account
        reading = account_readings.get(reading_account) or {}
        info = (source_context or {}).get(source_account) or provenance()
        unit = currency_code(source_currency) if source_currency is not None else info["currency_code"]
        if not unit and source_currency is not None:
            unit = DEFAULT_SOURCE_CURRENCY  # undeclared source currency is USD (ACL-2204)
        has_reading = reading.get("state") == "ok" and reading.get("spend") is not None
        account_spend = round(float(reading["spend"]), 2) if has_reading else None
        account_currency = (currency_code(reading.get("currency_code")) or
                            currency_code(reading.get("currency"))) if has_reading else ""
        source_spend = source_by_account.get(source_account)
        mismatch = bool(has_reading and unit and account_currency and unit != account_currency)
        source_zone, source_boundary = info.get("timezone", ""), info.get("day_boundary", "")
        account_zone = timezone_name(reading.get("timezone"))
        account_boundary = text(reading.get("day_boundary"))
        state = "comparable"
        if ambiguous:
            state = "ambiguous_account_identity"
        elif source_spend is None:
            state = "missing_source"
        elif not has_reading:
            state = "missing_account"
        elif not unit or not account_currency:
            state = "unknown_currency"
        elif mismatch:
            state = "currency_mismatch"
        elif not all((source_zone, source_boundary, account_zone, account_boundary)):
            state = "unknown_day_boundary"
        elif (source_zone, source_boundary) != (account_zone, account_boundary):
            state = "day_boundary_mismatch"
        gap = round(account_spend - source_spend, 2) if state == "comparable" else None
        rows.append({
            "account": account, "reading_account": reading_account,
            "source_spend": source_spend, "source_currency": unit,
            "source": info, "account_spend": account_spend, "account_currency": account_currency,
            "account_timezone": account_zone, "account_day_boundary": account_boundary,
            "currency_mismatch": mismatch, "comparison_state": state,
            "gap": gap, "differs": gap is not None and abs(gap) > TOLERANCE,
        })
    return rows
