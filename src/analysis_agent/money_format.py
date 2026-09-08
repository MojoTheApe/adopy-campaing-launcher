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


def fmt_currency(value: Any, currency: str = "") -> str:
    """Live account totals keep cents and the source currency; no conversion."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    code = str(currency or "").upper()
    label = code if len(code) == 3 and code.isascii() and code.isalpha() else "currency unknown"
    return f"{label} {number:,.2f}"
