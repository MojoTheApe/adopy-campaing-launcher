"""Спенд с аккаунта: хранилище, очередь и защита от чужого периода.

Браузер здесь не поднимается — на живом аккаунте это проверяет человек.
Проверяется всё остальное, и прежде всего то, ради чего задумана сверка
периода: число, взятое не за тот день, выглядит как правильный ответ.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analysis_agent import account_spend as sp  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    sp._RUNNING.clear()
    yield tmp_path


def test_yesterday_is_the_day_before():
    from datetime import date

    assert sp.yesterday(date(2026, 9, 7)) == "2026-09-06"
    assert sp.yesterday(date(2026, 1, 1)) == "2025-12-31"


def test_an_unread_account_is_idle():
    r = sp.get_reading("Acc", "2026-09-06")
    assert r.state == "idle" and r.spend is None


def test_a_reading_survives_a_restart():
    sp._put(sp.SpendReading(account="Acc", day="2026-09-06", spend=1234.5, state="ok"))
    again = sp.get_reading("Acc", "2026-09-06")
    assert again.spend == 1234.5 and again.state == "ok"


def test_readings_are_kept_per_day():
    sp._put(sp.SpendReading(account="Acc", day="2026-09-05", spend=1.0, state="ok"))
    sp._put(sp.SpendReading(account="Acc", day="2026-09-06", spend=2.0, state="ok"))
    assert sp.get_reading("Acc", "2026-09-05").spend == 1.0
    assert sp.all_readings("2026-09-06")["Acc"].spend == 2.0
    assert "Acc" in sp.all_readings("2026-09-05")


def test_a_corrupt_store_loses_readings_but_not_the_page(store):
    (store / "account_spend.json").write_text("{ не json", encoding="utf-8")
    assert sp.all_readings("2026-09-06") == {}


def test_unknown_fields_in_the_store_are_ignored():
    sp._save_all({"Acc|2026-09-06": {"account": "Acc", "day": "2026-09-06", "who_knows": 1}})
    assert sp.get_reading("Acc", "2026-09-06").account == "Acc"


# ── очередь ───────────────────────────────────────────────────────────


def _fake(result: sp.SpendReading):
    def reader(account: str, day: str) -> sp.SpendReading:
        return result

    return reader


def test_start_read_runs_and_stores(monkeypatch):
    done = sp.SpendReading(account="Acc", day="2026-09-06", spend=42.0, state="ok", how="test")
    first = sp.start_read("Acc", "2026-09-06", reader=_fake(done))
    assert first.state == "running"
    _settle()
    saved = sp.get_reading("Acc", "2026-09-06")
    assert saved.state == "ok" and saved.spend == 42.0
    assert saved.finished_at and saved.took_s >= 0


def test_a_reader_that_raises_becomes_a_failed_reading():
    def boom(account: str, day: str) -> sp.SpendReading:
        raise RuntimeError("профиль не поднялся")

    sp.start_read("Acc", "2026-09-06", reader=boom)
    _settle()
    saved = sp.get_reading("Acc", "2026-09-06")
    assert saved.state == "failed"
    assert "профиль не поднялся" in saved.error


def test_the_same_account_is_not_started_twice():
    import threading

    gate = threading.Event()

    def slow(account: str, day: str) -> sp.SpendReading:
        gate.wait(2)
        return sp.SpendReading(account=account, day=day, state="ok", spend=1.0)

    sp.start_read("Acc", "2026-09-06", reader=slow)
    assert sp.is_running("Acc", "2026-09-06")
    second = sp.start_read("Acc", "2026-09-06", reader=slow)
    assert second.state == "running"
    assert sp.running_now() == ["Acc"]
    gate.set()
    _settle()


def _settle(seconds: float = 3.0) -> None:
    import threading
    import time

    end = time.time() + seconds
    while time.time() < end:
        if not any(t.name.startswith("spend:") and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.05)
            return
        time.sleep(0.02)


# ── сверка периода: главное правило ───────────────────────────────────


@pytest.mark.parametrize(
    "shown, day, ok",
    [
        ("Sep 6, 2026", "2026-09-06", True),
        ("2026-09-06", "2026-09-06", True),
        ("Aug 1, 2026", "2026-09-06", False),
        ("Sep 6, 2026 - Sep 7, 2026", "2026-09-05", False),
        ("Last 7 days", "2026-09-06", False),
        ("", "2026-09-06", False),
    ],
)
def test_a_period_that_is_not_the_one_asked_for_is_refused(shown, day, ok):
    assert sp._period_matches(shown, day) is ok


def test_yesterday_on_screen_counts_only_when_we_asked_for_yesterday():
    assert sp._period_matches("Yesterday", sp.yesterday()) is True
    assert sp._period_matches("Yesterday", "2020-01-01") is False


# ── разбор числа ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, value",
    [
        ("$1,234.56", 1234.56),
        ("Results from 4 campaigns $12,044.00", 12044.0),
        ("€980", 980.0),
        ("1 234.50", 1234.50),
        ("no money here", None),
        # голое число без знака и без разделителя — не деньги
        ("Results from 4 campaigns", None),
        ("4 campaigns", None),
        ("$0.00", 0.0),
    ],
)
def test_money_is_read_out_of_the_cell(text, value):
    assert sp._first_money(text) == value


def test_url_carries_the_day_on_both_ends():
    url = sp._url("act_123", "2026-09-06")
    assert "act=act_123" in url and "date=2026-09-06_2026-09-06" in url


def test_url_adds_the_act_prefix_when_the_map_lacks_it():
    assert "act=act_123" in sp._url("123", "2026-09-06")


def test_a_missing_account_fails_before_any_browser_opens(monkeypatch):
    monkeypatch.setattr(sp, "_entry_for", lambda account: None)
    out = sp.read_via_rpa("Нет такого", "2026-09-06")
    assert out.state == "failed" and "нет в карте" in out.error


def test_an_account_without_an_id_fails_before_any_browser_opens(monkeypatch):
    class Entry:
        ad_account_id = ""

    monkeypatch.setattr(sp, "_entry_for", lambda account: Entry())
    out = sp.read_via_rpa("Acc", "2026-09-06")
    assert out.state == "failed" and "ad_account_id" in out.error
