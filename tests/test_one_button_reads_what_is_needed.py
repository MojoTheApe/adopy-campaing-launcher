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
from types import SimpleNamespace
from tests.profile_lease_fakes import fake_profile

ROOT = Path(__file__).resolve().parents[1]

from src.analysis_agent.country_split import why_split_is_needed

#: Разбивка, снятая с показания спенда «$10.00, прочитано в 12:00».
OK = {"state": "ok", "finished_at": "2026-09-22 12:04:00",
      "spend_at": "2026-09-22 12:00:00", "spend": 10.0, "spend_currency": "$"}
SPEND = {"spend_finished_at": "2026-09-22 12:00:00", "spend": 10.0, "currency": "$"}


def test_one_country_and_a_fresh_split_asks_the_account_for_nothing():
    """Главный случай: аккаунт в одной стране, разбивка свежая — отчёта нет."""
    assert why_split_is_needed(countries=1, split=OK, **SPEND) == ""


def test_more_than_one_country_matters_when_there_is_nothing_to_compare():
    """Спенда за эти даты нет: сумма ничего не говорит, страны берём сразу.

    Ревью 23.09: «в нескольких странах — значит читать всегда» выглядело
    безобидно и означало отчёт в кабинете при каждом нажатии «Read all»,
    вечно. Разбивка устаревает вместе с числом, которое объясняет, а не сама
    по себе; число стран решает там, где сравнить не с чем.
    """
    assert "more than one country" in why_split_is_needed(
        countries=2, split=OK, spend_finished_at="")
    assert why_split_is_needed(countries=1, split=OK, spend_finished_at="") == ""


def test_the_same_spend_is_not_read_twice_even_in_several_countries():
    """Сумма та же — ответ «куда она ушла» тот же, и отчёт строить незачем."""
    assert why_split_is_needed(countries=3, split=OK, **SPEND) == ""


def test_a_changed_spend_is_read_again():
    """Деньги двинулись — старая разбивка объясняет другое число."""
    why = why_split_is_needed(countries=1, split=OK,
                              spend_finished_at="2026-09-22 18:00:00",
                              spend=41.5, currency="$")
    assert "spend has changed" in why


def test_the_same_number_in_another_currency_is_another_number():
    assert "spend has changed" in why_split_is_needed(
        countries=1, split=OK, spend_finished_at="2026-09-22 18:00:00",
        spend=10.0, currency="KWD")


def test_never_read_means_read_now():
    assert "no country split" in why_split_is_needed(
        countries=1, split=None, **SPEND)


def test_a_failed_split_is_not_a_split():
    """Отказ — это «не знаем», а не ответ, который можно считать свежим."""
    assert why_split_is_needed(
        countries=1, split={"state": "failed", "error": "profile is closed",
                            "finished_at": "2026-09-22 13:00:00"}, **SPEND)


def test_a_running_split_is_not_a_split():
    assert why_split_is_needed(countries=1, split={"state": "running"}, **SPEND)


def test_an_empty_report_is_an_answer():
    """«За период ничего не крутилось» — это прочитано, а не пусто."""
    assert why_split_is_needed(
        countries=1, split={"state": "empty", "rows": [],
                            "finished_at": "2026-09-22 12:04:00",
                            "spend_at": "2026-09-22 12:00:00", "spend": 10.0,
                            "spend_currency": "$"}, **SPEND) == ""


def test_an_empty_report_is_read_again_once_the_money_appears():
    """«Ничего не крутилось» перестаёт быть ответом, как только появилась сумма."""
    empty = {"state": "empty", "rows": [], "finished_at": "2026-09-22 09:00:00",
             "spend_at": "2026-09-22 08:00:00", "spend": 0.0, "spend_currency": "$"}
    assert "spend has changed" in why_split_is_needed(
        countries=1, split=empty, **SPEND)


def test_re_reading_the_same_spend_does_not_stale_the_split():
    """Главная поломка первой версии ACL-1282 (ревью 23.09).

    Сравнение по часам не успокаивалось никогда: нажатие, которое разбивку не
    читало, всё равно обновляло время спенда, и следующее объявляло разбивку
    устаревшей. Отчёт в кабинете строился через раз — ровно та цена, ради
    которой задача и затевалась. Перечитанный спенд с той же суммой ничего
    не меняет.
    """
    assert why_split_is_needed(
        countries=1, split=OK, spend_finished_at="2026-09-22 23:59:00",
        spend=10.0, currency="$") == ""


