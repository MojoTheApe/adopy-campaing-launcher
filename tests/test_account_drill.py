"""Раскрытие аккаунта: свой вопрос, а не чужой.

Сравнение менеджеров спрашивает «у кого депозиты». Раскрытие аккаунта — «что
этот аккаунт крутил». Пока второе переиспользовало правило первого целиком,
аккаунт без депозитов отвечал «No matching rows», и это читалось как поломка
раскрытия, а не как «депозитов не было».
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analysis_agent import morning_drilldown as md  # noqa: E402
from src.web import app as web_app  # noqa: E402


def _frame(ftds: list[int]) -> pd.DataFrame:
    n = len(ftds)
    return pd.DataFrame({
        "Country Final": ["Colombia"] * n,
        "Account Alias": ["FacebookMKTOBFLX01"] * n,
        "Adset Name": [f"adset {i}" for i in range(n)],
        "# FTDs": ftds,
        "# Leads": [10] * n,
        "$ Spend": [100.0 + i for i in range(n)],
        "CM": ["Evgeni"] * n,
        "Ad Asset": ["asset"] * n,
        "Date": ["2026-09-05"] * n,
    })


def test_an_account_without_deposits_still_opens():
    """61 лид и ноль депозитов — панель обязана показать адсеты."""
    rows = md.get_underlying_rows(
        _frame([0, 0, 0]), section="account",
        country="Colombia", account="FacebookMKTOBFLX01",
    )
    assert len(rows) == 3, "аккаунт без депозитов снова отвечает пустотой"


def test_the_manager_comparison_still_asks_for_deposits():
    """Правило не отменено — оно осталось там, где и было верным."""
    rows = md.get_underlying_rows(
        _frame([0, 2, 0]), section="cm_comparison",
        country="Colombia", cm="Evgeni",
    )
    assert len(rows) == 1, "сравнение менеджеров перестало отбирать по депозитам"


def test_deposits_come_first_then_the_biggest_spend():
    rows = md.get_underlying_rows(
        _frame([0, 1, 0]), section="account",
        country="Colombia", account="FacebookMKTOBFLX01",
    )
    # строки приходят уже переименованными: ftds/spend, а не «# FTDs»
    assert rows[0]["ftds"] == 1, "адсет с депозитом должен идти первым"
    assert rows[1]["spend"] >= rows[2]["spend"], "без депозитов порядок по расходу"


def test_the_label_does_not_promise_deposits():
    """«7 campaigns with deposits» над строками без единого депозита."""
    tpl = (
        Path(__file__).resolve().parent.parent
        / "src" / "web" / "templates" / "_details_fragment.html"
    ).read_text(encoding="utf-8")
    at = tpl.index("section == 'account'")
    assert "with deposits" not in tpl[at: at + 200], (
        "подпись раскрытия аккаунта снова обещает депозиты"
    )


def test_a_multilogin_name_finds_its_workbook_rows():
    """`KLD_BFL_X01` в связке — `FacebookMKTOBFLX01` в книге.

    Пока перевод имени жил только в карте, строка показывала спенд, а
    раскрытие под ней отвечало «нет строк».
    """
    df = _frame([1, 0])
    assert web_app.workbook_alias_for(df, "Colombia", "KLD_BFL_X01") == "FacebookMKTOBFLX01"
    assert web_app.workbook_alias_for(df, "Colombia", "FacebookMKTOBFLX01") == "FacebookMKTOBFLX01"
