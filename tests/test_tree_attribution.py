"""Числа книги по кампаниям и объявлениям дерева аккаунта (ACL-1256).

Строка аккаунта на карте показывает лиды, FTD, CPL, CPA и CR из книги
статистики. Под ней — дерево из Ads Manager: кампании и объявления с числами
Meta. Владелец попросил разложить числа аккаунта по этим строкам. Главное
обещание: сумма по кампаниям — ровно строка аккаунта, потому что выборка и
формула одни и те же.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent.morning_report import (  # noqa: E402
    build_account_performance_summary,
    build_tree_attribution,
)


def _book() -> pd.DataFrame:
    """Книга в той форме, в какой её видит отчёт после обогащения."""
    rows = [
        # аккаунт X в Кувейте: две кампании, у первой два объявления
        ("Kuwait", "FacebookX", "KW_camp_A", "ad one", 5, 1, 100.0, "2026-09-02"),
        ("Kuwait", "FacebookX", "KW_camp_A", "ad one", 3, 0, 50.0, "2026-09-03"),
        ("Kuwait", "FacebookX", "KW_camp_A", "ad two", 2, 0, 40.0, "2026-09-03"),
        ("Kuwait", "FacebookX", "KW_camp_B", "ad three", 0, 0, 10.0, "2026-09-04"),
        # имя с двойным пробелом — таблица Meta схлопывает пробелы
        ("Kuwait", "FacebookX", "KW  camp  C", "ad  four", 4, 2, 60.0, "2026-09-04"),
        # тот же аккаунт в другой стране и другой аккаунт — не наши строки
        ("Chile", "FacebookX", "KW_camp_A", "ad one", 9, 9, 999.0, "2026-09-02"),
        ("Kuwait", "FacebookY", "KW_camp_A", "ad one", 7, 7, 777.0, "2026-09-02"),
    ]
    return pd.DataFrame(rows, columns=[
        "Country Final", "Account Alias", "Adset Name", "Ad Title",
        "# Leads", "# FTDs", "$ Spend", "Date",
    ])


def test_campaigns_add_up_to_the_account_row():
    df = _book()
    acc = build_account_performance_summary(df, "Kuwait", "FacebookX")
    got = build_tree_attribution(df, "Kuwait", "FacebookX")
    camps = got["campaigns"].values()
    assert got["has_data"] is True
    assert round(sum(c["spend"] for c in camps), 2) == round(acc["spend"], 2)
    assert sum(c["leads"] for c in camps) == acc["leads"]
    assert sum(c["deposits"] for c in camps) == acc["deposits"]


def test_the_formula_is_the_account_formula():
    got = build_tree_attribution(_book(), "Kuwait", "FacebookX")
    a = got["campaigns"]["KW_camp_A"]
    assert (a["spend"], a["leads"], a["deposits"]) == (190.0, 10, 1)
    assert a["cpl"] == 19.0 and a["cpa"] == 190.0 and a["cr"] == 10.0
    b = got["campaigns"]["KW_camp_B"]
    assert b["leads"] == 0 and b["cpl"] is None and b["cpa"] is None and b["cr"] is None


def test_ads_are_split_under_their_campaign():
    got = build_tree_attribution(_book(), "Kuwait", "FacebookX")
    ads = got["ads"]["KW_camp_A"]
    assert set(ads) == {"ad one", "ad two"}
    assert (ads["ad one"]["spend"], ads["ad one"]["leads"]) == (150.0, 8)
    assert (ads["ad two"]["spend"], ads["ad two"]["leads"]) == (40.0, 2)


def test_names_are_cleaned_like_the_table_cleans_them():
    got = build_tree_attribution(_book(), "Kuwait", "FacebookX")
    assert "KW camp C" in got["campaigns"]
    assert "ad four" in got["ads"]["KW camp C"]


def test_other_countries_and_accounts_are_not_counted():
    got = build_tree_attribution(_book(), "Kuwait", "FacebookX")
    assert got["campaigns"]["KW_camp_A"]["spend"] == 190.0


def test_an_account_without_rows_says_so():
    got = build_tree_attribution(_book(), "Peru", "FacebookX")
    assert got == {"has_data": False, "campaigns": {}, "ads": {}}


# ── запрос страницы ───────────────────────────────────────────────────────

def _ask(monkeypatch, **params):
    from src.web import app as web

    df = _book()
    monkeypatch.setattr(web, "get_cached_report", lambda: ({}, df, []))

    async def _go():
        transport = httpx.ASGITransport(app=web.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/account-attribution", params=params)

    return asyncio.run(_go())


def test_the_page_gets_numbers_formatted_like_the_account_row(monkeypatch):
    from src.analysis_agent.money_format import fmt_usd

    r = _ask(monkeypatch, account="FacebookX", country="Kuwait",
             date_from="2026-09-01", date_to="2026-09-30")
    data = r.json()
    assert r.status_code == 200 and data["ok"] and data["has_data"]
    a = data["campaigns"]["KW_camp_A"]
    assert a == {"spend": fmt_usd(190.0), "leads": 10, "deposits": 1,
                 "cpl": fmt_usd(19.0), "cpa": fmt_usd(190.0), "cr": "10.0%"}
    b = data["campaigns"]["KW_camp_B"]
    assert b["cpl"] is None and b["cpa"] is None and b["cr"] is None, "без лидов — прочерк, не ноль"
    assert data["ads"]["KW_camp_A"]["ad two"]["leads"] == 2


def test_the_period_is_the_period_on_screen(monkeypatch):
    r = _ask(monkeypatch, account="FacebookX", country="Kuwait",
             date_from="2026-09-03", date_to="2026-09-03")
    a = r.json()["campaigns"]["KW_camp_A"]
    assert a["leads"] == 5, "строки вне периода попали в числа"


def test_account_and_country_are_required(monkeypatch):
    r = _ask(monkeypatch, account="FacebookX")
    assert r.status_code == 400


def test_an_empty_period_is_month_to_date_like_the_map(monkeypatch):
    from src.web import app as web

    monkeypatch.setattr(web, "_month_to_date", lambda dates: ("2026-09-03", "2026-09-03"))
    data = _ask(monkeypatch, account="FacebookX", country="Kuwait").json()
    assert (data["date_from"], data["date_to"]) == ("2026-09-03", "2026-09-03")
    assert data["campaigns"]["KW_camp_A"]["leads"] == 5


def test_all_dates_is_asked_for_not_assumed(monkeypatch):
    data = _ask(monkeypatch, account="FacebookX", country="Kuwait", span="all").json()
    assert data["campaigns"]["KW_camp_A"]["leads"] == 10


def test_an_empty_workbook_cell_is_not_a_campaign_called_nan():
    df = _book()
    df.loc[len(df)] = ["Kuwait", "FacebookX", float("nan"), "ad x", 1, 0, 5.0, "2026-09-05"]
    got = build_tree_attribution(df, "Kuwait", "FacebookX")
    assert "nan" not in got["campaigns"]
