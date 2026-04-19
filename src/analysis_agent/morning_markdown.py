"""Render a MorningReport dict → Markdown string.

All 11 sections per country, structured for easy morning reading in Cursor.
"""
from __future__ import annotations

from typing import Any, Dict, List

# ── Markdown helpers ───────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def _table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "_No data._"
    escaped_headers = [_esc(x) for x in headers]
    escaped_rows = [[_esc(c) for c in row] for row in rows]
    # Compute column widths for alignment
    col_widths = [len(h) for h in escaped_headers]
    for row in escaped_rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))
    def _pad(cells: List[str]) -> str:
        padded = [cells[i].ljust(col_widths[i]) if i < len(col_widths) else cells[i]
                  for i in range(len(cells))]
        return "| " + " | ".join(padded) + " |"
    h = _pad(escaped_headers)
    sep = "| " + " | ".join("-" * w for w in col_widths) + " |"
    body = [_pad(row) for row in escaped_rows]
    return "\n".join([h, sep, *body])


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_float(v: float, decimals: int = 2) -> str:
    return f"{v:.{decimals}f}"


# ── Section renderers ─────────────────────────────────────────────────────────

def _render_cm_comparison(rows: List[Dict[str, Any]], my_cm: str) -> str:
    lines = ["### 1. Evgeni vs команда", ""]
    if not rows:
        lines.append("_No CM data for this country._")
        return "\n".join(lines)

    table_rows = []
    for r in rows:
        marker = " ← YOU" if r.get("is_me") else ""
        cm = str(r["cm"])[:22] + marker
        leads = r["leads"]
        deposits = r["deposits"]
        spend = r["spend"]
        cpl_s = _fmt_money(spend / leads) if leads else "—"
        cpa_s = _fmt_money(r["cpa"]) if r["cpa"] else "—"
        cr_s = f"{deposits / leads * 100:.1f}%" if leads else "—"
        table_rows.append([
            cm,
            _fmt_money(spend),
            str(leads),
            str(deposits),
            cpl_s,
            cpa_s,
            cr_s,
            _fmt_float(r["roi"]),
        ])
    lines.append(_table(["CM", "Spend", "Leads", "Dep", "CPL", "CPA", "CR", "ROI"], table_rows))
    return "\n".join(lines)


