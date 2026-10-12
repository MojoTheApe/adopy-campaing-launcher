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
    r = sp.get_reading("Acc", "2026-09-06", "2026-09-06")
    assert r.state == "idle" and r.spend is None


def test_a_reading_survives_a_restart():
    sp._put(sp.SpendReading(account="Acc", date_from="2026-09-06", date_to="2026-09-06", spend=1234.5, state="ok"))
    again = sp.get_reading("Acc", "2026-09-06", "2026-09-06")
    assert again.spend == 1234.5 and again.state == "ok"


def test_readings_are_kept_per_day():
    sp._put(sp.SpendReading(account="Acc", date_from="2026-09-05", date_to="2026-09-05", spend=1.0, state="ok"))
    sp._put(sp.SpendReading(account="Acc", date_from="2026-09-06", date_to="2026-09-06", spend=2.0, state="ok"))
    assert sp.get_reading("Acc", "2026-09-05", "2026-09-05").spend == 1.0
    assert sp.all_readings("2026-09-06", "2026-09-06")["Acc"].spend == 2.0
    assert "Acc" in sp.all_readings("2026-09-05", "2026-09-05")


def test_a_corrupt_store_loses_readings_but_not_the_page(store):
    (store / "account_spend.json").write_text("{ не json", encoding="utf-8")
    assert sp.all_readings("2026-09-06", "2026-09-06") == {}


def test_unknown_fields_in_the_store_are_ignored():
    sp._save_all({"Acc|2026-09-06|2026-09-06": {"account": "Acc", "date_from": "2026-09-06", "date_to": "2026-09-06", "who_knows": 1}})
    assert sp.get_reading("Acc", "2026-09-06", "2026-09-06").account == "Acc"


# ── очередь ───────────────────────────────────────────────────────────


def _fake(result: sp.SpendReading):
    def reader(account: str, a: str, b: str) -> sp.SpendReading:
        return result

    return reader


def test_start_read_runs_and_stores(monkeypatch):
    done = sp.SpendReading(account="Acc", date_from="2026-09-06", date_to="2026-09-06", spend=42.0, state="ok", how="test")
    first = sp.start_read("Acc", "2026-09-06", "2026-09-06", reader=_fake(done))
    assert first.state == "running"
    _settle()
    saved = sp.get_reading("Acc", "2026-09-06", "2026-09-06")
    assert saved.state == "ok" and saved.spend == 42.0
    assert saved.finished_at and saved.took_s >= 0


def test_a_reader_that_raises_becomes_a_failed_reading():
    def boom(account: str, a: str, b: str) -> sp.SpendReading:
        raise RuntimeError("профиль не поднялся")

    sp.start_read("Acc", "2026-09-06", "2026-09-06", reader=boom)
    _settle()
    saved = sp.get_reading("Acc", "2026-09-06", "2026-09-06")
    assert saved.state == "failed"
    assert "профиль не поднялся" in saved.error


def test_the_same_account_is_not_started_twice():
    import threading

    gate = threading.Event()

    def slow(account: str, a: str, b: str) -> sp.SpendReading:
        gate.wait(2)
        return sp.SpendReading(account=account, date_from=a, date_to=b, state="ok", spend=1.0)

    sp.start_read("Acc", "2026-09-06", "2026-09-06", reader=slow)
    assert sp.is_running("Acc", "2026-09-06", "2026-09-06")
    second = sp.start_read("Acc", "2026-09-06", "2026-09-06", reader=slow)
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
    assert sp._period_matches("Yesterday", sp.yesterday(), sp.yesterday()) is True
    assert sp._period_matches("Yesterday", "2020-01-01", "2020-01-01") is False


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


def test_url_carries_the_account_and_the_day():
    url = sp._url("act_123", "2026-09-06", "2026-09-06")
    assert "act=123" in url and "date=2026-09-06_2026-09-07" in url


def test_url_sends_digits_because_meta_ignores_the_prefix():
    """Префикс нужен формату карты, но мешает выбору кабинета в Ads Manager."""
    assert "act=123&" in sp._url("123", "2026-09-06", "2026-09-06")
    assert "act=act_" not in sp._url("act_123", "2026-09-06", "2026-09-06")


def test_a_missing_account_fails_before_any_browser_opens(monkeypatch):
    monkeypatch.setattr(sp, "_entry_for", lambda account: None)
    out = sp.read_via_rpa("Нет такого", "2026-09-06", "2026-09-06")
    assert out.state == "failed" and "not in the account map" in out.error


