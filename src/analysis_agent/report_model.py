"""Structured pieces for FB analytics report (combo line formatting, small helpers)."""
from __future__ import annotations

import re
import pandas as pd

from src.knowledge_agent.knowledge_base import format_target_context_line


def strip_date_from_display(s: str) -> str:
    """Убрать дату вида DD.MM.YYYY из строки для вывода."""
    if not s or not isinstance(s, str):
        return s
    s = re.sub(r"\s*\d{1,2}\.\d{1,2}\.\d{4}\s*", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def format_combo_row(row: pd.Series, with_context: bool = False) -> str:
    country = str(row.get("Country Final") or "").strip()
    country_code_raw = row.get("Country Code")
    country_code = (
        str(country_code_raw).strip()
        if country_code_raw and str(country_code_raw).strip().lower() not in ("", "nan")
        else country.upper()
    )
    device = str(row.get("Device") or "ALL").strip()
    interest = str(row.get("Interest") or "Wide").strip()
    age = str(row.get("Age") or "ALL").strip()
    creative = str(row.get("Creative") or "—").strip()
    text_segment = str(row.get("TextSegment") or "OTHER").strip()

    category = str(row.get("Category", "Strong"))
    category_label = "Strong" if category == "Strong" else "Interesting"

    line_header = f"{country} ({country_code})"
    combo_line = f"{country_code} - {device} + {interest} + {age} + {creative} | {text_segment}"

    deposits = int(row.get("Deposits") or 0)
    leads = int(row.get("Leads") or 0)
    cpl_val = row.get("CPL")
    cpl = float(cpl_val) if pd.notna(cpl_val) else 0.0
    cpa_val = row.get("CPA")
    cpa = float(cpa_val) if pd.notna(cpa_val) else 0.0
    cr_val = row.get("CR")
    cr = float(cr_val) if pd.notna(cr_val) else 0.0

    stats_line = (
        f"Stats: Deposits: {deposits} | Leads: {leads} | CPL: ${cpl:.2f} | "
        f"CPA: ${cpa:.2f} | CR: {cr:.2f}%"
    )

    context_line = ""
    if with_context:
        ctx = format_target_context_line(interest)
        if ctx:
            context_line = f"\n{ctx}"

    return f"""{line_header}
[{category_label}]
{combo_line}
{stats_line}{context_line}
"""
