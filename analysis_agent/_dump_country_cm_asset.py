"""One-off: print CM + asset aggregates per country for markdown report."""
import pandas as pd
from pathlib import Path

df = pd.read_excel(Path(__file__).parent / "_stats_doc_countries_lastmonth.xlsx")
for c in ["# Leads", "# FTDs", "$ Spend", "$ Net"]:
    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
df["CM"] = df["CM"].astype(str).str.strip()
df["Ad Asset"] = df["Ad Asset"].fillna("—").astype(str).str.strip()

countries = [
    "Albania",
    "Argentina",
    "Bahrain",
    "Chile",
    "Colombia",
    "Ecuador",
    "Guatemala",
    "Kuwait",
    "Mexico",
    "United Arab Emirates",
]


def roi_s(spend: float, net: float) -> str:
    if spend <= 0:
        return "—"
    return f"{net / spend:.2f}"


for co in countries:
    sub = df[df["Country"].astype(str).str.strip() == co]
    if sub.empty:
        continue
    print("===COUNTRY===", co)
    cm = (
        sub.groupby("CM", dropna=False)
        .agg(Deposits=("# FTDs", "sum"), Leads=("# Leads", "sum"), Spend=("$ Spend", "sum"), Net=("$ Net", "sum"))
        .reset_index()
    )
    cm["CPL"] = cm.apply(lambda r: f"{r['Spend'] / r['Leads']:.2f}" if r["Leads"] > 0 else "—", axis=1)
    cm["CPA"] = cm.apply(lambda r: f"{r['Spend'] / r['Deposits']:.2f}" if r["Deposits"] > 0 else "—", axis=1)
    cm["CR%"] = cm.apply(lambda r: round(100 * r["Deposits"] / r["Leads"], 2) if r["Leads"] > 0 else 0.0, axis=1)
    cm["ROI"] = cm.apply(lambda r: roi_s(r["Spend"], r["Net"]), axis=1)
    cm = cm.sort_values("Deposits", ascending=False)
    print(cm.to_string(index=False))
    print("---ASSETS---")
    ast = (
        sub.groupby("Ad Asset", dropna=False)
        .agg(Deposits=("# FTDs", "sum"), Leads=("# Leads", "sum"), Spend=("$ Spend", "sum"), Net=("$ Net", "sum"))
        .reset_index()
    )
    ast["CPL"] = ast.apply(lambda r: f"{r['Spend'] / r['Leads']:.2f}" if r["Leads"] > 0 else "—", axis=1)
    ast["CPA"] = ast.apply(lambda r: f"{r['Spend'] / r['Deposits']:.2f}" if r["Deposits"] > 0 else "—", axis=1)
    ast["CR%"] = ast.apply(lambda r: round(100 * r["Deposits"] / r["Leads"], 2) if r["Leads"] > 0 else 0.0, axis=1)
    ast["ROI"] = ast.apply(lambda r: roi_s(r["Spend"], r["Net"]), axis=1)
    ast = ast.sort_values("Deposits", ascending=False)
    print(ast.to_string(index=False))
    print()
