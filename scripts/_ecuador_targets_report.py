"""One-off: Ecuador targets-only markdown (Interest aggregate)."""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))

import pandas as pd

from config.paths import get_excel_path
from src.analysis_agent.analyze_fb_stats import compute_metrics, enrich_structure

OUT = _root / "analysis_agent" / "Ecuador_Targets_Only.md"


def main() -> None:
    df = pd.read_excel(get_excel_path())
    df = compute_metrics(df)
    df = enrich_structure(df)
    ec = df[df["Country Final"].astype(str).str.strip().str.lower() == "ecuador"].copy()
    ec["Interest"] = ec["Interest"].fillna("").astype(str).str.strip()
    ec.loc[ec["Interest"].str.lower().isin(["", "nan", "none"]), "Interest"] = "(пусто / не распознан)"
    g = (
        ec.groupby("Interest", dropna=False)
        .agg(
            Leads=("# Leads", "sum"),
            Deposits=("# FTDs", "sum"),
            Spend=("$ Spend", "sum"),
            Net=("$ Net", "sum"),
        )
        .reset_index()
    )
    g["CPL"] = g["Spend"] / g["Leads"].replace(0, pd.NA)
    g["CPA"] = g["Spend"] / g["Deposits"].replace(0, pd.NA)
    g["CR%"] = (g["Deposits"] / g["Leads"].replace(0, pd.NA)) * 100
    g["ROI"] = g["Net"] / g["Spend"].replace(0, pd.NA)
    g = g.sort_values("Spend", ascending=False)

    def fmt_money(x):
        if pd.isna(x):
            return "—"
        return f"${x:,.2f}"

    def fmt_pct(x):
        if pd.isna(x):
            return "—"
        return f"{x:.2f}%"

    def fmt_roi(x):
        if pd.isna(x):
            return "—"
        return f"{x:.2f}"

    src = get_excel_path()
    lines = [
        "# Эквадор: отчёт только по таргетам (Interest из Adset Name)",
        "",
        f"*Источник: `{src}` · строк в Эквадоре: {len(ec)} · уникальных таргетов: {len(g)}*",
        "",
        "| Таргет (Interest) | Leads | Deposits | Spend | Net | CPL | CPA | CR% | ROI |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in g.iterrows():
        t = str(r["Interest"]).replace("|", "\\|")
        row = (
            f"| {t} | {int(r['Leads'])} | {int(r['Deposits'])} | {fmt_money(r['Spend'])} | "
            f"{fmt_money(r['Net'])} | {fmt_money(r['CPL'])} | {fmt_money(r['CPA'])} | "
            f"{fmt_pct(r['CR%'])} | {fmt_roi(r['ROI'])} |"
        )
        lines.append(row)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Written {OUT}")


if __name__ == "__main__":
    main()