def test_a_split_without_a_time_of_its_own_is_read_again():
    """Записи до ACL-1282 времени не помнят.

    «Свежая, раз лежит» — самое дорогое из возможных предположений: экран
    показывал бы разбивку недельной давности рядом со свежим спендом и
    выглядел бы согласованным.
    """
    assert "does not say which spend" in why_split_is_needed(
        countries=1, split={"state": "ok", "rows": [],
                            "finished_at": "2026-09-22 12:04:00"}, **SPEND)


def test_the_split_remembers_which_spend_it_explains():
    """Без этого разбивку не с чем сравнивать, кроме часов, — а часы врут."""
    from src.analysis_agent.country_split import CountrySplit

    for field in ("spend_at", "spend", "spend_currency"):
        assert field in CountrySplit.__dataclass_fields__


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

import time  # noqa: E402

import pytest  # noqa: E402

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import country_split as cs  # noqa: E402
from src.web import account_links as links  # noqa: E402
from src.web import app  # noqa: E402

DAY = "2026-09-22"


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_entry_for", lambda account: SimpleNamespace(profile_id=fake_profile(account)))
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))

    def put_spend(account, finished, state="ok", spend=10.0):
        sp._put(sp.SpendReading(account=account, date_from=DAY, date_to=DAY, spend=spend,
                                currency="$", state=state, finished_at=finished))

    def put_split(account, finished, state="ok"):
        """Разбивка запоминает спенд ровно так же, как это делает start_split."""
        cs.save_split(cs.CountrySplit(account=account, date_from=DAY, date_to=DAY,
                                      state=state, rows=[], finished_at=finished,
                                      **cs._spend_now(account, DAY, DAY)))
    return put_spend, put_split


def test_a_single_country_account_with_a_fresh_split_is_not_asked_again(stores):
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_spend("Doc19", "2026-09-22 12:00:00")
    put_split("Doc19", "2026-09-22 12:04:00")
    assert app._split_plan("Doc19", DAY, DAY) == {"needed": False, "why": ""}


def test_an_account_in_two_countries_is_asked_before_its_first_spend(stores):
    """Сравнить не с чем — и общая сумма всё равно не сказала бы, куда ушло."""
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    links.link("Doc19", "country", "Kuwait")
    put_split("Doc19", "2026-09-22 12:04:00")   # спенда за эти даты ещё нет
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is True


def test_a_split_older_than_the_spend_is_asked_again(stores):
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_split("Doc19", "2026-09-21 09:00:00")
    put_spend("Doc19", "2026-09-22 12:00:00")
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is True


def test_the_decision_waits_for_the_reading_to_finish(stores):
    """ACL-1288. Решение сравнивает суммы, и спрашивать его надо у
    дочитанного показания.

    Прежняя версия спрашивала на запуске чтения и сравнивала с прошлой
    суммой: нажатие, на котором деньги как раз изменились, страны не
    перечитывало — это делало только следующее. На закрытых датах незаметно,
    на сегодняшних экран показывал свежий спенд рядом с разбивкой от
    старого и выглядел согласованным.

    Пока чтение идёт, решать не по чему: у записи «читаю» суммы нет.
    """
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_spend("Doc19", "2026-09-22 12:00:00", spend=10.0)
    put_split("Doc19", "2026-09-22 12:04:00")   # разбивка объясняет $10.00

    # Чтение идёт: суммы нет, сравнивать не с чем.
    sp._put(sp.SpendReading(account="Doc19", date_from=DAY, date_to=DAY,
                            state="running", started_at="2026-09-22 18:00:00"))
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is False

    # Чтение закончилось, и денег стало больше — вот теперь страны нужны.
    put_spend("Doc19", "2026-09-22 18:02:00", spend=41.5)
    plan = app._split_plan("Doc19", DAY, DAY)
    assert plan["needed"] is True and "spend has changed" in plan["why"]

    route = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    status = route[route.index('@app.get("/account-spend/status")'):]
    status = status[:status.index("@app.get", 10)]
    assert 'if reading.state == "ok":' in status and '_split_plan(' in status, (
        "опрос состояния не решает про страны — значит решение принимают "
        "где-то ещё, до того как сумма известна"
    )


