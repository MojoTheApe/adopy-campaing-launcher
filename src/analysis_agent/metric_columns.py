"""The one metric set every attribution table shows (ACL-1652).

Spend, Leads, FTDs, CPL, CPA, CR, in this order, on every level of the
dashboard. The account table uses the same BI numbers; its campaign-list status is not a metric.

Everything that has to agree between tables lives here: the header names, the
three ratio formulas, how a zero divisor is shown and how money and CR are
written. Templates take the headers and the cells from `_metrics.html`, which
calls these functions; the JS twin `acl6Metrics` in report.html is pinned to
this module by tests/test_metric_columns.py.
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from src.analysis_agent.money_format import fmt_usd

#: Заголовки: имена, порядок и регистр — решение владельца 2026-10-06.
METRIC_HEADERS = ("Spend", "Leads", "FTDs", "CPL", "CPA", "CR")
#: ACL-2063: Meta spend removed; all account metrics come from the workbook.
HOME_ACCOUNT_HEADERS = METRIC_HEADERS

DASH = "—"


def _num(value: Any) -> Optional[float]:
    """Число или None. NaN и мусор — это «нет числа», а не ноль."""
    try:
        f = float(value)
    except Exception:  # None, мусор, а в шаблоне ещё и Undefined: числа нет
        return None
    return f if math.isfinite(f) else None


def ratios(spend: Any, leads: Any, ftds: Any) -> dict[str, Optional[float]]:
    """CPL, CPA и CR (в процентах). None — когда делитель равен 0.

    CR = FTDs / Leads: при лидах и нуле FTD это 0.0%, а не прочерк — делитель
    у него не ноль. Spend, которого нет, считается нулём: CPA при FTD и нулевом
    спенде — честные $0.
    """
    s = _num(spend) or 0.0
    l = _num(leads) or 0.0
    f = _num(ftds) or 0.0
    return {
        "cpl": s / l if l > 0 else None,
        "cpa": s / f if f > 0 else None,
        "cr": f / l * 100.0 if l > 0 else None,
    }


def fmt_money(value: Any) -> str:
    """Доллары целыми, с округлением вверх: «$8» при $7.10. None — прочерк."""
    n = _num(value)
    return DASH if n is None else fmt_usd(n)


def fmt_count(value: Any) -> str:
    n = _num(value)
    return "0" if n is None else str(int(n))


def fmt_cr(value: Any) -> str:
    """Процент с одним знаком. None — прочерк, 0.0 — «0.0%»."""
    n = _num(value)
    if n is None:
        return DASH
    # Округление «половина вверх» — как у JS `toFixed(1)` в двойнике `acl6Metrics`.
    # Python-овское `f"{n:.1f}"` округляет половину к чётному (6.25 -> 6.2), и
    # серверная таблица с JS-таблицей показывали разный CR для одного лендинга.
    return f"{Decimal(n).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def metric_values(spend: Any, leads: Any, ftds: Any) -> list[str]:
    """Шесть готовых строк в порядке METRIC_HEADERS."""
    r = ratios(spend, leads, ftds)
    return [
        fmt_money(_num(spend) or 0.0),
        fmt_count(leads),
        fmt_count(ftds),
        fmt_money(r["cpl"]),
        fmt_money(r["cpa"]),
        fmt_cr(r["cr"]),
    ]