def _render_asset_summary(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return ""
    table_rows = []
    for r in rows:
        leads = r["leads"]
        deposits = r["deposits"]
        cr_s = f"{deposits / leads * 100:.1f}%" if leads else "—"
        table_rows.append([
            _esc(r["offer"]),
            _fmt_money(r["spend"]),
            str(leads),
            str(deposits),
            _fmt_money(r["cpl"]) if r["cpl"] else "—",
            _fmt_money(r["cpa"]) if r["cpa"] else "—",
            cr_s,
            _fmt_float(r["roi"]),
        ])
    return _table(["Asset (Offer)", "Spend", "Leads", "Dep", "CPL", "CPA", "CR", "ROI"], table_rows)


def _render_trend(trend: Dict[str, Any]) -> str:
    lines = ["### 2. Тренд (последние дни)", ""]
    if not trend.get("has_date"):
        lines.append("_Date column not found in Excel — trend analysis unavailable._")
        return "\n".join(lines)

    lines.append(f"Period: **{trend.get('period_label', '')}**")
    lines.append("")

    up = trend.get("trending_up", [])
    down = trend.get("trending_down", [])
    stable = trend.get("stable", [])

    if up:
        lines.append("**Новые (все депозиты из последнего периода):**")
        for e in up[:5]:
            lines.append(
                f"- {e['device']} + {e['interest']} + {e['creative'][:30]}"
                f"  [{e['offer']}]  dep: {e['deposits_recent']}"
            )
        lines.append("")

    if stable:
        lines.append("**Стабильные (депозиты и раньше, и сейчас):**")
        for e in stable[:5]:
            lines.append(
                f"- {e['device']} + {e['interest']} + {e['creative'][:30]}"
                f"  [{e['offer']}]  total: {e['deposits_total']}, recent: {e['deposits_recent']}"
            )
        lines.append("")

    if down:
        lines.append("**Остановились (были депозиты, сейчас 0):**")
        for e in down[:5]:
            lines.append(
                f"- {e['device']} + {e['interest']} + {e['creative'][:30]}"
                f"  [{e['offer']}]  total: {e['deposits_total']}, recent: 0"
            )
        lines.append("")

    if not up and not down and not stable:
        lines.append("_No combo-level trend data available._")

    return "\n".join(lines)


def _render_top_combos(combos: List[Dict[str, Any]]) -> str:
    lines = ["### 3. Рабочие комбинации", ""]
    if not combos:
        lines.append("_No Strong/Interesting combos for this country._")
        return "\n".join(lines)

    table_rows = []
    for c in combos:
        quality = c.get("quality", "ok")
        flag = " ✓" if quality == "ok" else " ⚠"
        type_label = c["category"] + flag
        roi_s = _fmt_float(c["roi"]) if c["roi"] else "—"
        cpa_s = _fmt_money(c["cpa"]) if c["cpa"] else "—"
        cpl_s = _fmt_money(c.get("cpl", 0)) if c.get("cpl") else "—"
        combo_label = f"{c['device']} + {c['interest']} + {c['age']} + {c['creative'][:25]}"
        table_rows.append([
            type_label,
            c["offer"][:15],
            combo_label,
            str(c["deposits"]),
            cpl_s,
            cpa_s,
            roi_s,
        ])
    lines.append(_table(
        ["Type", "Offer", "Combo", "Dep", "CPL", "CPA", "ROI"],
        table_rows,
    ))
    return "\n".join(lines)


def _render_targeting(rows: List[Dict[str, Any]]) -> str:
    lines = ["### 4. Таргетинг", ""]
    if not rows:
        lines.append("_No targeting data._")
        return "\n".join(lines)

    table_rows = []
    for r in rows:
        cpa_s = _fmt_money(r["cpa"]) if r["cpa"] else "—"
        table_rows.append([
            r["interest"][:20],
            str(r["deposits"]),
            str(r["leads"]),
            _fmt_money(r["spend"]),
            cpa_s,
        ])
    lines.append(_table(["Interest", "Dep", "Leads", "Spend", "CPA"], table_rows))
    return "\n".join(lines)


def _render_creatives(rows: List[Dict[str, Any]]) -> str:
    lines = ["### 5. Креативы", ""]
    if not rows:
        lines.append("_No creative data._")
        return "\n".join(lines)

    table_rows = []
    for r in rows:
        cpa_s = _fmt_money(r["cpa"]) if r["cpa"] else "—"
        table_rows.append([
            r["ad_title"][:30],
            str(r["deposits"]),
            str(r["leads"]),
            _fmt_money(r["spend"]),
            cpa_s,
        ])
    lines.append(_table(["Ad Title", "Dep", "Leads", "Spend", "CPA"], table_rows))
    return "\n".join(lines)


def _render_ai_context(block: str) -> str:
    return "\n".join([
        "### 4. Контекст для AI (скопируй в чат)",
        "",
        "```",
        block,
        "```",
    ])


def _render_hypotheses(block: str) -> str:
    return "\n".join([
        "### 5. Гипотезы (скопируй в чат)",
        "",
        "```",
        block,
        "```",
    ])


def _render_account_statuses(account_rows: List[Dict[str, Any]], has_gsheet: bool = False) -> str:
    lines = ["### 6–7. Статусы аккаунтов", ""]
    if not account_rows:
        if has_gsheet:
            lines.append("_No accounts found for this country in the GSheet._")
        else:
            lines.append(
                "_GSheet account data not loaded. Set GSHEET_ACCOUNTS_ID in .env to enable._"
            )
        return "\n".join(lines)

    table_rows = []
    for r in account_rows:
        ready_s = "✅" if r["is_ready"] else "❌"
        to_do_s = r["to_do"] if r["to_do"] else "—"
        cost_s = r.get("cost", "") or "—"
        stock_s = r.get("stock", "") or "—"
        table_rows.append([
            r["account_label"],
            cost_s,
            stock_s,
            to_do_s,
            ready_s,
        ])
    lines.append(_table(["Account", "Cost", "Asset", "To Do", "✓"], table_rows))
    return "\n".join(lines)


def _render_close_recs(close_recs: Dict[str, List[Dict[str, Any]]]) -> str:
    lines = ["### 8. Что закрыть (оптимизация)", ""]
    any_data = any(v for v in close_recs.values())
    if not any_data:
        lines.append("_No ad sets to close — nothing exceeds benchmarks._")
        return "\n".join(lines)

    for acc_label, recs in close_recs.items():
        if not recs:
            continue
        lines.append(f"**{acc_label}:**")
        lines.append("")
        table_rows = []
        for r in recs:
            cpl_s = _fmt_money(r["cpl"]) if r.get("cpl") else "—"
            cpa_s = _fmt_money(r["cpa"]) if r.get("cpa") else "—"
            table_rows.append([
                r["adset_name"][:55],
                _fmt_money(r["spend"]),
                str(r["leads"]),
                str(r["deposits"]),
                cpl_s,
                cpa_s,
                r.get("reason", ""),
            ])
        lines.append(_table(
            ["Ad Set", "Spend", "Leads", "FTDs", "CPL", "CPA", "Reason"],
            table_rows,
        ))
        lines.append("")
    return "\n".join(lines)


def _render_open_recs(open_recs: Dict[str, List[Dict[str, Any]]]) -> str:
    lines = ["### 9. Что открыть (2 кампании на аккаунт)", ""]
    any_data = any(v for v in open_recs.values())
    if not any_data:
        lines.append("_No ready accounts or no combos to recommend._")
        return "\n".join(lines)

    for acc_label, recs in open_recs.items():
        if not recs:
            continue
        lines.append(f"**{acc_label}:**")
        lines.append("")
        for i, r in enumerate(recs, 1):
            quality = r.get("quality", "ok")
            flag = " ✓" if quality == "ok" else " ⚠"
            cat = r.get("category", "") + flag
            argument = r.get("argument", "")
            below = r.get("below_benchmark", False)
            warn_note = "  ⚠ below benchmark" if below else ""

            lines.append(
                f"  Campaign {i}: `{r['campaign_name']}`"
                f"   [{r['targeting']} — {argument}]{warn_note}"
            )
            for cr in r.get("creatives", []):
                roi_s = f", ROI {cr['roi']:.2f}" if cr.get("roi") else ""
                dep_s = f"{cr['deposits']} dep" if cr["deposits"] else ""
                note = f"  ({cr['note']})" if cr.get("note") else ""
                parts = [s for s in [dep_s, roi_s.lstrip(", ")] if s]
                stats = f"  ({', '.join(parts)})" if parts else ""
                lines.append(f"    - {cr['name']}{stats}{note}")
            lines.append("")
    return "\n".join(lines)


# ── Country section ────────────────────────────────────────────────────────────

def _render_country_section(section: Dict[str, Any], my_cm: str, has_gsheet: bool = False) -> str:
    country = section["country"]
    parts = [
        f"## Country: {country}",
        "",
        _render_cm_comparison(section.get("cm_comparison", []), my_cm),
        "",
        _render_asset_summary(section.get("asset_summary", [])),
        "",
        _render_trend(section.get("trend", {})),
        "",
        _render_top_combos(section.get("top_combos", [])),
        "",
        _render_ai_context(section.get("ai_context", "")),
        "",
        _render_hypotheses(section.get("hypotheses", "")),
        "",
        _render_account_statuses(section.get("account_rows", []), has_gsheet=has_gsheet),
        "",
        _render_close_recs(section.get("close_recs", {})),
        "",
        _render_open_recs(section.get("open_recs", {})),
        "",
        "---",
        "",
    ]
    return "\n".join(parts)


# ── Full report renderer ───────────────────────────────────────────────────────

def render_morning_report_md(report: Dict[str, Any]) -> str:
    """Render MorningReport dict → full Markdown string."""
    date_s = report.get("date", "")
    source = report.get("source_excel", "")
    my_cm = report.get("my_cm", "")
    trend_days = report.get("trend_days", 7)
    total_combos = report.get("total_combos", 0)
    has_gsheet = report.get("has_gsheet", False)
    countries = report.get("countries", [])

    gsheet_note = "✅ connected" if has_gsheet else "⚠️  not connected (set GSHEET_ACCOUNTS_ID in .env)"

    parts = [
        f"# Morning Report — {date_s}",
        "",
        f"- **CM:** {my_cm}",
        f"- **Source:** `{source}`",
        f"- **Trend window:** {trend_days} days",
        f"- **Qualifying combos:** {total_combos}",
        f"- **Google Sheet:** {gsheet_note}",
        "",
        "**How to use sections 6 & 7:**  Copy the code block into Cursor chat and ask your question.",
        "",
        "---",
        "",
    ]

    for section in countries:
        parts.append(_render_country_section(section, my_cm, has_gsheet=has_gsheet))

    if not countries:
        parts.append("_No country data found. Check Excel path and filters._")

    return "\n".join(parts).rstrip() + "\n"