def test_a_question_without_an_answer_reads_rather_than_stays_silent(monkeypatch):
    """Поломка здесь отвечает «нужна»: молча пропущенная разбивка выглядит на
    экране как «страны не читаются вовсе»."""
    monkeypatch.setattr(links, "values_for", lambda *a, **k: (_ for _ in ()).throw(OSError("store is gone")))
    plan = app._split_plan("Doc19", DAY, DAY)
    assert plan["needed"] is True and plan["why"]


def test_pressing_the_button_again_and_again_builds_one_report(stores):
    """Свойство целиком, на настоящих хранилищах: шесть нажатий — один отчёт.

    Ревьюер воспроизвёл здесь три отчёта на шесть нажатий: чтение спенда само
    обновляло время, с которым сравнивали разбивку. Считаем нажатия так же,
    как их делает маршрут: решение — до чтения, разбивка — после него.
    """
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    reports = 0
    for press in range(6):
        plan = app._split_plan("Doc19", DAY, DAY)
        # Спенд читается всегда и каждый раз обновляет своё время.
        put_spend("Doc19", f"2026-09-22 1{press}:00:00")
        if plan["needed"]:
            reports += 1
            put_split("Doc19", f"2026-09-22 1{press}:04:00")
    assert reports == 1, f"кабинет построил {reports} отчётов вместо одного"


def test_a_multi_country_account_is_not_asked_again_for_the_same_money(stores):
    """Тот же счёт для аккаунта в двух странах: «Read all» не строит отчёт
    каждый прогон. Ревью 23.09: пять из четырнадцати аккаунтов стоят под
    двумя странами, и это было бы пять отчётов при каждом нажатии."""
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    links.link("Doc19", "country", "Kuwait")
    reports = 0
    for press in range(4):
        plan = app._split_plan("Doc19", DAY, DAY)
        put_spend("Doc19", f"2026-09-22 1{press}:00:00")
        if plan["needed"]:
            reports += 1
            put_split("Doc19", f"2026-09-22 1{press}:04:00")
    assert reports == 1, f"кабинет построил {reports} отчётов вместо одного"


def test_money_that_moved_brings_the_report_back(stores):
    """Обратная сторона: пока сумма меняется, страны перечитываются."""
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_spend("Doc19", "2026-09-22 12:00:00")
    put_split("Doc19", "2026-09-22 12:04:00")
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is False
    put_spend("Doc19", "2026-09-22 13:00:00", spend=41.5)
    plan = app._split_plan("Doc19", DAY, DAY)
    assert plan["needed"] is True and "spend has changed" in plan["why"]


def test_two_spellings_of_one_country_are_one_country(stores):
    """«KW» и «Kuwait» карта показывает одной страной. Считать их двумя
    значило бы навсегда записать аккаунт в многострановые — и строить отчёт
    при каждом нажатии."""
    put_spend, put_split = stores
    links.link("Doc19", "country", "Kuwait")
    links.link("Doc19", "country", "kuwait")
    put_spend("Doc19", "2026-09-22 12:00:00")
    put_split("Doc19", "2026-09-22 12:04:00")
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is False


def test_the_press_that_changes_the_money_reads_the_countries(stores):
    """Само отставание, целиком, на настоящих хранилищах (ACL-1288).

    Считаем нажатия так же, как их делает дешборд: чтение спенда, затем —
    по его результату — решение о странах. Пока деньги не менялись, отчётов
    нет; как только сумма другая, страны читаются **на этом же** нажатии, а
    не через одно.
    """
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    money = [10.0, 10.0, 10.0, 41.5, 41.5]
    read_on = []
    for press, amount in enumerate(money):
        put_spend("Doc19", f"2026-09-22 1{press}:00:00", spend=amount)
        if app._split_plan("Doc19", DAY, DAY)["needed"]:
            read_on.append(press)
            put_split("Doc19", f"2026-09-22 1{press}:04:00")
    assert read_on == [0, 3], (
        f"страны прочитаны на нажатиях {read_on}: 0 — первое чтение, "
        "3 — то, на котором деньги изменились"
    )


