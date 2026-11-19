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
