"""USD display for morning reports (ceil, no cents)."""
from __future__ import annotations

import math
from typing import Any


def fmt_usd(value: Any) -> str:
    """Format money as ``$1,234`` — round up, no decimal places."""
    if value is None:
        return "—"
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in ("—", "-", "nan", "none"):
            return "—"
        try:
            num = float(s.replace("$", "").replace(",", ""))
        except ValueError:
            return s
    else:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return "—"
    if math.isnan(num):
        return "—"
    return f"${math.ceil(num):,}"


#: Знаки валют, которые Meta пишет рядом с суммой. Пишутся слитно с числом,
#: как она их и пишет: «$44.17», а не «$ 44.17».
_CURRENCY_SIGNS = ("$", "€", "£", "₪")


def fmt_currency(value: Any, currency: str = "") -> str:
    """Live account totals keep cents and the source currency; no conversion.

    Показывается то, что написала Meta: код — кодом («KWD 21.61»), знак —
    знаком («$44.17»). Прежде всё, кроме трёхбуквенного кода, превращалось в
    «currency unknown», и после решения владельца 15.09 передавать знак как
    есть на экране получалось «currency unknown 44.17» — при том что знак был
    известен и написан.

    Пустая валюта остаётся «currency unknown»: Meta не написала ничего, и
    голое число без пометки читалось бы как доллары по привычке.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    written = str(currency or "").strip()
    if written in _CURRENCY_SIGNS:
        return f"{written}{number:,.2f}"
    code = written.upper()
    label = code if len(code) == 3 and code.isascii() and code.isalpha() else "currency unknown"
    return f"{label} {number:,.2f}"
