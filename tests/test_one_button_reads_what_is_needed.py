# -*- coding: utf-8 -*-
"""Одна кнопка читает спенд и, только когда нужно, страны (ACL-1282).

Кнопок было две. «Read spend» спрашивал у аккаунта сумму, «Countries» —
куда она ушла; обе открывали один и тот же кабинет по отдельности. Владелец
свёл их в одну, и вся задача свелась к одному вопросу: **когда за спендом
идти ещё и за странами**.

Цена вопроса несимметричная. Спенд — это прочитанная страница. Разбивка
создаёт внутри кабинета отчёт, выставляет в нём период и разрезы и читает
сводную таблицу: дольше, заметнее для Meta и оставляет за собой сущность.
Поэтому «читать всегда» — не безобидное решение, а «не читать» молча — хуже
вдвойне: на экране это выглядит как отсутствие данных, а не как выбор.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.analysis_agent.country_split import why_split_is_needed

OK = {"state": "ok", "finished_at": "2026-09-22 12:00:00"}


def test_one_country_and_a_fresh_split_asks_the_account_for_nothing():
    """Главный случай: аккаунт в одной стране, разбивка свежая — отчёта нет."""
    assert why_split_is_needed(
        countries=1, split=OK, spend_finished_at="2026-09-22 11:00:00") == ""


def test_more_than_one_country_always_needs_the_split():
    """Сумма по аккаунту не говорит, куда ушли деньги, — даже свежая."""
    why = why_split_is_needed(
        countries=2, split=OK, spend_finished_at="2026-09-22 11:00:00")
    assert why and "more than one country" in why


def test_never_read_means_read_now():
    assert "no country split" in why_split_is_needed(
        countries=1, split=None, spend_finished_at="2026-09-22 11:00:00")


def test_a_failed_split_is_not_a_split():
    """Отказ — это «не знаем», а не ответ, который можно считать свежим."""
    assert why_split_is_needed(
        countries=1, split={"state": "failed", "error": "profile is closed",
                            "finished_at": "2026-09-22 13:00:00"},
        spend_finished_at="2026-09-22 11:00:00")


def test_a_running_split_is_not_a_split():
    assert why_split_is_needed(
        countries=1, split={"state": "running"}, spend_finished_at="")


def test_an_empty_report_is_an_answer():
    """«За период ничего не крутилось» — это прочитано, а не пусто."""
    assert why_split_is_needed(
        countries=1, split={"state": "empty", "rows": [],
                            "finished_at": "2026-09-22 12:00:00"},
        spend_finished_at="2026-09-22 11:00:00") == ""


def test_a_split_older_than_the_spend_is_read_again():
    """Разбивку снимали с других чисел — значит она отвечает про другое."""
    assert "older than the spend" in why_split_is_needed(
        countries=1, split=OK, spend_finished_at="2026-09-22 18:00:00")


def test_a_split_without_a_time_of_its_own_is_read_again():
    """Записи до ACL-1282 времени не помнят.

    «Свежая, раз лежит» — самое дорогое из возможных предположений: экран
    показывал бы разбивку недельной давности рядом со свежим спендом и
    выглядел бы согласованным.
    """
    assert "does not say when" in why_split_is_needed(
        countries=1, split={"state": "ok", "rows": []},
        spend_finished_at="2026-09-22 11:00:00")


def test_a_never_read_account_does_not_need_a_reason_from_the_spend():
    """Спенда за эти даты ещё нет — сравнивать не с чем, и это не повод
    объявлять разбивку устаревшей: она просто есть."""
    assert why_split_is_needed(countries=1, split=OK, spend_finished_at="") == ""


def test_the_combined_reading_settles_instead_of_repeating_itself():
    """Свойство, ради которого разбивка идёт второй, а не первой.

    Одна кнопка читает спенд, потом страны — значит время разбивки всегда
    новее. Следующее нажатие по тем же датам видит свежую разбивку и
    аккаунт вторым отчётом не беспокоит. Иначе правило «старше спенда»
    заводило бы отчёт при каждом нажатии.
    """
    spend_at = "2026-09-22 12:00:00"
    split_at = "2026-09-22 12:04:00"  # разбивка сняла своё время позже
    assert why_split_is_needed(
        countries=1, split={"state": "ok", "finished_at": split_at},
        spend_finished_at=spend_at) == ""


def test_the_split_remembers_when_it_was_read():
    """Без своего времени разбивку не с чем сравнивать."""
    from src.analysis_agent.country_split import CountrySplit

    assert "started_at" in CountrySplit.__dataclass_fields__
    assert "finished_at" in CountrySplit.__dataclass_fields__


def test_a_stored_split_from_an_older_version_still_loads():
    """В хранилище лежат записи без новых полей: терять их нельзя."""
    from src.analysis_agent.country_split import _as_split

    split = _as_split({"account": "Doc19", "date_from": "2026-09-22",
                       "date_to": "2026-09-22", "state": "ok",
                       "was_a_field_once": "gone"})
    assert split.account == "Doc19" and split.finished_at == ""


# ── Решение принимает сервер, а не экран ──────────────────────────────────
#
# У клиента нет ни привязок аккаунта к странам, ни обоих хранилищ. Держать
# это правило на странице значило бы держать его в двух местах: строка и
# «Read all» спрашивают одно и то же и обязаны получать один ответ.

import pytest  # noqa: E402

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import country_split as cs  # noqa: E402
from src.web import account_links as links  # noqa: E402
from src.web import app  # noqa: E402

DAY = "2026-09-22"


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))

    def put_spend(account, finished, state="ok"):
        sp._put(sp.SpendReading(account=account, date_from=DAY, date_to=DAY, spend=10.0,
                                currency="$", state=state, finished_at=finished))

    def put_split(account, finished, state="ok"):
        cs.save_split(cs.CountrySplit(account=account, date_from=DAY, date_to=DAY,
                                      state=state, rows=[], finished_at=finished))
    return put_spend, put_split


def test_a_single_country_account_with_a_fresh_split_is_not_asked_again(stores):
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_spend("Doc19", "2026-09-22 12:00:00")
    put_split("Doc19", "2026-09-22 12:04:00")
    assert app._split_plan("Doc19", DAY, DAY) == {"needed": False, "why": ""}


def test_an_account_in_two_countries_is_always_asked(stores):
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    links.link("Doc19", "country", "Kuwait")
    put_spend("Doc19", "2026-09-22 12:00:00")
    put_split("Doc19", "2026-09-22 12:04:00")
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is True


def test_a_split_older_than_the_spend_is_asked_again(stores):
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_split("Doc19", "2026-09-21 09:00:00")
    put_spend("Doc19", "2026-09-22 12:00:00")
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is True


def test_the_decision_is_made_before_the_spend_reading_replaces_the_old_one(stores):
    """Порядок здесь и есть правило.

    `start_read` кладёт на место прошлого показания запись «читаю» — без
    времени. Спросить после запуска значит спросить у записи, которая ещё
    ничего не знает: разбивка навсегда останется «свежей», и страны не
    прочитаются больше никогда. Поэтому решение принимают до запуска, и
    порядок этих двух строк в маршруте — не стиль.
    """
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_spend("Doc19", "2026-09-22 18:00:00")
    put_split("Doc19", "2026-09-21 09:00:00")   # разбивка старше спенда
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is True

    # Ровно то, что делает start_read в первой же строке.
    sp._put(sp.SpendReading(account="Doc19", date_from=DAY, date_to=DAY,
                            state="running", started_at="2026-09-22 19:00:00"))
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is False, (
        "спрошенное после запуска решение видит запись без времени — "
        "именно поэтому его принимают до"
    )

    route = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    body = route[route.index("async def account_spend_read("):]
    body = body[:body.index("@app.post")]
    assert body.index("plan = _split_plan(") < body.index("account_spend.start_read("), (
        "решение о странах считается после запуска чтения — тогда оно всегда «не нужно»"
    )


def test_a_question_without_an_answer_reads_rather_than_stays_silent(monkeypatch):
    """Поломка здесь отвечает «нужна»: молча пропущенная разбивка выглядит на
    экране как «страны не читаются вовсе»."""
    monkeypatch.setattr(links, "values_for", lambda *a, **k: (_ for _ in ()).throw(OSError("store is gone")))
    plan = app._split_plan("Doc19", DAY, DAY)
    assert plan["needed"] is True and plan["why"]
