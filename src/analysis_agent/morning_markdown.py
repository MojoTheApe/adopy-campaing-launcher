"""Render a MorningReport dict → Markdown string.

All numbered sections per country (0–10), structured for easy morning reading in Cursor.
"""
from __future__ import annotations

from typing import Any, Dict, List

from src.analysis_agent.gsheet_accounts import format_account_status_todo_cell

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


def _safe_money_sort_key(v: str) -> float:
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _fmt_float(v: float, decimals: int = 2) -> str:
    return f"{v:.{decimals}f}"


def _combo_label(*parts: Any) -> str:
    return " + ".join(str(part).strip() for part in parts if str(part).strip())


def _ellipsis(s: Any, max_len: int) -> str:
    text = str(s).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _compact_type_label(category: str, flag: str) -> str:
    label = "Int." if category == "Interesting" else category
    return label + flag


# Legend for editors: Upload FB / GSheet column K workflow (see gsheet_accounts module doc).
_ACCOUNT_STATUSES_LEGEND = (
    "_**Spend / Leads / FTDs / CPL / CPA / CR** — full-account totals from the Facebook stats export "
    "(Excel). "
    "7–8 **To Do** for ready accounts (✅) mirrors **section 10** (same two campaigns, first creative "
    "each): compact `slug/target/creative` with offer **slug** like §10 naming, `; ` between bundles; "
    "trailing `⚠` = below benchmark. Full names are in §10. "
    "If section 10 has nothing for that account, **To Do** is **—**. **Video selfie** keeps the "
    "GSheet note. Otherwise (not ready) uses col J/K. "
    "GSheet rows with **Account Disabled** or **Account Restricted** in col J are omitted._"
)


# ── Section renderers ─────────────────────────────────────────────────────────

def _render_cm_comparison(rows: List[Dict[str, Any]], my_cm: str) -> str:
    lines = ["### 1. Evgeni vs Team", ""]
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


