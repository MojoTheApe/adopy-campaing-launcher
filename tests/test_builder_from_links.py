"""Билдер берёт аккаунты у связей со странами, а не у книги статистики.

Аккаунт, подключённый на дешборде и в книге отсутствующий, был виден в карте
страны — со спендом, статусом и кнопкой Read — но в Campaign Builder не
появлялся вовсе. Собрать на нём кампанию было негде, сколько ни настраивай.

Ровно этот случай — новый тестовый аккаунт: в рабочей книге статистики его нет
и не должно быть.

Билдер отвечает на «что запускаем», а не на «что попало в отчёт». Источник —
связи аккаунтов со странами, то самое хранилище, которое уже умеет держать один
аккаунт в нескольких странах.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import account_links  # noqa: E402
from src.web.app import _add_linked_accounts_to_builder  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Своё хранилище связей, а не машины разработчика.

    Подменить путь хранения мало: `seed_from_account_map` читает карту
    аккаунтов машины и заносит из неё страны. На машине разработчика их
    одиннадцать, и проверка «в пустом отчёте стран нет» падала на них, а не на
    коде. Хуже была бы обратная сторона — зелёная проверка на чужих данных.
    """
    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(account_links, "seed_from_account_map", lambda: 0)
    return tmp_path


def _report(*countries):
    return {"countries": [{"country": c, "open_recs": dict(r)} for c, r in countries]}


def test_an_account_absent_from_the_workbook_still_gets_a_builder():
    """Тот самый случай: подключён здесь, в отчёте его нет."""
    account_links.link("FacebookDQ438", "country", "Saudi Arabia")
    report = _report(("Saudi Arabia", {}))
    assert _add_linked_accounts_to_builder(report) == 1
    assert list(report["countries"][0]["open_recs"]) == ["FacebookDQ438"]


def test_it_arrives_without_recommendations():
    """Рекомендации считают по числам, которых нет.

    Пустой список — это «карточек нет, собирайте вручную», и билдер такую
    ситуацию уже умеет показывать.
    """
    account_links.link("FacebookDQ438", "country", "Saudi Arabia")
    report = _report(("Saudi Arabia", {}))
    _add_linked_accounts_to_builder(report)
    assert report["countries"][0]["open_recs"]["FacebookDQ438"] == []


def test_accounts_already_in_the_report_keep_their_recommendations():
    """Добавляем недостающее, а не переписываем то, что посчитано."""
    account_links.link("KLD_BFL_X84", "country", "Saudi Arabia")
    report = _report(("Saudi Arabia", {"KLD_BFL_X84": [{"strategy_type": "scale"}]}))
    assert _add_linked_accounts_to_builder(report) == 0
    assert report["countries"][0]["open_recs"]["KLD_BFL_X84"] == [{"strategy_type": "scale"}]


def test_a_country_stored_as_a_code_finds_its_country():
    """В связях страна записана так, как её назвали при подключении.

    Иногда полным именем, иногда кодом. Смотреть надо оба написания, иначе
    привязка к «SA» не найдёт «Saudi Arabia» — и аккаунт снова пропадёт.
    """
    account_links.link("FacebookElf191", "country", "SA")
    report = _report(("Saudi Arabia", {}))
    assert _add_linked_accounts_to_builder(report) == 1
    assert "FacebookElf191" in report["countries"][0]["open_recs"]


def test_one_account_reaches_the_builder_of_every_country_it_is_linked_to():
    """Хранилище связей давно умеет держать аккаунт в нескольких странах.

    До сих пор этим не пользовались: в билдере он был бы виден только там, где
    его знает книга.
    """
    account_links.link("FacebookUL869", "country", "Chile")
    account_links.link("FacebookUL869", "country", "Peru")
    report = _report(("Chile", {}), ("Peru", {}))
    assert _add_linked_accounts_to_builder(report) == 2
    assert "FacebookUL869" in report["countries"][0]["open_recs"]
    assert "FacebookUL869" in report["countries"][1]["open_recs"]


def test_an_account_is_not_added_twice_under_a_different_spelling():
    """Связь может быть записана и полным именем страны, и кодом."""
    account_links.link("FacebookDQ438", "country", "Saudi Arabia")
    account_links.link("FacebookDQ438", "country", "SA")
    report = _report(("Saudi Arabia", {}))
    assert _add_linked_accounts_to_builder(report) == 1
    assert list(report["countries"][0]["open_recs"]) == ["FacebookDQ438"]


def test_a_country_with_no_links_is_left_alone():
    account_links.link("FacebookDQ438", "country", "Peru")
    report = _report(("Saudi Arabia", {"KLD_BFL_X84": []}))
    assert _add_linked_accounts_to_builder(report) == 1  # Peru gets its own section.
    assert list(report["countries"][0]["open_recs"]) == ["KLD_BFL_X84"]
    assert report["countries"][1]["open_recs"] == {"FacebookDQ438": []}


def test_a_report_without_countries_gets_them_from_the_links():
    """Книга отстала — стран в отчёте нет вовсе.

    Раньше здесь ожидался ноль: функция только раскладывала аккаунты по уже
    существующим странам, а пустой отчёт оставался пустым. С тех пор она сама
    заводит стране пустую секцию, и ожидание нуля стало неверным — тест был
    красным и до этой ветки.

    Проверять надо то, ради чего правка делалась: страна с подключённым
    аккаунтом на странице есть, а отчёт без единой связи по-прежнему не падает.
    """
    account_links.link("FacebookDQ438", "country", "Saudi Arabia")
    report: dict = {"countries": []}
    assert _add_linked_accounts_to_builder(report) == 1
    assert [c["country"] for c in report["countries"]] == ["Saudi Arabia"]


def test_a_report_without_countries_and_without_links_does_not_break():
    empty: dict = {}
    assert _add_linked_accounts_to_builder(empty) == 0
    assert empty.get("countries") == []


def test_a_country_without_open_recs_at_all_gets_the_key():
    """У страны, которой книга не знает, ключа может не быть вовсе."""
    account_links.link("FacebookDQ438", "country", "Saudi Arabia")
    report = {"countries": [{"country": "Saudi Arabia"}]}
    assert _add_linked_accounts_to_builder(report) == 1
    assert list(report["countries"][0]["open_recs"]) == ["FacebookDQ438"]


def test_the_helper_is_actually_called_when_the_page_is_built():
    """Проверки выше зовут функцию напрямую — они не заметят, что её не зовут.

    Именно так и вышло на первом заходе: вызов убрали, все девять остались
    зелёными. Функция, которую никто не вызывает, — это не починка.
    """
    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    render = source[source.index("def _render_report"):]
    render = render[: render.index("\n@app.")]
    assert "_add_linked_accounts_to_builder(report)" in render, (
        "билдер снова строится только по книге — вызов потерялся"
    )
    # Список аккаунтов для проверки готовности собирается ниже по функции:
    # добавить аккаунт после него значило бы дать ему строку в билдере без
    # проверки конфигурации.
    assert render.index("_add_linked_accounts_to_builder(report)") < render.index("all_account_labels"), (
        "аккаунты добавляются после сбора списка — они останутся без preflight"
    )
