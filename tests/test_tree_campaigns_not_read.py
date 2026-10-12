"""ACL-1504: неудачное чтение говорит «Campaigns not read», а не «0 campaigns».

Facebook_1443, 02.10: Ads Manager не открылся за 90 с, и раскрытая строка
карты сказала «IN THE ACCOUNT · 0 CAMPAIGNS · 0 ADS» с сырой ошибкой, где
стоял адрес с `act=`. Читалось как «в аккаунте нет кампаний» — неправда.

Здесь осталась половина про чтение: оно различает «список прочитан и пуст» и «до
списка не дошли» (account_spend). Экранная половина (старый список «in the
account») убрана в ACL-1800: под строкой аккаунта теперь командная таблица, а её
«Not read yet» и ошибки чтения проверяет `test_acl1800_account_scoped_table.py`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402

ADSET = {"level": "campaign", "id": "120250178859040703", "name": "SA_ARM_iOS_IMG_Reading_30_6.9",
         "switch": "on", "switch_label": "On/off", "delivery": "Active",
         "spend": "$412.02", "results": "18 Leads (Form)",
         "cost_per_result": "$22.89 Per Lead (Form)"}
AD = {"level": "ad", "id": "120250178850950703", "name": "1962_12- Officialy",
      "switch": "on", "switch_label": "On/off", "delivery": "Active",
      "spend": "$120.40", "results": "5 Leads (Form)", "cost_per_result": ""}

# Выдуманный id собирается на ходу: в исходнике `act=<цифры>` ловит сканер
# секретов (scripts/acl_scan_secrets.py), а проверить нужно именно эту форму.
FAKE_ID = "9" * 13
RAW = (
    "campaign: navigation failed (Page.goto: Timeout 90000ms exceeded.\n"
    "Call log:\n  - navigating to \"https://adsmanager.facebook.com/adsmanager/manage/"
    "campaigns?act=" + FAKE_ID + "&date=2026-09-29_2026-09-29\", waiting until "
    "\"domcontentloaded\" act=987654321)"
)


# ── чтение: дошли ли до списка ─────────────────────────────────────────────

class _Page:
    def __init__(self, fail_goto: bool = False):
        self.fail_goto = fail_goto
        self.url = ""

    def goto(self, url, **kw):
        if self.fail_goto:
            raise TimeoutError("Page.goto: Timeout 90000ms exceeded.")
        self.url = url


@pytest.fixture
def passable(monkeypatch):
    monkeypatch.setattr(sp, "_wait_for_grid", lambda page: True)
    monkeypatch.setattr(sp, "_act_from_url", lambda page, tries=1: "act_123")
    monkeypatch.setattr(sp, "_shown_period", lambda page, tries=12: "Sep 1, 2026")
    monkeypatch.setattr(sp, "_period_matches", lambda shown, a, b="": True)
    return monkeypatch


def test_a_navigation_failure_did_not_reach_the_list(passable):
    reached: dict = {}
    objects, _, err = sp._read_tree(_Page(fail_goto=True), "act_123", "act_123",
                                    "2026-09-01", reached=reached)
    assert objects == [] and "navigation failed" in err
    assert not reached.get("list")


def test_an_empty_list_that_was_read_is_read(passable):
    passable.setattr(sp, "read_level", lambda page, level: ([], "data-surface", ""))
    reached: dict = {}
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01", reached=reached)
    assert objects == [] and err == ""
    assert reached.get("list") is True


def test_an_unparsed_table_is_not_an_empty_list(passable):
    passable.setattr(sp, "read_level",
                     lambda page, level: ([], "", f"{level} table was not parsed: boom"))
    reached: dict = {}
    sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01", reached=reached)
    assert not reached.get("list")


# ── что уходит на экран ────────────────────────────────────────────────────

def _reading(**kw) -> sp.SpendReading:
    base = dict(account="Facebook_1443", date_from="2026-09-29", date_to="2026-09-29",
                state="ok", spend=10.0, currency="USD", finished_at="2026-10-02 10:00:00")
    base.update(kw)
    return sp.SpendReading(**base)


def test_not_read_carries_a_readable_reason():
    out = _reading(objects_error=RAW, objects_read=False).to_dict()
    problem = out["objects_problem"]
    assert problem["reason"] == "Ads Manager did not open in 90 s."
    assert "Read again" in problem["action"] and "Multilogin" in problem["action"]


def test_the_technical_text_has_no_account_ids_or_urls():
    shown = _reading(objects_error=RAW, objects_read=False).to_dict()["objects_error"]
    assert "navigation failed" in shown
    assert FAKE_ID not in shown and "987654321" not in shown
    assert "https://" not in shown and "facebook.com" not in shown


def test_an_empty_read_list_is_not_a_problem():
    out = _reading(objects=[], objects_error="", objects_read=True).to_dict()
    assert "objects_problem" not in out


def test_an_old_record_without_the_flag_is_judged_by_itself():
    assert "objects_problem" in _reading(objects_error=RAW).to_dict()
    assert "objects_problem" not in _reading(objects=[{"level": "campaign"}]).to_dict()


def test_a_failed_spend_read_is_not_a_tree_problem():
    out = _reading(state="failed", objects_error=RAW, objects_read=False).to_dict()
    assert "objects_problem" not in out


def test_the_last_good_tree_stays_with_its_date(monkeypatch, tmp_path):
    store = tmp_path / "account_spend.json"
    monkeypatch.setattr(sp, "_store_path", lambda: store)
    good = _reading(date_from="2026-09-28", date_to="2026-09-28",
                    finished_at="2026-09-29 11:46:00", objects=[ADSET, AD], objects_read=True)
    sp._put(good)
    bad = _reading(objects_error=RAW, objects_read=False)
    sp._put(bad)
    saved = json.loads(store.read_text(encoding="utf-8"))
    prev = saved["Facebook_1443|2026-09-29|2026-09-29"]["objects_prev"]
    assert prev["at"] == "2026-09-29 11:46:00" and prev["date_from"] == "2026-09-28"
    assert len(prev["objects"]) == 2
    # Вторая неудача подряд не стирает прежнее дерево.
    sp._put(_reading(date_from="2026-09-30", date_to="2026-09-30",
                     finished_at="2026-10-02 12:00:00", objects_error=RAW, objects_read=False))
    saved = json.loads(store.read_text(encoding="utf-8"))
    assert saved["Facebook_1443|2026-09-30|2026-09-30"]["objects_prev"]["at"] == "2026-09-29 11:46:00"


def test_a_bare_meta_token_is_scrubbed():
    """L1: токен вне адреса и без `access_token=` тоже не уходит на экран."""
    token = "EAA" + "Bx7" * 12  # собран на ходу — см. FAKE_ID
    shown = sp.scrub_tree_error(f"campaign: request failed with {token} in a header")
    assert token not in shown and "EAA" not in shown
    assert "request failed" in shown