def _render_news_context(news_context: Dict[str, Any]) -> str:
    lines = ["### 0. News Context", ""]
    items = news_context.get("items") or []
    reliable = [item for item in items if item.get("confidence") != "low"]
    if not items or not reliable:
        lines.append("_No reliable recent asset news found. Use evergreen angles from working combos._")
        return "\n".join(lines)

    for item in items:
        asset_title = f"**{_esc(item.get('asset', ''))}**"
        theme_label = item.get("theme_label")
        if theme_label:
            label_kind = "combo" if item.get("interest_source") == "combo" else "theme"
            asset_title += f" _({label_kind}: {_esc(theme_label)})_"
        lines.append(asset_title)
        lines.append(f"- Signal: {_esc(item.get('signal', ''))}")
        summary = item.get("summary")
        if summary:
            lines.append(f"- Why it matters: {_esc(summary)}")
        fd = item.get("freshness_days")
        if fd is not None:
            lines.append(f"- Recency: ~{int(round(float(fd)))} days ago")
        query_basis = item.get("query_basis")
        if query_basis:
            lines.append(f"- Query basis: {_esc(query_basis)}")
        headline_angle = item.get("headline_angle")
        if headline_angle:
            lines.append(f"- Headline angle: {_esc(headline_angle)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_trend(trend: Dict[str, Any]) -> str:
    lines = ["### 2. Trend (Recent Days)", ""]
    if not trend.get("has_date"):
        lines.append("_Date column not found in Excel — trend analysis unavailable._")
        return "\n".join(lines)

    lines.append(f"Period: **{trend.get('period_label', '')}**")
    lines.append("")

    up = trend.get("trending_up", [])
    down = trend.get("trending_down", [])
    stable = trend.get("stable", [])

    if up:
        lines.append("**New (all deposits from recent period):**")
        for e in up[:5]:
            lines.append(
                f"- {e['device']} + {e['interest']} + {e['creative'][:30]}"
                f"  [{e['offer']}]  dep: {e['deposits_recent']}"
            )
        lines.append("")

    if stable:
        lines.append("**Stable (deposits before and now):**")
        for e in stable[:5]:
            lines.append(
                f"- {e['device']} + {e['interest']} + {e['creative'][:30]}"
                f"  [{e['offer']}]  total: {e['deposits_total']}, recent: {e['deposits_recent']}"
            )
        lines.append("")

    if down:
        lines.append("**Stopped (had deposits, now 0):**")
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
    lines = ["### 3. Working Combos", ""]
    if not combos:
        lines.append("_No Strong/Interesting combos for this country._")
        return "\n".join(lines)

    table_rows = []
    for c in combos:
        quality = c.get("quality", "ok")
        flag = " ✓" if quality == "ok" else " ⚠"
        type_label = _compact_type_label(c["category"], flag)
        roi_s = _fmt_float(c["roi"]) if c["roi"] else "—"
        cpa_s = _fmt_money(c["cpa"]) if c["cpa"] else "—"
        cpl_s = _fmt_money(c.get("cpl", 0)) if c.get("cpl") else "—"
        combo_label = _ellipsis(
            _combo_label(c["device"], c["interest"], c["age"], c["creative"][:25]),
            34,
        )
        table_rows.append([
            type_label,
            _ellipsis(c["offer"], 8),
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
    lines = ["### 4. Targeting", ""]
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
    lines = ["### 4. Creatives", ""]
    if not rows:
        lines.append("_No creative data._")
        return "\n".join(lines)

    table_rows = []
    for r in rows:
        cpa_s = _fmt_money(r["cpa"]) if r["cpa"] else "—"
        table_rows.append([
            _ellipsis(r.get("ad_title") or "", 72),
            str(r["deposits"]),
            str(r["leads"]),
            _fmt_money(r["spend"]),
            cpa_s,
        ])
    lines.append(_table(["Ad Title", "Dep", "Leads", "Spend", "CPA"], table_rows))
    return "\n".join(lines)


def _render_ai_context(block: str) -> str:
    return "\n".join([
        "### 5. AI Context (copy into chat)",
        "",
        "```",
        block,
        "```",
    ])


def _render_hypotheses(block: str) -> str:
    return "\n".join([
        "### 6. Hypotheses (copy into chat)",
        "",
        "```",
        block,
        "```",
    ])


def _render_account_statuses(account_rows: List[Dict[str, Any]], has_gsheet: bool = False) -> str:
    lines = ["### 7–8. Account Statuses", "", _ACCOUNT_STATUSES_LEGEND, ""]
    if not account_rows:
        if has_gsheet:
            lines.append("_No accounts found for this country in the GSheet._")
        else:
            lines.append(
                "_GSheet account data not loaded. Set GSHEET_ACCOUNTS_ID in .env to enable._"
            )
        return "\n".join(lines)

    for r in account_rows:
        ready_s = "✅" if r["is_ready"] else "❌"
        if "to_do_display" in r:
            to_do_s = r["to_do_display"]
        else:
            to_do_s = format_account_status_todo_cell(
                str(r.get("to_do", "")), str(r.get("to_do_extra", "") or ""),
            )
        ex = r.get("account_excel") or {}
        if ex.get("has_data"):
            spend_s = _fmt_money(ex["spend"])
            leads_s = str(ex["leads"])
            ftds_s = str(ex["deposits"])
            cpl_v = ex.get("cpl")
            cpa_v = ex.get("cpa")
            cr_v = ex.get("cr")
            cpl_s = _fmt_money(cpl_v) if cpl_v is not None else "—"
            cpa_s = _fmt_money(cpa_v) if cpa_v is not None else "—"
            cr_s = f"{cr_v:.1f}%" if cr_v is not None else "—"
        else:
            spend_s = leads_s = ftds_s = cpl_s = cpa_s = cr_s = "—"
        stock_s = r.get("stock", "") or "—"
        account = _esc(r["account_label"])
        perf = (
            f"{spend_s} spend | {leads_s} leads | {ftds_s} FTDs | "
            f"CPL {cpl_s} | CPA {cpa_s} | CR {cr_s}"
        )
        lines.extend([
            f"**{account}** {ready_s}",
            f"Perf: {_esc(perf)}",
            f"Status: {_esc(stock_s)}",
            f"To Do: {_esc(to_do_s)}",
            "",
        ])
    return "\n".join(lines)


def _optimization_campaign_cell(adset_name: str) -> str:
    """Excel exports sometimes aggregate leads under ``Adset Name`` ``-`` with zero spend."""
    s = str(adset_name or "").strip()
    lower = s.lower()
    if not s or s == "-" or lower in ("nan", "none"):
        return "Account-level (no ad set in export)"
    return s[:45]


def _render_close_recs(
    close_recs: Dict[str, List[Dict[str, Any]]],
    close_summaries: Dict[str, Dict[str, Any]] | None = None,
) -> str:
    lines = [
        "### 9. Optimization",
        "",
        "_Ad-level drill-down: double-click `start_dashboard.bat` or run `python serve.py` (opens http://127.0.0.1:9090/)._",
        "",
    ]
    any_data = any(v for v in close_recs.values())
    if not any_data:
        lines.append("_No campaign spend found for ready accounts._")
        return "\n".join(lines)

    for acc_label, recs in close_recs.items():
        if not recs:
            continue
        table_rows = []
        sorted_recs = sorted(
            recs,
            key=lambda r: _safe_money_sort_key(_fmt_money(r["spend"])),
            reverse=True,
        )
        for r in sorted_recs:
            cpl_s = _fmt_money(r["cpl"]) if r.get("cpl") else "—"
            cpa_s = _fmt_money(r["cpa"]) if r.get("cpa") else "—"
            table_rows.append([
                _optimization_campaign_cell(r["adset_name"]),
                _fmt_money(r["spend"]),
                str(r["leads"]),
                str(r["deposits"]),
                cpl_s,
                cpa_s,
            ])
        lines.append(f"**{acc_label}:**")
        lines.append("")
        lines.append(_table(
            ["Ad set name", "Spend", "Leads", "FTDs", "CPL", "CPA"],
            table_rows,
        ))
        lines.append("")
    return "\n".join(lines)


def _render_open_recs(open_recs: Dict[str, List[Dict[str, Any]]]) -> str:
    lines = [
        "### 10. What to Open (4 strategies per account)",
        "",
        "_Aligned with §2 Trend: **1** stable · **2** new trend + variations · "
        "**3** recovery (stopped) · **4** test when no stable trend._",
        "",
    ]
    any_data = any(v for v in open_recs.values())
    if not any_data:
        lines.append("_No ready accounts or no combos to recommend._")
        return "\n".join(lines)

    for acc_label, recs in open_recs.items():
        if not recs:
            continue
        lines.append(f"**{acc_label}:**")
        lines.append("")
        for r in recs:
            strategy_label = r.get("strategy_label") or ""
            argument = r.get("argument", "")
            below = r.get("below_benchmark", False)
            warn_note = "  ⚠ below benchmark" if below else ""

            lines.append(f"  **{strategy_label}**")
            lines.append(
                f"  `{r['campaign_name']}`"
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
    ]
    if section.get("news_context"):
        parts.extend([
            _render_news_context(section.get("news_context", {})),
            "",
        ])
    parts.extend([
        _render_cm_comparison(section.get("cm_comparison", []), my_cm),
        "",
        _render_asset_summary(section.get("asset_summary", [])),
        "",
        _render_trend(section.get("trend", {})),
        "",
        _render_top_combos(section.get("top_combos", [])),
        "",
        _render_creatives(section.get("creatives", [])),
        "",
        _render_ai_context(section.get("ai_context", "")),
        "",
        _render_hypotheses(section.get("hypotheses", "")),
        "",
        _render_account_statuses(section.get("account_rows", []), has_gsheet=has_gsheet),
        "",
        _render_close_recs(
            section.get("close_recs", {}),
            section.get("close_summaries", {}),
        ),
        "",
        _render_open_recs(section.get("open_recs", {})),
        "",
        "---",
        "",
    ])
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
        "**How to use:** Section **4** lists top Ad Titles per country (deposits / spend). "
        "Paste the section **5–6** code blocks into chat for AI. "
        "Section **7–8** lists GSheet account statuses (see the legend under each country).",
        "",
        "---",
        "",
    ]

    for section in countries:
        parts.append(_render_country_section(section, my_cm, has_gsheet=has_gsheet))

    if not countries:
        parts.append("_No country data found. Check Excel path and filters._")

    return "\n".join(parts).rstrip() + "\n"
