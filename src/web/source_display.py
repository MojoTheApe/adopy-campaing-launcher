"""Request-local source units for statistics; account money and budgets stay separate."""
from __future__ import annotations

import math

from jinja2 import pass_context

from src.analysis_agent import bi_compare, metric_columns
from src.analysis_agent.money_format import fmt_usd
from src.web import period_compare


def source_info(context):
    report = context.get("report") or {}
    for owner in (context.get("amap") or {}, context.get("account_map") or {}, report):
        if "source_info" in owner and owner["source_info"] is not None:
            return owner["source_info"]
    return None


def money(value, info):
    # Old fragments without any source context keep their established USD contract.
    # An explicitly unknown source is different: it must never gain a dollar sign.
    if info is None:
        return fmt_usd(value)
    number = metric_columns._num(value)
    if number is None:
        return metric_columns.DASH
    unit = bi_compare.currency_code(info.get("currency_code"))
    return ("$" if unit == "USD" else (unit or "currency unknown") + " ") + f"{math.ceil(number):,}"


@pass_context
def source_money(context, value, info=None):
    return money(value, source_info(context) if info is None else info)


@pass_context
def metric_values(context, spend, leads, ftds):
    values = metric_columns.metric_values(spend, leads, ftds)
    r = metric_columns.ratios(spend, leads, ftds)
    info = source_info(context)
    for index, value in ((0, metric_columns._num(spend) or 0), (3, r["cpl"]), (4, r["cpa"])):
        values[index] = money(value, info)
    return values


def _comparison_info(context):
    current = source_info(context)
    compare = context.get("compare")
    previous = (getattr(compare, "_report", {}) or {}).get("source_info", current)
    # Never compare unlike or unknown financial units/days. Count deltas are independent.
    compatible = current is None or bool(current and previous and all(
        current.get(key) and current.get(key) == previous.get(key)
        for key in ("currency_code", "timezone", "day_boundary")))
    return previous, compatible


@pass_context
def metric_compare(context, cur, prev):
    cells = period_compare.metric_compare(cur, prev)
    info, compatible = _comparison_info(context)
    if prev is not None:
        values = metric_columns.ratios(prev.get("spend"), prev.get("leads"), prev.get("deposits"))
        for index, value in ((0, prev.get("spend")), (3, values["cpl"]), (4, values["cpa"])):
            cells[index]["prev"] = money(value, info)
            if not compatible:
                cells[index].update(delta="—", dir="", tone="")
    return cells


@pass_context
def value_compare(context, cur, prev, kind="money"):
    cell = period_compare.value_compare(cur, prev, kind)
    if kind == "money":
        info, compatible = _comparison_info(context)
        cell["prev"] = money(prev, info)
        if not compatible:
            cell.update(delta="—", dir="", tone="")
    return cell


@pass_context
def comparison_cell(context, cell):
    # Workbench/variant models retain the shared formatter's prebuilt money cells.
    # Re-label only those cells; counts, percentages and raw values stay untouched.
    previous = str(cell.get("prev", ""))
    if not previous.startswith("$"):
        return cell
    info, compatible = _comparison_info(context)
    out = dict(cell, prev=money(previous[1:].replace(",", ""), info))
    if not compatible:
        out.update(delta="—", dir="", tone="")
    return out


@pass_context
def comparison_info(context):
    info, compatible = _comparison_info(context)
    return {"currency_code": (info or {}).get("currency_code", "USD" if info is None else ""),
            "comparable": compatible}
