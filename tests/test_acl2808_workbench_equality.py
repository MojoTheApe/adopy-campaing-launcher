"""ACL-2808: Workbench считается одним проходом, цифры и порядок строк прежние.

`tests/fixtures/acl2808_workbench_golden.json` снят со старого кода (срез за
срезом, ~700 мелких группировок) на этой же синтетической таблице. Новый код
обязан дать тот же результат: числа, строки, порядок.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import country_workbench as cw  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "acl2808_workbench_golden.json"

COLS = ["Country Final", "Offer", "CM", "Account Alias", "Adset Name", "Device", "Age",
        "Interest", "Ad Title", "Variant", "$ Spend", "# Leads", "# FTDs"]

_COUNTRIES = ["Colombia", "Peru", "Chile", "Narnia"]
_OFFERS = ["Alpha", "Beta", "Gamma", "Delta", "Eps", "Zeta", "Eta", "Theta", "Iota"]
_CMS = ["Eva", "Bob", "Carl", "Dina", " eva ", "", "Eva"]
_TITLES = ["1121 1 - Story14", "1121 - 2", "1121 3 - Other", "1122 1 - A", "1122 2 - B",
           "1123", "1124 - 5", "no digits here", "", "-", "1125 7 - Zed", "1125 7 - Zed2"]
_INTERESTS = ["FX", "Trading", "HV|Trading", "Crypto", "", "AAY_Day", "fx"]
_DEVICES = ["All", "iOS", "Android"]
_AGES = ["35+", "25-45", "18+"]
_VARIANTS = ["LP1", "LP2", "", "LP3"]


def make_df(seed: int = 2808, rows: int = 600) -> pd.DataFrame:
    rnd = random.Random(seed)
    out = []
    for _ in range(rows):
        country = rnd.choice(_COUNTRIES)
        offers = _OFFERS if country == "Colombia" else _OFFERS[:3]
        ftds = rnd.choice([0, 0, 0, 1, 1, 2, 3, 5])
        leads = rnd.choice([0, 3, 7, 12, 25, 40])
        out.append([
            country, rnd.choice(offers), rnd.choice(_CMS), rnd.choice(["A1", "A2", "A3"]),
            rnd.choice(["Ad1", "Ad2", "Ad3", "Ad4"]), rnd.choice(_DEVICES), rnd.choice(_AGES),
            rnd.choice(_INTERESTS), rnd.choice(_TITLES), rnd.choice(_VARIANTS),
            rnd.choice([0, 10, 25.5, 40, 99.99, 150]), leads, ftds,
        ])
    # целые «+» значения: у строк с одинаковыми FTD/Spend порядок задаёт первое появление
    return pd.DataFrame(out, columns=COLS)


def snapshot(df: pd.DataFrame) -> dict:
    snap = {}
    for country in _COUNTRIES + ["Absent"]:
        for cm in ("Eva", " bob ", ""):
            snap[f"{country}|{cm}"] = cw.build_country_workbench(df, country, cm)
    return json.loads(json.dumps(snap, default=str))


def test_workbench_matches_the_pre_vectorized_golden():
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = snapshot(make_df())
    assert list(actual) == list(expected)
    for key in expected:
        assert actual[key] == expected[key], key
    # Сравнение словарей не видит порядок строк; списки — видят, но на всякий случай
    # сверяем и сериализацию целиком (порядок ключей внутри строк тоже прежний).
    assert json.dumps(actual, sort_keys=False) == json.dumps(expected, sort_keys=False)


def test_golden_is_not_trivially_empty():
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    wb = expected["Colombia|Eva"]
    assert len(wb["assets"]) == 1 + cw.MAX_ASSETS
    sl = wb["slices"][""]
    assert sl["creatives"] and sl["setups"] and sl["campaigns"]
    assert any(c.get("unresolved") for c in sl["creatives"])
    assert any(s["creative"] for s in sl["setups"])


# ── Остальные разделы страны и сводка источника (раунд 2) ───────────────────

SECTION_GOLDEN = Path(__file__).parent / "fixtures" / "acl2808_sections_golden.json"


def make_section_df(seed: int = 28081, rows: int = 400) -> pd.DataFrame:
    rnd = random.Random(seed)
    base = make_df(seed, rows)
    base["Date"] = [f"2026-11-{rnd.randint(1, 12):02d}" for _ in range(len(base))]
    base["Creative"] = [rnd.choice(_TITLES + ["Image 3", "Video 9"]) for _ in range(len(base))]
    base["$ Net"] = [rnd.choice([0, 5, 12.5, -3]) for _ in range(len(base))]
    base["VariantId"] = [rnd.choice(["", "1", "2", "10"]) for _ in range(len(base))]
    base.loc[base.index[::37], "Interest"] = None
    return base


def section_snapshot(df: pd.DataFrame) -> dict:
    from src.analysis_agent import morning_report as mr

    snap = {}
    for country in _COUNTRIES:
        snap[country] = {
            "trend": mr.build_trend(df, country),
            "creatives": mr.build_creative_breakdown(df, country),
            "targeting": mr.build_targeting_breakdown(df, country),
            "cm": mr.build_cm_comparison(df, country, "Eva"),
            "assets": mr.build_asset_summary(df, country),
            "asset_variants": mr.build_asset_variants(df, country),
            "variants": mr.build_variant_breakdown(df, country),
        }
    return json.loads(json.dumps(snap, default=str))


def test_country_sections_match_the_pre_change_golden():
    expected = json.loads(SECTION_GOLDEN.read_text(encoding="utf-8"))
    actual = section_snapshot(make_section_df())
    assert json.dumps(actual) == json.dumps(expected)


def test_sum_agg_equals_the_named_aggregation():
    from src.analysis_agent.morning_report import _sum_agg

    df = make_section_df().rename(columns={"$ Spend": "Spend$"}).rename(columns={"Spend$": "$ Spend"})
    for keys in ("Offer", ["Offer", "Interest"], ["Country Final", "Device", "Interest"]):
        old = (df.groupby(keys, dropna=False)
               .agg(Deposits=("# FTDs", "sum"), Leads=("# Leads", "sum"), Spend=("$ Spend", "sum"))
               .reset_index())
        new = _sum_agg(df, keys, Deposits="# FTDs", Leads="# Leads", Spend="$ Spend")
        pd.testing.assert_frame_equal(new, old)


def test_source_metadata_over_distinct_values_equals_the_per_row_result():
    from src.analysis_agent import bi_compare as bc

    def old_setting(frame, key, default):  # построчный эталон: clean(v) на каждой строке
        column = bc.METADATA_COLUMNS[key]
        if frame is None or frame.empty or column not in frame:
            return default, True
        clean = bc.timezone_name if key == "timezone" else bc.text
        declared = [clean(v) for v in frame[column]]
        values = {v or default for v in declared}
        if len(values) != 1:
            return "", False
        return values.pop(), not any(declared)

    def old_currency(frame):
        column = bc.METADATA_COLUMNS["currency_code"]
        if frame is None or frame.empty or column not in frame:
            return bc.DEFAULT_SOURCE_CURRENCY, True
        declared = [bc.currency_code(v) for v in frame[column]]
        values = {v or bc.DEFAULT_SOURCE_CURRENCY for v in declared}
        if len(values) != 1:
            return "", False
        return values.pop(), not any(declared)

    nan = float("nan")
    frame = pd.DataFrame({
        bc.METADATA_COLUMNS["currency_code"]: ["usd", None, "USD", "", nan, "unknown", "EUR"],
        bc.METADATA_COLUMNS["timezone"]: ["Asia/Jerusalem", None, "", "UTC", "Etc/UTC", "Nowhere/Land", "UTC"],
        bc.METADATA_COLUMNS["day_boundary"]: ["calendar_day", "", None, "calendar_day", "x", "nan", ""],
        bc.SOURCE_COLUMN: ["bi", "bi", "workbook", None, "", "bi", "bi"],
    })
    parts = [frame, frame.iloc[:1], frame.iloc[[0, 2]], frame.iloc[[1, 4]], frame.iloc[[1, 3]],
             frame.iloc[[2, 6]], frame.iloc[:0], frame.drop(columns=[bc.SOURCE_COLUMN])]
    for part in parts:
        assert bc._source_currency(part) == old_currency(part)
        for key, default in (("timezone", bc.DEFAULT_SOURCE_TIMEZONE),
                             ("day_boundary", bc.DEFAULT_SOURCE_DAY_BOUNDARY)):
            assert bc._source_setting(part, key, default) == old_setting(part, key, default)
        if not part.empty and bc.SOURCE_COLUMN in part:
            sources = sorted({bc.text(v) or "unknown" for v in part[bc.SOURCE_COLUMN]})
            assert bc.provenance(frame=part)["sources"] == sources