def test_an_account_without_an_id_fails_before_any_browser_opens(monkeypatch):
    class Entry:
        ad_account_id = ""

    monkeypatch.setattr(sp, "_entry_for", lambda account: Entry())
    out = sp.read_via_rpa("Acc", "2026-09-06", "2026-09-06")
    assert out.state == "failed" and "ad_account_id" in out.error


# ── прицепиться, но не перезапускать ──────────────────────────────────
#
# Перезапуск профиля ради одного числа разлогинил x01. Ветка перезапуска
# вдобавок умеет терять прокси аккаунта. Поэтому чтение либо цепляется к
# уже открытому окну, либо не происходит вовсе.


class _Entry:
    ad_account_id = "act_1"
    profile_id = "p-1"


def _fake_browser(monkeypatch, *, attach, running, has_dir=True):
    from src.fb_ads_rpa import browser as rpa_browser

    class Direct:
        # Публичная пара, как у настоящего провайдера: она делегирует
        # приватным, а strict_profile зафиксирован внутри неё.
        def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
            return self._try_attach_existing(
                profile_id, user_data_dir, strict_profile=True, new_page=new_page
            )

        def profile_is_running(self, user_data_dir, profile_id=""):
            return self._profile_process_running(user_data_dir, profile_id)

        def _try_attach_existing(self, profile_id, udd, **kwargs):
            return attach

        def _profile_process_running(self, udd, profile_id):
            return running

    monkeypatch.setattr(rpa_browser, "MultiloginDirectCDPProvider", Direct)
    # папку ищем сами — обход всего дерева стоил десять секунд на вызов
    monkeypatch.setattr(
        sp, "_mlx_profile_dir",
        lambda pid, folder_id="": ("/profiles/" + pid) if has_dir else None,
    )


def test_an_entry_without_a_profile_cannot_be_attached():
    with pytest.raises(sp.ProfileNotAttachable, match="profile_id"):
        sp._attach_only(_Entry.__new__(type("E", (), {"profile_id": ""})), "Acc")


def test_an_open_automation_profile_is_reused(monkeypatch):
    marker = object()
    _fake_browser(monkeypatch, attach=marker, running=True)
    assert sp._attach_only(_Entry(), "Acc") is marker


from src.fb_ads_rpa import browser as rpa_browser  # noqa: E402