def test_a_reading_that_failed_does_not_decide_anything(stores):
    """У неудачи суммы нет, и «разбивка свежая» из неё не следует."""
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_spend("Doc19", "2026-09-22 12:00:00", spend=10.0)
    put_split("Doc19", "2026-09-22 12:04:00")
    put_spend("Doc19", "2026-09-22 13:00:00", spend=None, state="failed")
    assert app._split_plan("Doc19", DAY, DAY)["needed"] is False


# ── Через сами маршруты, а не мимо них ────────────────────────────────────
#
# Ревью 23.09: тест выше пишет показание и тут же зовёт `_split_plan` сам,
# поэтому он останется зелёным, даже если решение вернуть обратно в момент
# запуска чтения — то есть ровно к той ошибке, ради которой он написан.
# Здесь нажатие проходит теми же двумя маршрутами, что и с экрана.

import asyncio  # noqa: E402

import httpx  # noqa: E402


def _press(reader):
    """Нажатие «Read spend»: запуск чтения и опрос, как их делает страница."""
    async def run():
        transport = httpx.ASGITransport(app=app.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            started = await client.post("/account-spend/read", data={
                "account": "Doc19", "date_from": DAY, "date_to": DAY, "span": ""})
            # Экран опрашивает состояние, пока чтение не кончится; здесь то же
            # самое, только без трёхсекундных пауз.
            for _ in range(500):
                status = await client.get("/account-spend/status", params={
                    "account": "Doc19", "date_from": DAY, "date_to": DAY})
                if (status.json().get("reading") or {}).get("state") != "running":
                    break
                time.sleep(0.01)
            return started.json(), status.json()

    sp._RUNNING.clear()
    with_reader = sp.start_read
    try:
        sp.start_read = lambda a, f, t: with_reader(a, f, t, reader=reader)
        return asyncio.run(run())
    finally:
        sp.start_read = with_reader


def _reads(amount):
    def reader(account, date_from, date_to):
        return sp.SpendReading(account=account, date_from=date_from, date_to=date_to,
                               state="ok", spend=amount, currency="$")
    return reader


def test_the_routes_decide_about_countries_only_when_the_reading_is_done(stores):
    """Запуск чтения про страны не отвечает: сравнивать ещё не с чем."""
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    started, _ = _press(_reads(10.0))
    assert started["ok"] and "split" not in started and "plan" not in started, (
        "решение вернулось в маршрут запуска — значит снова сравнивает "
        "прошлую сумму, а не ту, что увидит человек"
    )


def test_the_status_route_asks_for_countries_on_the_press_that_changed_the_money(stores):
    """То же отставание, но целиком через маршруты (ACL-1288)."""
    put_spend, put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")

    def press(amount):
        _started, status = _press(_reads(amount))
        plan = status.get("plan") or {}
        if plan.get("needed"):
            put_split("Doc19", sp._now())
        return bool(plan.get("needed"))

    asked = [press(a) for a in (10.0, 10.0, 41.5, 41.5)]
    assert asked == [True, False, True, False], (
        f"страны спрошены на {asked}: первое чтение и то, где сумма другая"
    )


def test_the_row_says_which_spend_its_country_split_explains():
    """Критерий из тикета: по экрану должно быть видно, к какому числу
    относится пострановая разбивка. «В стране столько-то» рядом с другой
    суммой — две правды про разные деньги."""
    row = (ROOT / "src" / "web" / "templates" / "_account_map_country.html").read_text(encoding="utf-8")
    cell = row[row.index('class="amap-live-ok"'):]
    cell = cell[:cell.index("</span>")]
    assert "countries" in cell and "sp.spend" in cell, (
        "подсказка не говорит, какую сумму объясняет разбивка"
    )
    assert "not the" in cell, (
        "расхождение разбивки со спендом ничем не отмечено"
    )


def test_a_never_split_account_is_not_read_from_disk_twice(stores):
    """«Не передали» и «передали пустое» — разные вещи.

    У аккаунта, которого по странам ещё не читали, разбивка равна None.
    Принять это за «возьми из хранилища» значит читать диск лишний раз на
    каждом опросе состояния — а он ходит каждые три секунды всё то время,
    пока в кабинете строится отчёт.
    """
    put_spend, _put_split = stores
    links.link("Doc19", "country", "Saudi Arabia")
    put_spend("Doc19", "2026-09-22 12:00:00")
    reads = []
    real = cs.get_split
    try:
        cs.get_split = lambda *a, **k: (reads.append(a), real(*a, **k))[1]
        plan = app._split_plan("Doc19", DAY, DAY,
                               reading=sp.get_reading("Doc19", DAY, DAY), split=None)
    finally:
        cs.get_split = real
    assert plan["needed"] is True and not reads, "хранилище перечитали зря"


# ── Серверная половина подписи: её видно только отрисовкой ────────────────
#
# Ревью 23.09: проверка «в шаблоне есть слова `split_txt != spend_txt`»
# зеленела и тогда, когда сравнение возвращали к числам. А с числами аккаунт
# с 41.502 против 41.50 получал бы при загрузке страницы «not the $41.50
# above» — правило перечитывания и клиент считают это одними деньгами, и
# экран обвинял бы себя в отставании, которого нет.

def _render_row(*, spend, split_spend, currency="$", split_currency="$",
                date_from="2026-09-01", date_to="2026-09-22"):
    """Строка аккаунта, отрисованная настоящим шаблоном."""
    from jinja2 import Environment, FileSystemLoader

    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(ROOT / "src" / "web" / "templates")),
                      autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    account = "Doc19"
    reading = {"state": "ok", "spend": spend, "currency": currency,
               "date_from": date_from, "date_to": date_to,
               "finished_at": "2026-09-22 18:00:00", "how": "rpa"}
    split = {"state": "ok", "spend": split_spend, "spend_currency": split_currency}
    country = {"country": "Saudi Arabia", "key": "SA", "virtual": False,
               "rows": [{"account": account, "has_data": False, "ready": True,
                         "in_workbook": True, "status": {}, "to_do": ""}]}
    return env.get_template("_account_map_country.html").render(
        mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [],
              "live": {account: reading}, "splits": {account: split},
              "fallback": {}, "dates": [], "date_from": date_from,
              "date_to": date_to, "span": ""})


