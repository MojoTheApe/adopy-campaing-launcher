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
    h = "| " + " | ".join(_esc(x) for x in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_esc(x) for x in row) + " |" for row in rows]
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
        cpa_s = _fmt_money(r["cpa"]) if r["cpa"] else "—"
        table_rows.append([
            cm,
            str(r["deposits"]),
            str(r["leads"]),
            _fmt_money(r["spend"]),
            _fmt_money(r["net"]),
            cpa_s,
            _fmt_float(r["roi"]),
        ])
    lines.append(_table(["CM", "Dep", "Leads", "Spend", "Net", "CPA", "ROI"], table_rows))
    return "\n".join(lines)


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
        roi_s = _fmt_float(c["roi"]) if c["roi"] else "—"
        cpa_s = _fmt_money(c["cpa"]) if c["cpa"] else "—"
        combo_label = f"{c['device']} + {c['interest']} + {c['age']} + {c['creative'][:20]}"
        table_rows.append([
            c["category"],
            c["offer"][:15],
            combo_label,
            str(c["deposits"]),
            cpa_s,
            roi_s,
            c["top_account"][:20] if c["top_account"] else "—",
        ])
    lines.append(_table(
        ["Type", "Offer", "Combo (device+interest+age+creative)", "Dep", "CPA", "ROI", "TopAccount"],
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
        "### 6. Контекст для AI (скопируй в чат)",
        "",
        "```",
        block,
        "```",
    ])


def _render_hypotheses(block: str) -> str:
    return "\n".join([
        "### 7. Гипотезы (скопируй в чат)",
        "",
        "```",
        block,
        "```",
    ])


def _render_account_statuses(account_rows: List[Dict[str, Any]]) -> str:
    lines = ["### 8–9. Статусы аккаунтов", ""]
    if not account_rows:
        lines.append(
            "_GSheet account data not loaded. Set GSHEET_ACCOUNTS_ID in .env to enable._"
        )
        return "\n".join(lines)

    table_rows = []
    for r in account_rows:
        ready_s = "✅ Ready" if r["is_ready"] else f"❌ {r['to_do'] or 'Disabled'}"
        to_do_s = r["to_do"] if r["to_do"] else "—"
        table_rows.append([r["account_label"], to_do_s, ready_s])
    lines.append(_table(["Account", "To Do", "Status"], table_rows))
    return "\n".join(lines)


def _render_close_recs(close_recs: Dict[str, List[Dict[str, Any]]]) -> str:
    lines = ["### 10. Что закрыть (оптимизация)", ""]
    any_data = any(v for v in close_recs.values())
    if not any_data:
        lines.append("_No ad sets with 0 FTD and notable spend — nothing to close._")
        return "\n".join(lines)

    for acc_label, recs in close_recs.items():
        if not recs:
            continue
        lines.append(f"**{acc_label}:**")
        for r in recs:
            lines.append(
                f"- {r['adset_name'][:60]}  "
                f"(Spend: {_fmt_money(r['spend'])}, Leads: {r['leads']}, FTDs: 0)"
            )
        lines.append("")
    return "\n".join(lines)


def _render_open_recs(open_recs: Dict[str, List[Dict[str, Any]]]) -> str:
    lines = ["### 11. Что открыть (2 кампании на аккаунт)", ""]
    any_data = any(v for v in open_recs.values())
    if not any_data:
        lines.append("_No ready accounts or no combos to recommend._")
        return "\n".join(lines)

    for acc_label, recs in open_recs.items():
        if not recs:
            continue
        lines.append(f"**{acc_label}:**")
        for i, r in enumerate(recs, 1):
            roi_s = f"ROI {r['roi']:.2f}" if r.get("roi") else ""
            dep_s = f"{r['deposits']} dep"
            cat = r.get("category", "")
            lines.append(
                f"  Campaign {i}: `{r['campaign_name']}`"
            )
            lines.append(
                f"    Ad Set: {r['adset_name']}  [{cat} | {dep_s} {roi_s}]"
            )
        lines.append("")
    return "\n".join(lines)


# ── Country section ────────────────────────────────────────────────────────────

def _render_country_section(section: Dict[str, Any], my_cm: str) -> str:
    country = section["country"]
    parts = [
        f"## Country: {country}",
        "",
        _render_cm_comparison(section.get("cm_comparison", []), my_cm),
        "",
        _render_trend(section.get("trend", {})),
        "",
        _render_top_combos(section.get("top_combos", [])),
        "",
        _render_targeting(section.get("targeting", [])),
        "",
        _render_creatives(section.get("creatives", [])),
        "",
        _render_ai_context(section.get("ai_context", "")),
        "",
        _render_hypotheses(section.get("hypotheses", "")),
        "",
        _render_account_statuses(section.get("account_rows", [])),
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
        parts.append(_render_country_section(section, my_cm))

    if not countries:
        parts.append("_No country data found. Check Excel path and filters._")

    return "\n".join(parts).rstrip() + "\n"
