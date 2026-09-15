# -*- coding: utf-8 -*-
"""Сведение спенда по странам и то, как его отдаёт дешборд (acl-357).

Главное правило, ради которого задача и существует: **страны берутся из
показаний, а не из карты аккаунтов.** Деньги нередко уходят на страну, к
которой аккаунт не привязан — 15.09 живое чтение DQ501 показало Chile на
аккаунте, где весь вечер запускали кампании на Гватемалу, — и показывать их
под привязанной страной значит показывать неправду.

Через `TestClient` не ходим: в этой сборке `httpx` с ним несовместим, и
соседние проверки его тоже не используют. Зовём обработчик напрямую.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import country_split as cs  # noqa: E402
from src.web import app as web  # noqa: E402

DAY_FROM, DAY_TO = "2026-08-16", "2026-09-15"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    return tmp_path


def _save(account, rows, state="ok", date_from=DAY_FROM, date_to=DAY_TO):
    cs.save_split(cs.CountrySplit(
        account=account, date_from=date_from, date_to=date_to,
        state=state, rows=rows,
    ))


# ── сведение ──────────────────────────────────────────────────────────────

def test_spend_is_summed_across_accounts_by_country():
    """Валюта названа кодом — складывать поперёк кабинетов безопасно."""
    _save("A", [{"country": "Chile", "spend": 44.17, "currency": "CLP"}])
    _save("B", [{"country": "Chile", "spend": 5.83, "currency": "CLP"},
                {"country": "Peru", "spend": 10.0, "currency": "CLP"}])
    totals = cs.totals_by_country(DAY_FROM, DAY_TO)
    assert [(t["country"], t["spend"]) for t in totals] == [("Chile", 50.0), ("Peru", 10.0)]
    assert totals[0]["accounts"] == ["A", "B"]


def test_a_country_absent_from_the_account_map_still_appears():
    """Ради этого задача и заведена: спенд ушёл не на привязанную страну.

    Сведение не заглядывает в карту аккаунтов вовсе — значит «неизвестная»
    страна появляется сама, а не как особый случай, который можно забыть.
    """
    _save("FacebookDQ501", [{"country": "Chile", "spend": 44.17, "currency": ""}])
    assert [t["country"] for t in cs.totals_by_country(DAY_FROM, DAY_TO)] == ["Chile"]


def test_currencies_are_never_added_together():
    """Шекели и доллары — две строки, а не одна выдуманная сумма."""
    _save("A", [{"country": "Israel", "spend": 100.0, "currency": "ILS"}])
    _save("B", [{"country": "Israel", "spend": 20.0, "currency": "USD"}])
    totals = cs.totals_by_country(DAY_FROM, DAY_TO)
    assert {(t["currency"], t["spend"]) for t in totals} == {("ILS", 100.0), ("USD", 20.0)}


def test_an_unknown_currency_is_its_own_bucket():
    """«$» ставят и за AUD, и за MXN — пустая валюта не то же, что USD."""
    _save("A", [{"country": "Peru", "spend": 5.0, "currency": ""}])
    _save("B", [{"country": "Peru", "spend": 7.0, "currency": "USD"}])
    assert len(cs.totals_by_country(DAY_FROM, DAY_TO)) == 2


def test_failed_readings_do_not_pretend_to_be_zero():
    """Отказ — это «не знаем». Подмешать его нулём значит выдать неполное за полное."""
    _save("A", [{"country": "Chile", "spend": 44.17, "currency": ""}])
    _save("B", [], state="failed")
    totals = cs.totals_by_country(DAY_FROM, DAY_TO)
    assert len(totals) == 1 and totals[0]["accounts"] == ["A"]


def test_another_period_is_not_mixed_in():
    _save("A", [{"country": "Chile", "spend": 44.17, "currency": ""}])
    _save("A", [{"country": "Peru", "spend": 1.0, "currency": ""}],
          date_from="2026-07-01", date_to="2026-07-31")
    assert [t["country"] for t in cs.totals_by_country(DAY_FROM, DAY_TO)] == ["Chile"]


def test_a_reading_with_no_rows_is_not_a_country():
    _save("A", [], state="empty")
    assert cs.totals_by_country(DAY_FROM, DAY_TO) == []


# ── маршрут дешборда ──────────────────────────────────────────────────────

def _countries(monkeypatch, sections):
    monkeypatch.setattr(web, "get_cached_report", lambda: ({"countries": sections}, None, []))
    monkeypatch.setattr(web, "_enrich_country_overview", lambda report: None)
    response = asyncio.run(web.account_spend_countries(date_from=DAY_FROM, date_to=DAY_TO))
    return json.loads(response.body)


def test_the_route_marks_a_country_the_report_does_not_have(monkeypatch):
    """Дешборду нужно знать, под какую страну раздел ещё не открыт."""
    _save("A", [{"country": "Chile", "spend": 44.17, "currency": ""},
                {"country": "Peru", "spend": 3.0, "currency": ""}])
    data = _countries(monkeypatch, [{"country": "Peru"}])
    marks = {c["country"]: c["known"] for c in data["countries"]}
    assert marks == {"Chile": False, "Peru": True}


def test_country_names_are_matched_the_way_the_report_matches_them(monkeypatch):
    """Отчёт ключует страны через `strip().casefold()` — здесь так же."""
    _save("A", [{"country": "Chile", "spend": 1.0, "currency": ""}])
    data = _countries(monkeypatch, [{"country": "  chile "}])
    assert data["countries"][0]["known"] is True


def test_an_unreadable_workbook_does_not_hide_the_readings(monkeypatch):
    """Книга не прочиталась — показания всё равно наши, просто без пометок."""
    _save("A", [{"country": "Chile", "spend": 44.17, "currency": ""}])

    def _boom():
        raise RuntimeError("книга недоступна")

    monkeypatch.setattr(web, "get_cached_report", _boom)
    response = asyncio.run(web.account_spend_countries(date_from=DAY_FROM, date_to=DAY_TO))
    data = json.loads(response.body)
    assert data["ok"] is True
    assert [c["country"] for c in data["countries"]] == ["Chile"]
    assert data["countries"][0]["known"] is None
    assert "книга недоступна" in data["note"]


def test_a_bad_period_is_refused_before_anything_is_read(monkeypatch):
    response = asyncio.run(web.account_spend_countries(date_from="16.08.2026"))
    assert response.status_code == 400
    assert json.loads(response.body)["ok"] is False


# ── запуск чтения ─────────────────────────────────────────────────────────

def test_a_reading_starts_in_the_background_and_answers_at_once():
    """Держать страницу минуты — значит показать зависший дешборд."""
    import threading

    let_go = threading.Event()
    done = threading.Event()

    def slow_reader(account, date_from, date_to):
        let_go.wait(10)
        done.set()
        return cs.CountrySplit(account=account, date_from=date_from, date_to=date_to,
                               state="ok", rows=[{"country": "Chile", "spend": 1.0,
                                                  "currency": ""}])

    started = cs.start_split("A", DAY_FROM, DAY_TO, reader=slow_reader)
    try:
        assert started.state == "running"
        assert "A" in cs.running_now()
    finally:
        let_go.set()
    assert done.wait(10)
    for _ in range(50):
        if "A" not in cs.running_now():
            break
        import time
        time.sleep(0.1)
    assert cs.get_split("A", DAY_FROM, DAY_TO)["state"] == "ok"


def test_a_reader_that_raises_leaves_a_named_failure_not_silence():
    def angry(account, date_from, date_to):
        raise RuntimeError("профиль не отвечает")

    cs.start_split("B", DAY_FROM, DAY_TO, reader=angry)
    import time
    for _ in range(50):
        stored = cs.get_split("B", DAY_FROM, DAY_TO)
        if stored and stored.get("state") == "failed":
            break
        time.sleep(0.1)
    stored = cs.get_split("B", DAY_FROM, DAY_TO)
    assert stored["state"] == "failed"
    assert "профиль не отвечает" in stored["error"]


# ── знак не валюта: поперёк аккаунтов складываем только код ───────────────

def test_the_same_sign_on_two_accounts_is_not_added_up():
    """Два кабинета, оба со знаком `$`, могут вести учёт в песо и в долларах.

    Сложенное число выглядело бы обычным и было бы бессмысленным. Решение
    владельца 15.09: под знаком не складывать.
    """
    _save("A", [{"country": "Chile", "spend": 44.17, "currency": "$"}])
    _save("B", [{"country": "Chile", "spend": 10.0, "currency": "$"}])
    totals = cs.totals_by_country(DAY_FROM, DAY_TO)
    assert len(totals) == 2, "сложили два кабинета под одним знаком"
    assert {t["accounts"][0] for t in totals} == {"A", "B"}
    assert all(t["summed"] is False for t in totals)
    assert all(len(t["accounts"]) == 1 for t in totals)


def test_a_named_code_is_added_up_across_accounts():
    """«KWD» — валюта названа, и складывать её поперёк кабинетов безопасно."""
    _save("A", [{"country": "Kuwait", "spend": 20.0, "currency": "KWD"}])
    _save("B", [{"country": "Kuwait", "spend": 5.5, "currency": "KWD"}])
    totals = cs.totals_by_country(DAY_FROM, DAY_TO)
    assert len(totals) == 1
    assert totals[0]["spend"] == 25.5
    assert totals[0]["accounts"] == ["A", "B"]
    assert totals[0]["summed"] is True


def test_rows_of_one_account_still_add_up_under_a_sign():
    """Внутри одного кабинета валюта одна, что бы ни было написано."""
    _save("A", [{"country": "Peru", "spend": 1.5, "currency": "$"},
                {"country": "Peru", "spend": 2.5, "currency": "$"}])
    totals = cs.totals_by_country(DAY_FROM, DAY_TO)
    assert len(totals) == 1 and totals[0]["spend"] == 4.0


def test_an_unwritten_currency_is_not_added_up_either():
    """Пустая валюта — тоже «не названа»."""
    _save("A", [{"country": "Chile", "spend": 1.0, "currency": ""}])
    _save("B", [{"country": "Chile", "spend": 2.0, "currency": ""}])
    assert len(cs.totals_by_country(DAY_FROM, DAY_TO)) == 2
