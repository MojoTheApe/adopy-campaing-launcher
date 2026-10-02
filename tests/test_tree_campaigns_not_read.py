"""ACL-1504: неудачное чтение говорит «Campaigns not read», а не «0 campaigns».

Facebook_1443, 02.10: Ads Manager не открылся за 90 с, и раскрытая строка
карты сказала «IN THE ACCOUNT · 0 CAMPAIGNS · 0 ADS» с сырой ошибкой, где
стоял адрес с `act=`. Читалось как «в аккаунте нет кампаний» — неправда.

Здесь две половины: чтение различает «список прочитан и пуст» и «до списка не
дошли» (account_spend), а экран рисует второе словами, с Read again и
свёрнутыми техническими подробностями (настоящий код из report.html).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from tests.test_the_account_tree_on_screen import ADSET, AD, browser, page  # noqa: E402,F401

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


# ── экран ──────────────────────────────────────────────────────────────────

def _with_read_button(page, disabled=False):
    page.evaluate("""(off) => {
      window.__reads = 0;
      const b = document.createElement('button');
      b.className = 'amap-btn amap-read'; b.disabled = off;
      b.onclick = () => { window.__reads++; };
      document.querySelector('#acc td').appendChild(b);
    }""", disabled)


def _paint(page, s):
    page.evaluate("(s) => amapPaintTree(document.getElementById('acc'), s)", s)
    page.evaluate("() => { const t = document.querySelector('.amap-tree'); if (t) t.hidden = false; }")


def _failed(prev=None):
    r = _reading(objects_error=RAW, objects_read=False, objects_prev=prev or {})
    return r.to_dict()


def test_failed_before_the_list_says_campaigns_not_read(page):
    _with_read_button(page)
    _paint(page, _failed())
    text = page.evaluate("() => document.querySelector('.amap-tree').textContent")
    assert "campaigns not read" in text.lower()
    assert "0 campaigns" not in text
    assert "Ads Manager did not open in 90 s." in text
    assert "Open the profile in Multilogin" in text
    assert page.evaluate("() => !document.querySelector('.amap-tree-err')")


def test_the_raw_error_is_collapsed_and_scrubbed(page):
    _with_read_button(page)
    _paint(page, _failed())
    got = page.evaluate("""() => {
      const d = document.querySelector('.amap-tree details.amap-tree-tech');
      return d && {open: d.open, summary: d.querySelector('summary').textContent,
                   body: d.querySelector('.amap-tree-tech-body').textContent};
    }""")
    assert got and got["open"] is False and got["summary"] == "Technical details"
    assert "navigation failed" in got["body"]
    assert FAKE_ID not in got["body"] and "https://" not in got["body"]


def test_read_again_presses_the_existing_read_spend(page):
    _with_read_button(page)
    _paint(page, _failed())
    page.evaluate("() => document.querySelector('.amap-tree-reread').click()")
    assert page.evaluate("() => window.__reads") == 1


def test_read_again_waits_while_reading(page):
    _with_read_button(page, disabled=True)
    _paint(page, _failed())
    assert page.evaluate("() => document.querySelector('.amap-tree-reread').disabled") is True


def test_the_last_good_numbers_stay_with_their_read_date(page):
    _with_read_button(page)
    prev = {"objects": [ADSET, AD], "at": "2026-09-29 11:46:00",
            "date_from": "2026-09-28", "date_to": "2026-09-28"}
    _paint(page, _failed(prev))
    got = page.evaluate("""() => ({
      head: document.querySelector('.amap-tree-prev-head').textContent,
      lines: document.querySelectorAll('.amap-tree-line').length,
      buttons: document.querySelectorAll('.amap-tree button.amap-tree-sw').length,
    })""")
    assert "last good read" in got["head"] and "2026-09-29 11:46" in got["head"]
    assert "1 campaigns" in got["head"] and "1 ads" in got["head"]
    assert got["lines"] == 2
    # Тумблер прежнего чтения мог смениться — выключать по нему нельзя.
    assert got["buttons"] == 0


def test_a_read_empty_list_says_zero_campaigns(page):
    _paint(page, _reading(objects=[], objects_error="", objects_read=True).to_dict())
    text = page.evaluate("() => document.querySelector('.amap-tree-head').textContent")
    assert "0 campaigns · 0 ads" in text
    assert page.evaluate("() => !document.querySelector('.amap-tree-notread')")


# ── ревью PR #505 ──────────────────────────────────────────────────────────

def _template_fn(head: str) -> str:
    src = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    start = src.index(head)
    return src[start: src.index("\n}\n", start) + 2]


def test_read_again_comes_back_after_read_all(page):
    """M1: во время Read all Read spend погашен, и дерево рисуется с серым
    Read again. Когда очередь кончилась, Read spend включается — Read again
    обязан включиться вместе с ним, без перезагрузки."""
    page.add_script_tag(content="var amapReadAllBusy = false;\n"
                        + _template_fn("function amapSpendCell(btn) {") + "\n"
                        + _template_fn("function amapReadAllControls(busy) {"))
    page.evaluate("""() => {
      window.__reads = 0;
      const b = document.createElement('button');
      b.className = 'amap-btn amap-read'; b.disabled = true;
      b.setAttribute('onclick', 'window.__reads++');
      document.querySelector('#acc td').appendChild(b);
    }""")
    _paint(page, _failed())
    assert page.evaluate("() => document.querySelector('.amap-tree-reread').disabled") is True
    page.evaluate("() => amapReadAllControls(false)")
    got = page.evaluate("""() => ({
      read: document.querySelector('#acc .amap-read').disabled,
      again: document.querySelector('.amap-tree-reread').disabled,
    })""")
    assert got == {"read": False, "again": False}
    page.evaluate("() => document.querySelector('.amap-tree-reread').click()")
    assert page.evaluate("() => window.__reads") == 1
    # И обратно: новый Read all гасит обе.
    page.evaluate("() => amapReadAllControls(true)")
    assert page.evaluate("() => document.querySelector('.amap-tree-reread').disabled") is True


def test_a_bare_meta_token_is_scrubbed():
    """L1: токен вне адреса и без `access_token=` тоже не уходит на экран."""
    token = "EAA" + "Bx7" * 12  # собран на ходу — см. FAKE_ID
    shown = sp.scrub_tree_error(f"campaign: request failed with {token} in a header")
    assert token not in shown and "EAA" not in shown
    assert "request failed" in shown


def test_a_try_for_other_dates_says_so(page):
    """L2: запасное чтение за другие даты (ACL-1271) и в «not read» говорит,
    что даты не те, что на экране."""
    _with_read_button(page)
    _paint(page, _failed())  # чтение за 2026-09-29, на экране 1–12 сентября
    head = page.evaluate("() => document.querySelector('.amap-tree-notread-head').textContent")
    assert "campaigns not read" in head and "not the dates on screen" in head
    page.evaluate("() => { window._amapPick = {from: '2026-09-29', to: '2026-09-29', span: ''}; }")
    _paint(page, _failed())
    head = page.evaluate("() => document.querySelector('.amap-tree-notread-head').textContent")
    assert "not the dates on screen" not in head
