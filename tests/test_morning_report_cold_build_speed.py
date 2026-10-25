"""Холодная сборка отчёта на годовой книге: быстрые пути дают те же числа.

На годовой книге (258 тыс. строк) сборка утренних отчётов шла ~12 минут:
Workbench считал каждую группу отдельными фильтрами pandas, разделы страны
разбирали заголовки и таргеты построчно. Быстрые пути считают то же
одной группировкой. Здесь они сверяются с прежней построчной реализацией
на кубе с ничьими, пустым байером и разным регистром имени.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import country_workbench as cw  # noqa: E402
from src.analysis_agent import morning_report as mr  # noqa: E402
from src.analysis_agent.analyze_fb_stats import aggregate_combos  # noqa: E402

COLS = ["Country Final", "Offer", "CM", "Account Alias", "Adset Name", "Device", "Age",
        "Interest", "Ad Title", "Variant", "$ Spend", "# Leads", "# FTDs"]


def _random_df(seed: int = 7, n: int = 600) -> pd.DataFrame:
    rnd = random.Random(seed)
    titles = ["1121 1 - Story14", "1121 - 2", "1121 2 - Story14", "2046 1 - Story14",
              "2046 - Story14", "New Engagement Ad", "-", "3300 - Hero", None]
    rows = []
    for _ in range(n):
        rows.append([
            rnd.choice(["Colombia", "Peru"]),
            rnd.choice(["Alpha", "Beta", "Gamma"]),
            rnd.choice(["Eva", " eva ", "EVA", "Bob", "Carl", ""]),
            rnd.choice(["A1", "A2", "A3"]),
            rnd.choice(["CO_Alpha_FX", "CO_Beta_HV", "PE_Gamma_Day"]),
            rnd.choice(["All", "iOS"]),
            rnd.choice(["35+", "25+"]),
            rnd.choice(["FX", "fx", "HV|Trading", "Day", None]),
            rnd.choice(titles),
            rnd.choice(["LP1", "LP2", ""]),
            # Целые суммы — чтобы ничьи по Spend и FTDs были частыми.
            float(rnd.choice([0, 10, 20, 50])),
            rnd.choice([0, 1, 2, 5]),
            rnd.choice([0, 0, 1, 2]),
        ])
    return pd.DataFrame(rows, columns=COLS)


# ── прежняя реализация (до ускорения), дословно ─────────────────────────────

def _old_group(part, keys, make, me, limit):
    rows = [cw._row(make(vals if isinstance(vals, tuple) else (vals,)), sub, me)
            for vals, sub in part.groupby(keys, dropna=False, sort=False)]
    return cw._rank(rows, limit)


def _old_creative_rows(part, me, limit):
    known = part[part["Pack"] != ""]
    packs = []
    for pack, sub in known.groupby("Pack", sort=False):
        versions = _old_group(
            sub, ["CKey"],
            lambda v, s=sub: cw._creative_keys(s[s["CKey"] == v[0]].iloc[0]),
            me, 50)
        row = cw._row({"pack": pack}, sub, me)
        row["versions"] = versions
        packs.append(row)
    packs = cw._rank(packs, limit)
    unresolved = part[part["Pack"] == ""]
    if not unresolved.empty and unresolved["Spend"].sum() > 0:
        row = cw._row({"pack": "", "unresolved": True}, unresolved, me)
        row["versions"] = []
        packs.append(row)
    return packs


GROUPINGS = [
    (["Account", "Adset"], lambda v: {"account": v[0], "name": v[1]}),
    (["Landing"], lambda v: {"landing": v[0]}),
    (["Device", "Age", "Target", "Pack", "Landing"],
     lambda v: dict(zip(["device", "age", "target", "pack", "landing"], v))),
    (["CM"], lambda v: {"cm": v[0]}),
]


def test_grouped_rows_match_the_per_group_implementation():
    df = _random_df()
    for my_cm in ("Eva", "", "Nobody"):
        me = my_cm.strip().casefold()
        cube = cw.build_cube(df, "Colombia")
        # Без предрасчёта байера (как в тестах и прямых вызовах) и с ним (как в сборке).
        prepared = cube.copy()
        prepared["_cm"], prepared["_mine"] = cw._cm_columns(prepared, me)
        for keys, make in GROUPINGS:
            for limit in (3, 15, 1000):
                expected = _old_group(cube, keys, make, me, limit)
                assert cw._group(cube, keys, make, me, limit) == expected
                assert cw._group(prepared, keys, make, me, limit) == expected


def test_creative_rows_match_the_per_group_implementation():
    df = _random_df(seed=11)
    for my_cm in ("Eva", ""):
        me = my_cm.strip().casefold()
        cube = cw.build_cube(df, "Peru")
        for limit in (2, 15):
            expected = _old_creative_rows(cube, me, limit)
            assert cw._creative_rows(cube, me, limit) == expected


def test_the_whole_workbench_is_unchanged_by_the_precomputed_buyer_columns():
    df = _random_df(seed=3)
    wb = cw.build_country_workbench(df, "Colombia", "Eva")
    cube = cw.build_cube(df, "Colombia")
    for name, got in wb["slices"].items():
        part = cube if not name else cube[cube["Asset"] == name]
        me = "eva"
        assert got["campaigns"] == _old_group(
            part, ["Account", "Adset"], lambda v: {"account": v[0], "name": v[1]}, me, cw.ROWS_PER_TAB)
        assert got["creatives"] == _old_creative_rows(part, me, cw.ROWS_PER_TAB)


def test_targeting_and_creative_breakdowns_parse_each_value_once_with_the_same_result():
    df = _random_df(seed=5)
    df_c = df[df["Country Final"] == "Colombia"]
    buckets = mr._map_unique(df_c["Interest"], mr.interest_bucket_key)
    assert buckets.tolist() == df_c["Interest"].map(mr.interest_bucket_key).tolist()

    rows = mr.build_creative_breakdown(df, "Colombia")
    for row in rows:
        mask = df_c["Ad Title"].map(
            lambda t: mr.split_ad_title(t) == (row["creative_id"], row["text_name"]))
        titles = sorted({str(x).strip() for x in df_c.loc[mask, "Ad Title"].dropna()})
        assert row["ad_title"] == (" · ".join(titles) if titles else "(no title)")
        assert row["deposits"] == int(df_c.loc[mask, "# FTDs"].sum())


def _old_row_keys(frame, cols):
    """Ключ комбинации, как его строил прежний apply(axis=1)."""
    def _row_key(r):
        parts = []
        for c in cols:
            v = r.get(c)
            if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and str(v).strip().lower() == "nan"):
                parts.append("__NA__")
            else:
                parts.append(str(v).strip())
        return "_".join(parts)
    return frame[cols].apply(_row_key, axis=1).tolist()


def test_combo_keys_match_the_row_wise_keys():
    df = _random_df(seed=9)
    for col, value in (("Creative", "1121"), ("TextSegment", " Story14 ")):
        df[col] = value
    df.loc[df.index[::4], "Interest"] = None
    df.loc[df.index[::5], "Offer"] = "nan"
    df.loc[df.index[::6], "Age"] = float("nan")
    cols = ["Country Final", "Offer", "Device", "Interest", "Age", "Creative", "TextSegment"]
    grouped = df.groupby(cols, dropna=False).size().reset_index()
    from src.analysis_agent.analyze_fb_stats import _combo_row_keys
    assert _combo_row_keys(grouped, cols) == _old_row_keys(grouped, cols)

    df["$ Net"] = df["$ Spend"] * 2
    df["# FTDs"] = 3  # все комбинации проходят в Strong
    combos = aggregate_combos(df)
    assert not combos.empty
    # Ключ склеивает только TopCM / TopAccount; сами комбинации не меняются.
    assert {"TopCM", "TopAccount"} <= set(combos.columns)
