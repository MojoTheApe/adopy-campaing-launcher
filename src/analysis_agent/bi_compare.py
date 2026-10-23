"""ACL-103: что за цифры даёт BI и как их сверять с цифрами из кабинета.

Три вещи, которых не было в первой версии (подзадачи 103d, 103e, 103f):

- ``provenance()`` — валюта и граница дня, с которыми показаны цифры BI.
  Запрос к ``BI_Export``.``UF.Export`` не несёт ни валюту, ни часовой пояс
  (колонок для них в ``bi_stats.COLUMN_MAP`` нет), поэтому это ДОПУЩЕНИЕ с
  документированными значениями по умолчанию: USD и календарный день BI.
  Пока живое чтение BI его не подтвердило, экран и заметка называют его
  допущением. Подтвердили — ``BI_ASSUMPTIONS_CONFIRMED=1`` (а значения
  меняются через ``BI_CURRENCY`` / ``BI_DAY_BOUNDARY``), без правки кода.
- ``compare_spend()`` — цифры источника (BI/книга) и цифры аккаунта
  (показания из кабинета, ``account_spend``) рядом и по отдельности: разница
  между ними и есть сигнал, а не повод усреднять.
- маппинг полей — docs/ACL-103-bi-field-mapping.md.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Optional

import pandas as pd

DEFAULT_CURRENCY = "USD"
DEFAULT_DAY_BOUNDARY = "BI calendar day (the DATE column as delivered by BI; time zone not stated by BI)"

#: Расхождение меньше этого — округление, не сигнал.
TOLERANCE = 0.01


def _confirmed() -> bool:
    return (os.environ.get("BI_ASSUMPTIONS_CONFIRMED") or "").strip().lower() in ("1", "true", "yes")


def provenance(source: str = "bi") -> Dict[str, Any]:
    """Валюта и граница дня для цифр источника; ``assumed`` — пока не подтверждено."""
    if source == "excel":
        return {"source": "Stats workbook", "currency": DEFAULT_CURRENCY,
                "day_boundary": "workbook Date column", "assumed": True,
                "note": "Stats workbook: currency is USD by convention, day boundary is the workbook Date."}
    currency = (os.environ.get("BI_CURRENCY") or DEFAULT_CURRENCY).strip().upper()
    boundary = (os.environ.get("BI_DAY_BOUNDARY") or DEFAULT_DAY_BOUNDARY).strip()
    assumed = not _confirmed()
    note = f"BI figures are in {currency}; day boundary: {boundary}."
    if assumed:
        note += (" This is an assumption, not read from BI: the BI table has no currency or time zone"
                 " column. Confirm it with BI before comparing money to an account in another currency.")
    return {"source": "BI", "currency": currency, "day_boundary": boundary, "assumed": assumed, "note": note}


def source_spend_by_account(df: pd.DataFrame, date_from: str = "", date_to: str = "") -> Dict[str, float]:
    """Спенд источника по алиасу аккаунта за период (даты включительно)."""
    if df is None or df.empty:
        return {}
    frame = df
    if date_from:
        dates = pd.to_datetime(frame["Date"], errors="coerce")
        frame = frame[(dates >= pd.Timestamp(date_from)) & (dates <= pd.Timestamp(date_to or date_from))]
    grouped = frame.groupby("Account Alias")["$ Spend"].sum()
    return {str(k): round(float(v), 2) for k, v in grouped.items()}


def compare_spend(
    source_by_account: Dict[str, float],
    account_readings: Dict[str, Dict[str, Any]],
    *,
    source_currency: Optional[str] = None,
) -> list[Dict[str, Any]]:
    """Строка на аккаунт: цифра источника, цифра аккаунта, разница — отдельно.

    ``account_readings`` — ``SpendReading.to_dict()`` по аккаунту. Показание без
    числа (идёт/не удалось) не сравнивается: ``account_spend`` пуст, ``gap`` пуст.
    Разные валюты не вычитаются — ``currency_mismatch``.
    """
    source_currency = (source_currency or provenance()["currency"]).upper()
    rows = []
    for account in sorted(set(source_by_account) | set(account_readings)):
        reading = account_readings.get(account) or {}
        has_reading = reading.get("state") == "ok" and reading.get("spend") is not None
        account_spend = round(float(reading["spend"]), 2) if has_reading else None
        account_currency = (reading.get("currency") or "").strip().upper() if has_reading else ""
        source_spend = source_by_account.get(account)
        mismatch = bool(has_reading and account_currency and account_currency != source_currency)
        gap = None
        if source_spend is not None and account_spend is not None and not mismatch:
            gap = round(account_spend - source_spend, 2)
        rows.append({
            "account": account,
            "source_spend": source_spend,
            "source_currency": source_currency,
            "account_spend": account_spend,
            "account_currency": account_currency,
            "currency_mismatch": mismatch,
            "gap": gap,
            "differs": gap is not None and abs(gap) > TOLERANCE,
        })
    return rows
