"""Подключённый аккаунт должен видеть свои числа из книги.

Книга знает аккаунт как `FacebookMKTOBFLX82`, Multilogin — как `KLD_BFL_X82`.
Пока соединяли строго по имени, всякий добавленный аккаунт показывал сплошные
прочерки: данные были, а join промахивался. Общее у имён — хвост, тот самый,
которым аккаунт называют вслух.

Отдельно проверяется, что двусмысленное совпадение не принимается: подставленное
наугад число выглядит точно так же, как правильное.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.web import app as web_app  # noqa: E402


def test_tail_is_the_last_three_alphanumerics():
    assert web_app._account_tail("KLD_BFL_X82") == "X82"
    assert web_app._account_tail("FacebookMKTOBFLX82") == "X82"
    assert web_app._account_tail("FacebookElf191") == "191"
    assert web_app._account_tail("ab") == ""


def test_a_single_match_joins():
    known = {"FacebookMKTOBFLX82": {}, "FacebookMKTOBFLW07": {}}
    assert web_app._alias_by_tail("KLD_BFL_X82", known) == "FacebookMKTOBFLX82"


def test_an_ambiguous_tail_is_refused():
    """Два имени с одним хвостом — не повод взять первое."""
    known = {"FacebookOneX82": {}, "FacebookTwoX82": {}}
    assert web_app._alias_by_tail("KLD_BFL_X82", known) == ""


def test_an_unknown_tail_stays_unmatched():
    known = {"FacebookMKTOBFLW07": {}}
    assert web_app._alias_by_tail("KLD_BFL_X84", known) == ""


def test_the_row_says_which_name_the_numbers_came_from():
    """Совпадение по хвосту не должно выглядеть как собственное имя аккаунта."""
    tpl = (
        Path(__file__).resolve().parent.parent
        / "src" / "web" / "templates" / "_account_map_country.html"
    ).read_text(encoding="utf-8")
    assert "r.workbook_alias" in tpl, "на экране не видно, откуда взяты числа"


def test_an_account_does_not_match_itself():
    """Свой хвост у имени, разумеется, единственный.

    Без этого на экране появлялась подпись «= KLD_BFL_X01» напротив аккаунта
    KLD_BFL_X01 — совпадение с самим собой, объясняющее ровно ничего.
    """
    known = {"KLD_BFL_X01": {}, "FacebookMKTOBFLW07": {}}
    assert web_app._alias_by_tail("KLD_BFL_X01", known) == ""


def test_numbers_do_not_depend_on_todays_report():
    """Числа карты считаются из книги за период, а не из отчёта за сегодня.

    8 сентября книга заканчивалась 7-м, отчёт за сегодня был пуст — и карта
    показывала прочерки у всех аккаунтов сразу, хотя данные за выбранный
    период в книге лежали. Проверяется тем, что расчёт вообще не смотрит на
    report.countries: пустой отчёт с непустой книгой обязан дать числа.
    """
    import pandas as pd

    df = pd.DataFrame(
        {
            "Date": ["2026-09-05", "2026-09-06"],
            "Country Final": ["Kuwait", "Kuwait"],
            "Account Alias": ["FacebookMKTOBFLV97", "FacebookMKTOBFLV97"],
            "$ Spend": [100.0, 50.0],
            "# Leads": [4, 2],
            "# FTDs": [1, 0],
        }
    )
    from src.analysis_agent.morning_report import build_account_performance_summary

    got = build_account_performance_summary(df, "Kuwait", "FacebookMKTOBFLV97")
    assert got["has_data"] is True
    assert round(got["spend"], 2) == 150.0
    assert got["leads"] == 6


def test_a_suffixed_link_still_finds_the_workbook_name():
    """`FacebookMKTOBFLW27 CR Ait` в связке — `FacebookMKTOBFLW27` в книге."""
    import pandas as pd
    from src.analysis_agent.morning_report import build_account_performance_summary

    df = pd.DataFrame(
        {
            "Date": ["2026-09-05"],
            "Country Final": ["Costa Rica"],
            "Account Alias": ["FacebookMKTOBFLW27"],
            "$ Spend": [12.0],
            "# Leads": [1],
            "# FTDs": [0],
        }
    )
    got = build_account_performance_summary(df, "Costa Rica", "FacebookMKTOBFLW27 CR Ait")
    assert got["has_data"] is True, "слепота к хвостам имени перестала работать"


def test_the_row_can_say_the_data_sits_under_another_country():
    tpl = (
        Path(__file__).resolve().parent.parent
        / "src" / "web" / "templates" / "_account_map_country.html"
    ).read_text(encoding="utf-8")
    assert "r.elsewhere" in tpl, (
        "«строк нет» и «строки под другой страной» снова выглядят одинаково"
    )