def test_the_server_calls_half_a_cent_the_same_money_too():
    """Та же мера, что у правила и у клиента: до половины копейки."""
    html = _render_row(spend=41.50, split_spend=41.502)
    assert "countries read for $41.50" in html
    assert "not the" not in html, "сервер обвиняет разбивку в отставании, которого нет"


def test_the_server_says_when_the_split_explains_other_money():
    html = _render_row(spend=41.50, split_spend=10.0)
    assert "countries read for $10.00, not the $41.50 above" in html


def test_the_server_says_when_the_split_does_not_remember_its_spend():
    """Записи до ACL-1282 суммы не помнят, и молчать о них — значит выдать их
    за свежие. Эту ветку шаблона не проверял никто (ревью 23.09)."""
    assert "countries read before this spend was known" in _render_row(
        spend=41.50, split_spend=None)


def test_the_server_calls_another_currency_another_number():
    html = _render_row(spend=41.50, split_spend=41.50, split_currency="EUR")
    assert "countries read for EUR 41.50, not the $41.50 above" in html


def test_the_server_writes_one_day_as_one_day():
    """Клиент прибит к этому виду; сервер обязан держаться того же, иначе
    подсказка мигает при первой же перерисовке — только зеркально."""
    html = _render_row(spend=41.50, split_spend=41.50,
                       date_from="2026-09-01", date_to="2026-09-01")
    assert "2026-09-01 · read" in html and "2026-09-01…2026-09-01" not in html


def test_the_server_and_the_client_write_the_same_sentence():
    """Две половины одной подписи: разойдутся — человек увидит, как текст
    меняется сам собой при первом же опросе. Каждая фраза проверяется на
    обеих сторонах, без исключений: исключение прятало ровно ту ветку,
    которую никто не проверял (ревью 23.09)."""
    page = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    note = page[page.index("function amapSplitNote("):]
    note = note[:note.index(chr(10) + "}" + chr(10))]
    rendered = (_render_row(spend=41.50, split_spend=10.0)
                + _render_row(spend=41.50, split_spend=None))
    for phrase in ("countries read for ", ", not the ", " above",
                   "countries read before this spend was known"):
        assert phrase in note, f"клиент не пишет «{phrase}»"
        assert phrase in rendered, f"сервер не пишет «{phrase}»"
