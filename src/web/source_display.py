"""Request-local source units for statistics; account money and budgets stay separate."""
from __future__ import annotations

import math

from jinja2 import pass_context

from src.analysis_agent import bi_compare, metric_columns
from src.analysis_agent.money_format import fmt_usd
from src.web import period_compare


def saved_comparison(frame, countries, readings, date_from, date_to, span=""):
    """Use the supplied frame/readings, without fetching a source or profile."""
    import pandas as pd
    from src.analysis_agent.account_names import account_key
    from src.analysis_agent.analyze_fb_stats import alias_matches

    selected = frame if frame is not None else pd.DataFrame()
    if date_from or date_to:
        selected = (bi_compare.period_rows(selected, date_from, date_to)
                    if "Date" in selected else selected.iloc[:0])
    accounts = {}
    for country in countries:
        for row in country.get("rows", []):
            label = row["account"]
            entry = accounts.setdefault(label.casefold(), {"account": label, "aliases": set()})
            entry["aliases"].add(row.get("workbook_alias") or label)

    def amount(value, unit):
        number = metric_columns._num(value)
        return "—" if number is None else f"{unit or 'currency unknown'} {number:,.2f}"

    rows = []
    for entry in accounts.values():
        label, aliases = entry["account"], entry["aliases"]
        source_rows = selected.iloc[:0]
        ambiguous = len({account_key(alias) for alias in aliases}) != 1
        if not ambiguous and "Account Alias" in selected and "$ Spend" in selected:
            source_rows = selected[alias_matches(selected["Account Alias"], next(iter(aliases)))]
        source = bi_compare.provenance(frame=source_rows)
        stored = [v for key, v in readings.items() if key.strip().casefold() == label.casefold()]
        reading = stored[0] if len(stored) == 1 else {}
        # A latest-read fallback belongs to its own dates, never this comparison.
        if (span == "all" or not date_from or reading.get("date_from") != date_from
                or reading.get("date_to") != (date_to or date_from)):
            reading = {}
        totals = {} if source_rows.empty else {label: round(float(source_rows["$ Spend"].sum()), 2)}
        row = bi_compare.compare_spend(totals, {label: reading}, source_context={label: source})[0]
        reason = {
            "comparable": "Difference: account minus source",
            "missing_source": "No source figure for these dates",
            "missing_account": "No saved account figure for these dates",
            "unknown_currency": "Currency is unknown; no difference calculated",
            "currency_mismatch": "Currencies differ; no difference calculated",
            "unknown_day_boundary": "Day boundary is unknown; no difference calculated",
            "day_boundary_mismatch": "Day boundaries differ; no difference calculated",
        }[row["comparison_state"]]
        if ambiguous:
            reason = "Source alias is ambiguous; no difference calculated"
        row.update(
            source_alias=" / ".join(sorted(set(source_rows.get("Account Alias", [])))),
            source_amount=amount(row["source_spend"], row["source_currency"]),
            account_amount=amount(row["account_spend"], row["account_currency"]),
            gap_amount=amount(row["gap"], row["source_currency"]), reason=reason,
            account_at=reading.get("finished_at", "") if row["account_spend"] is not None else "",
        )
        rows.append(row)
    return {"rows": rows, "date_from": date_from, "date_to": date_to or date_from,
            "all_dates": span == "all"}


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