class _Provider:
    """Двойник провайдера Publish: запоминает, как его попросили открыть профиль."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def open(self, entry, label, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def test_a_hand_opened_profile_is_reopened_gently_like_publish(monkeypatch):
    """ACL-1812/ACL-1811: вручную открытый профиль автоматически НЕ перезапускается (разлогин 5414)."""
    _fake_browser(monkeypatch, attach=None, running=True)
    provider = _Provider(result=object())
    monkeypatch.setattr(rpa_browser, "get_browser_provider", lambda name: provider)
    with pytest.raises(sp.ProfileNotAttachable) as exc:
        sp._attach_only(_Entry(), "Acc")
    assert str(exc.value) == sp.HAND_OPEN_MESSAGE and exc.value.code == sp.HAND_OPEN_CODE
    assert provider.calls == [], provider.calls


def test_an_already_attached_profile_is_never_restarted(monkeypatch):
    marker = object()
    _fake_browser(monkeypatch, attach=marker, running=True)
    monkeypatch.setattr(rpa_browser, "get_browser_provider",
                        lambda name: pytest.fail("перезапуск не нужен — окно уже с портом"))
    assert sp._attach_only(_Entry(), "Acc") is marker


def test_a_closed_profile_says_so(monkeypatch):
    _fake_browser(monkeypatch, attach=None, running=False)
    with pytest.raises(sp.ProfileNotAttachable, match="is not open"):
        sp._attach_only(_Entry(), "Acc")


def test_a_missing_profile_directory_is_named(monkeypatch):
    _fake_browser(monkeypatch, attach=None, running=False, has_dir=False)
    with pytest.raises(sp.ProfileNotAttachable, match="Profile folder"):
        sp._attach_only(_Entry(), "Acc")


def test_the_refusal_becomes_a_readable_failure_not_a_crash(monkeypatch):
    monkeypatch.setattr(sp, "_entry_for", lambda account: _Entry())

    def refuse(entry, label):
        raise sp.ProfileNotAttachable("профиль не открыт")

    monkeypatch.setattr(sp, "_attach_only", refuse)
    out = sp.read_via_rpa("Acc", "2026-09-06", "2026-09-06")
    assert out.state == "failed" and out.error == "профиль не открыт"


# ── номер кабинета спрашиваем у самого кабинета ───────────────────────


def test_the_ad_account_comes_out_of_the_url():
    class Page:
        url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123&nav=1"

    assert sp._act_from_url(Page(), tries=1) == "act_123"


def test_an_already_prefixed_act_is_not_doubled():
    class Page:
        url = "https://adsmanager.facebook.com/x?act=act_123&y=2"

    assert sp._act_from_url(Page(), tries=1) == "act_123"


def test_no_account_in_the_url_is_not_a_number():
    class Page:
        url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"

    assert sp._act_from_url(Page(), tries=1) == ""


def test_a_probe_without_a_profile_asks_no_browser():
    out = sp.read_ad_account("")
    assert out["ok"] is False and "profile_id" in out["error"]


def test_a_profile_that_cannot_be_attached_is_explained(monkeypatch):
    def refuse(entry, label):
        raise sp.ProfileNotAttachable("профиль не открыт")

    monkeypatch.setattr(sp, "_attach_only", refuse)
    monkeypatch.setattr(sp, "_probe_entry", lambda *a, **k: object())
    out = sp.read_ad_account("p-1")
    assert out == {"ok": False, "act": "", "account_name": "", "error": "профиль не открыт"}


def test_attached_but_no_account_says_so(monkeypatch):
    class Page:
        url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"

        def goto(self, *a, **k):
            return None

        def query_selector(self, sel):
            return None

    class Conn:
        page = Page()

        def close(self, **k):
            return None

    monkeypatch.setattr(sp, "_probe_entry", lambda *a, **k: object())
    monkeypatch.setattr(sp, "_attach_only", lambda entry, label: Conn())
    monkeypatch.setattr(sp, "_act_from_url", lambda page, tries=20: "")
    out = sp.read_ad_account("p-1")
    assert out["ok"] is False and "no ad account number" in out["error"]


def test_a_good_probe_returns_the_number_and_the_name(monkeypatch):
    class Page:
        def goto(self, *a, **k):
            return None

    class Conn:
        page = Page()

        def close(self, **k):
            return None

    monkeypatch.setattr(sp, "_probe_entry", lambda *a, **k: object())
    monkeypatch.setattr(sp, "_attach_only", lambda entry, label: Conn())
    monkeypatch.setattr(sp, "_act_from_url", lambda page, tries=20: "act_777")
    monkeypatch.setattr(sp, "_ads_manager_account_name", lambda page: "KLD BFL X84")
    out = sp.read_ad_account("p-1", "folder-1", "B")
    assert out["ok"] and out["act"] == "act_777" and out["account_name"] == "KLD BFL X84"


def test_the_profile_directory_is_found_by_address_not_by_walking(tmp_path, monkeypatch):
    """Штатный обход дерева стоил ~10 секунд на вызов, и на каждый прогон тоже."""
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    wanted = tmp_path / "profiles" / "ws-1" / "folder-1" / "p-1"
    wanted.mkdir(parents=True)
    assert sp._mlx_profile_dir("p-1", "folder-1") == wanted
    # без folder_id тоже находит — на один уровень глубже, всё ещё без rglob
    assert sp._mlx_profile_dir("p-1") == wanted


def test_the_profile_directory_layout_without_a_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    flat = tmp_path / "profiles" / "ws-1" / "p-2"
    flat.mkdir(parents=True)
    assert sp._mlx_profile_dir("p-2") == flat


def test_a_profile_that_was_never_opened_here_has_no_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("MLX_HOME", str(tmp_path))
    (tmp_path / "profiles" / "ws-1").mkdir(parents=True)
    assert sp._mlx_profile_dir("p-3") is None


def test_no_mlx_home_is_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("MLX_HOME", str(tmp_path / "нет-такого"))
    assert sp._mlx_profile_dir("p-1") is None


# ── период приходит с экрана ──────────────────────────────────────────


def test_a_range_is_kept_apart_from_a_single_day():
    sp._put(sp.SpendReading(account="Acc", date_from="2026-09-06", date_to="2026-09-06", spend=1.0, state="ok"))
    sp._put(sp.SpendReading(account="Acc", date_from="2026-09-01", date_to="2026-09-06", spend=9.0, state="ok"))
    assert sp.get_reading("Acc", "2026-09-06", "2026-09-06").spend == 1.0
    assert sp.get_reading("Acc", "2026-09-01", "2026-09-06").spend == 9.0
    assert "Acc" not in sp.all_readings("2026-09-05", "2026-09-05")


def test_a_single_day_refuses_a_range_on_screen():
    """Месяц на экране не должен сойти за день по совпадению числа и года."""
    assert sp._period_matches("Sep 1, 2026 - Sep 30, 2026", "2026-09-01", "2026-09-01") is False
    assert sp._period_matches("Sep 1, 2026", "2026-09-01", "2026-09-01") is True


def test_a_range_needs_both_ends_on_screen():
    assert sp._period_matches("Sep 1, 2026 - Sep 6, 2026", "2026-09-01", "2026-09-06") is True
    assert sp._period_matches("Sep 6, 2026", "2026-09-01", "2026-09-06") is False


def test_the_url_end_is_exclusive_so_the_last_day_survives():
    """Проверено на живом аккаунте: date=…_06 показывает по пятое."""
    assert "date=2026-09-01_2026-09-07" in sp._url("act_1", "2026-09-01", "2026-09-06")
    assert "date=2026-09-06_2026-09-07" in sp._url("act_1", "2026-09-06")


def test_the_url_end_crosses_a_month():
    assert "date=2026-08-01_2026-09-01" in sp._url("act_1", "2026-08-01", "2026-08-31")


def test_a_broken_date_does_not_break_the_url():
    assert "date=нет_нет" in sp._url("act_1", "нет", "нет")


# ── валюта: передаём то, что написала Meta ────────────────────────────────

class _TotalsPage:
    """Страница, у которой в строке итогов одна сумма."""

    def __init__(self, raw):
        self._raw = raw

    def evaluate(self, script, *args):
        return {"spend": self._raw, "label": True}


import pytest as _pytest  # noqa: E402


@_pytest.mark.parametrize("raw, written", [
    ("$44.17", "$"),
    ("€12.00", "€"),
    ("£9.50", "£"),
    ("₪120.00", "₪"),
    ("KWD 21.61", "KWD"),
    ("", ""),
    ("Results from 4 campaigns", ""),
])
def test_the_currency_is_passed_through_as_meta_wrote_it(raw, written):
    """Ничего не переводим: `$` остаётся `$`.

    Прежде здесь стояло `("$", "USD")`, и на мексиканском кабинете дешборд
    молча подписывал песо долларами — цифра выглядела знакомой, усомниться
    было не в чем. Знак сам по себе не говорит, чьи это доллары, но и не
    врёт об этом, в отличие от подставленного кода.
    """
    page = _TotalsPage(raw)
    sp._scan_totals(page)
    assert getattr(page, "_acl_spend_currency", None) == written


def test_an_empty_campaign_table_is_saved_for_diagnosis(monkeypatch, tmp_path):
    """18.09 Doc19: «no-row-surfaces» без скриншота — теперь таблица сохраняется целиком."""
    from src.fb_ads_rpa import page_capture as pc

    saved = {}
    monkeypatch.setattr(pc, "capture", lambda page, **kw: saved.update(kw) or str(tmp_path))

    class _Page:
        def evaluate(self, js, *a):
            return "<div role='grid'><div role='columnheader'>Campaign</div></div>"

    sp._snapshot_empty_table(_Page(), "campaign", "no-row-surfaces")
    assert saved["step"] == "tree_campaign_no_rows"
    assert "columnheader" in (tmp_path / "block_table.html").read_text(encoding="utf-8")


# ── новая таблица Ads Manager без меток строк (FacebookDoc19, 18.09) ─────────

def test_the_new_table_without_row_ids_is_read_by_name():
    """Снимок живой таблицы: меток table_row нет, id кампаний в строках нет."""
    import os
    from pathlib import Path
    from playwright.sync_api import sync_playwright

    html = (Path(__file__).parent / "fixtures" / "meta" / "campaign_table_without_row_ids.html").read_text(encoding="utf-8")
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            page = b.new_page()
            page.set_content(html)
            got = page.evaluate(sp._READ_TREE_JS, "campaign")
        finally:
            b.close()
    assert got["how"] == "by-name"
    rows = {r["name"]: r for r in got["rows"]}
    assert list(rows) == ["Campaign 1", "Campaign 2", "Campaign 3", "Campaign 4", "Campaign 5"]
    first = rows["Campaign 1"]
    assert (first["id"], first["switch"], first["delivery"]) == ("", "on", "Active")
    assert (first["results"], first["cost_per_result"], first["spend"]) == ("6", "$19.27", "$115.59")
    assert rows["Campaign 3"]["switch"] == "off" and rows["Campaign 3"]["spend"] == "$0.00"
    assert got["kinds"] == ["CAMPAIGN_GROUP"]


def test_an_object_without_an_id_is_never_turned_off():
    out = sp._turn_off("Acc", "campaign", "", expected_name="Campaign 1")
    assert "without campaign ids" in out.error
