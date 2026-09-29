"""ACL-1377: account names written without the ad account id still join.

Дешборд знает аккаунт как `Facebook_6540_12345678901234` (имя + id
рекламного аккаунта), книга статистики — как `Facebook6540`. Первый токен
их не сводит, хвост тоже (`234` из id против `540` из имени) — и лид 28.09
на 6540 пропадал и из строки аккаунта, и из дерева кампаний.

Новый шаг `workbook_alias_for` между точным именем и хвостом: имя без
`_<10+ цифр>` в конце, только буквы и цифры, без регистра; в пределах
страны; только единственное совпадение.

Книга здесь синтетическая — той же формы, что реальная, но без её данных.
"""
from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

import httpx
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import account_links as links  # noqa: E402
from src.web import app as web  # noqa: E402
from src.web import available_accounts as avail  # noqa: E402

LONG = "Facebook_6540_12345678901234"
CAMP = "CO_test_Video_35_28.09"


def _book(rows) -> pd.DataFrame:
    """(country, alias, campaign, leads, spend, date) → книга как её видит отчёт."""
    return pd.DataFrame(
        [{"Country Final": c, "Account Alias": a, "Adset Name": camp, "Ad Title": "ad 1",
          "# Leads": leads, "# FTDs": 0, "$ Spend": spend, "Date": d, "CM": "Tester"}
         for c, a, camp, leads, spend, d in rows]
    )


def _colombia() -> pd.DataFrame:
    return _book([
        ("Colombia", "Facebook6540", CAMP, 1, 12.5, "2026-09-28"),
        ("Colombia", "Facebook6545", "CO_other_29.09", 4, 30.0, "2026-09-28"),
        ("Colombia", "FacebookDoc19", "CO_doc_28.09", 2, 20.0, "2026-09-28"),
        ("Colombia", "FacebookMKTOBFLX84", "CO_x84_28.09", 3, 15.0, "2026-09-28"),
    ])


# ── ключ имени ────────────────────────────────────────────────────────────

def test_the_key_drops_the_ad_account_id_separators_and_case():
    assert web._account_key(LONG) == "facebook6540"
    assert web._account_key("Facebook6540") == "facebook6540"
    # короткое число — часть имени, а не id аккаунта
    assert web._account_key("Facebook_6540_123") == "facebook6540123"
    assert web._account_key("_12345678901234") == ""


# ── workbook_alias_for ────────────────────────────────────────────────────

def test_the_long_dashboard_name_finds_the_workbook_name():
    assert web.workbook_alias_for(_colombia(), "Colombia", LONG) == "Facebook6540"


def test_6540_and_6545_stay_separate():
    df = _colombia()
    assert web.workbook_alias_for(df, "Colombia", "Facebook_6545_12345678901235") == "Facebook6545"
    only_6540 = df[df["Account Alias"] == "Facebook6540"]
    assert web.workbook_alias_for(only_6540, "Colombia", "Facebook_6545_12345678901235") == \
        "Facebook_6545_12345678901235", "6545 не должен находить 6540"


def test_an_ambiguous_key_matches_nothing():
    """Два имени книги с одним ключом — подставлять одно из них наугад нельзя."""
    df = _book([
        ("Colombia", "Facebook6540", CAMP, 1, 12.5, "2026-09-28"),
        ("Colombia", "Facebook_6540", CAMP, 1, 12.5, "2026-09-28"),
    ])
    assert web.workbook_alias_for(df, "Colombia", LONG) == LONG


def test_the_match_is_scoped_to_the_country():
    df = _book([
        ("Mexico", "Facebook6540", CAMP, 1, 12.5, "2026-09-28"),
        ("Colombia", "FacebookOther", CAMP, 1, 12.5, "2026-09-28"),
    ])
    assert web.workbook_alias_for(df, "Colombia", LONG) == LONG
    assert web.workbook_alias_for(df, "Mexico", LONG) == "Facebook6540"
    # тот же ключ в другой стране не делает совпадение двусмысленным
    both = pd.concat([df, _colombia()])
    assert web.workbook_alias_for(both, "Colombia", LONG) == "Facebook6540"


def test_the_key_comes_before_the_tail():
    """Хвост `234` из id совпал бы с чужим аккаунтом — имя важнее догадки."""
    df = pd.concat([_colombia(), _book([("Colombia", "FacebookZZ234", CAMP, 9, 99.0, "2026-09-28")])])
    assert web.workbook_alias_for(df, "Colombia", LONG) == "Facebook6540"


def test_an_exact_first_token_match_is_unchanged():
    got = web.workbook_alias_for(_colombia(), "Colombia", "FacebookDoc19 - Eros Andersson")
    assert got == "FacebookDoc19 - Eros Andersson"


def test_the_tail_rule_is_unchanged():
    assert web.workbook_alias_for(_colombia(), "Colombia", "KLD_BFL_X84") == "FacebookMKTOBFLX84"


def test_the_workbook_normaliser_is_untouched():
    from src.analysis_agent.analyze_fb_stats import normalize_account_alias

    assert normalize_account_alias(LONG) == LONG
    assert normalize_account_alias("FacebookDoc19 - Eros Andersson") == "FacebookDoc19"


# ── /account-attribution ──────────────────────────────────────────────────

def _ask(monkeypatch, df, **params):
    monkeypatch.setattr(web, "get_cached_report", lambda: ({}, df, []))

    async def _go():
        transport = httpx.ASGITransport(app=web.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/account-attribution", params=params)

    return asyncio.run(_go())


def test_the_campaign_tree_gets_the_lead_for_the_long_name(monkeypatch):
    r = _ask(monkeypatch, _colombia(), account=LONG, country="Colombia",
             date_from="2026-09-28", date_to="2026-09-28")
    data = r.json()
    assert r.status_code == 200 and data["ok"] and data["has_data"]
    assert data["alias"] == "Facebook6540"
    assert data["campaigns"][CAMP]["leads"] == 1
    assert set(data["campaigns"]) == {CAMP}, "в дерево попали кампании чужого аккаунта"


# ── строка аккаунта на карте ──────────────────────────────────────────────

@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    map_path = str(tmp_path / "ad_account_map.csv")
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: map_path)
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: map_path)
    with open(map_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["account_name", "ad_account_id", "profile_id"])
        w.writeheader()
        w.writerow({"account_name": LONG, "ad_account_id": "act_1", "profile_id": "p-1"})
    yield tmp_path


def test_the_account_row_gets_the_lead_for_the_long_name(_isolated):
    links.link(LONG, "country", "Colombia")
    view = web._build_account_map_view({"countries": []}, _colombia(), span="all")
    colombia = [c for c in view["countries"] if c["country"] == "Colombia"][0]
    row = [r for r in colombia["rows"] if r["account"] == LONG][0]
    assert row["has_data"] is True and row["leads"] == 1
    assert row["workbook_alias"] == "Facebook6540", "не видно, чьи числа подставлены"
    assert row["in_workbook"] is True


# ── список Available accounts ─────────────────────────────────────────────

def test_the_available_list_knows_the_long_name_too():
    activity = avail.workbook_activity(_colombia())
    assert avail.workbook_key(LONG, activity) == "facebook6540"
    assert avail.workbook_key(LONG, activity, by_tail=True) == "facebook6540"
    assert avail.workbook_key("Facebook_6546_12345678901234", activity) == ""
