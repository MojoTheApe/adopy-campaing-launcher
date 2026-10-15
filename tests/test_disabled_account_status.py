# -*- coding: utf-8 -*-
"""Отключённый кабинет не показывается как «no warning» (ACL-1391).

30.09 владелец прислал Ads Manager кабинета FacebookDoc20: у кампании
`KW_trl_All_Video_Mix_35_17.09` в колонке Delivery «Account disabled», баннера
об отключении нет — единственный баннер на странице «We're evolving how we
measure results». Карта при этом писала «no warning».

Ошибок было две:

1. Чтение состояния искало только баннеры. Форму «Account disabled» в колонке
   Delivery знал RPA (ACL-1313), а чтение — нет: две копии одного списка
   разошлись.
2. «no warning» было от 22.09, все чтения после него упали, а на экране
   старый ответ выглядел сегодняшним.

Разметка синтетическая, по скриншоту владельца 30.09: живой кабинет ради
снимка не открывали — чтение запускает только человек.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402

_MEASURE_BANNER = """
<div role="status"><span>We're evolving how we measure results</span>
  <div>Starting on 17 March 2026, click results will report website and in-store conversions only after link clicks.</div>
  <div role="button">About this update</div></div>
"""

_VERIFY_BANNER = """
<div role="alert"><div><span>Verification required to advertise</span></div>
  <div>Complete verification to remove restrictions from your ad account.</div></div>
"""

_PAGE = """<!doctype html><html><body>
%(banner)s
<div><span>Campaigns</span></div>
<table>
  <tr><th>Campaign</th><th>Delivery</th><th>Results</th></tr>
  <tr><td>Example_Campaign_Mix</td><td><span>%(delivery)s</span></td><td>9</td></tr>
  <tr><td>Example_Campaign_Day</td><td><span>Off</span></td><td>16</td></tr>
</table>
<div>Why is my ad account disabled? Learn more in the Help Center.</div>
</body></html>"""


def _page_html(*, delivery="Account disabled", banner=_MEASURE_BANNER) -> str:
    return _PAGE % {"delivery": delivery, "banner": banner}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch):
    monkeypatch.setattr(sp, "_sleep", lambda *_a, **_k: None)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


# ── первая ошибка: форма колонки Delivery ────────────────────────────────

def test_account_disabled_in_the_delivery_column_is_read_as_disabled(page):
    """Ровно случай Doc20: баннера нет, отключение видно только в Delivery."""
    page.set_content(_page_html())
    state, text = sp._read_status(page)
    assert state == "disabled"
    assert text == "Account disabled", "человеку нужны слова самой страницы"


def test_verification_required_is_not_clear(page):
    page.set_content(_page_html(delivery="Off", banner=_VERIFY_BANNER))
    state, text = sp._read_status(page)
    assert state == "restricted"
    assert "Verification required to advertise" in text


def test_the_help_link_alone_is_not_a_disabled_account(page):
    """«Why is my ad account disabled?» есть и у здорового кабинета."""
    page.set_content(_page_html(delivery="Off"))
    assert sp._read_status(page, tries=1) == ("clear", "")


def test_disabled_in_the_column_outranks_a_review_banner(page):
    page.set_content(_page_html(banner="<div>Your account is under review.</div>"))
    assert sp._read_status(page)[0] == "disabled"


_VERIFY_BANNER_TIGHT = (
    '<div role="alert"><div>Verification required to advertise</div>'
    "<div>Complete verification to remove restrictions from your ad account.</div></div>"
)


def test_disabled_in_the_column_outranks_the_verification_banner(page):
    """Прогон ecce52f330b8 (22.09): баннер «Verification required to advertise»
    выше ячейки «Account disabled». Детектор RPA отдаёт первое совпадение по
    порядку DOM — баннер; чтение должно взять все и выбрать `disabled`
    (ревью PR #392, F1)."""
    page.set_content(_page_html(banner=_VERIFY_BANNER_TIGHT))
    assert sp._read_status(page) == ("disabled", "Account disabled")


def test_the_cell_in_a_frame_is_read_too(page):
    """Совпадения собираются во всех фреймах, а не только в главном."""
    page.set_content(
        _page_html(delivery="Off", banner=_VERIFY_BANNER_TIGHT)
        + '<iframe srcdoc="<span>Account disabled</span>"></iframe>'
    )
    page.wait_for_function("document.querySelector('iframe').contentDocument.body.innerText.length > 0")
    assert sp._read_status(page)[0] == "disabled"


def test_meta_lines_are_joined_with_a_space(page):
    """`textContent` склеивает строки баннера: «…advertiseComplete…». В
    подсказку человеку идёт текст с пробелом."""
    page.set_content(_page_html(delivery="Off", banner=_VERIFY_BANNER_TIGHT))
    state, text = sp._read_status(page)
    assert state == "restricted"
    assert "advertiseComplete" not in text
    assert "advertise Complete verification" in text


def test_a_broken_rpa_detector_is_logged_once(page, monkeypatch, caplog):
    """Переименовали приватные имена RPA — чтение падает на баннеры, но не
    молча: одно предупреждение в лог (ревью PR #392, F6)."""
    from src.fb_ads_rpa import meta_dom

    monkeypatch.delattr(meta_dom, "_ACCOUNT_BLOCKED_JS")
    monkeypatch.setattr(sp, "_blocked_warned", False)
    page.set_content(_page_html())
    with caplog.at_level("WARNING", logger=sp.log.name):
        assert sp._read_status(page, tries=2) == ("clear", "")
        sp._read_status(page, tries=1)
    warned = [r for r in caplog.records if "account-block detector" in r.getMessage()]
    assert len(warned) == 1


def test_every_kind_the_rpa_knows_has_a_status():
    """Одна правда на два места: новый вид блокировки у RPA без слова здесь —
    это тот самый расход, из-за которого Doc20 стоял «no warning»."""
    kinds = {kind for kind, _pat in md._ACCOUNT_BLOCKED_PATTERNS}
    missing = kinds - set(sp._BLOCKED_KIND_STATUS)
    assert not missing, f"виды блокировки без слова в колонке состояния: {sorted(missing)}"
    assert "clear" not in sp._BLOCKED_KIND_STATUS.values()


def test_a_page_without_frames_still_reads_banners():
    """Подставная страница без `frames` не роняет чтение: признаки ячеек — добавка."""
    class _Text:
        def inner_text(self, _sel):
            return "Your ad account is disabled."
    assert sp._read_status(_Text(), tries=1)[0] == "disabled"


# ── вторая ошибка: старое состояние выдавалось за свежее ────────────────

ACC = "FacebookDoc20 - Example"


def _reading(finished, *, state="failed", status="", error=""):
    return {"account": ACC, "date_from": "2026-09-01", "date_to": "2026-09-28",
            "state": state, "status": status, "error": error, "finished_at": finished}


def _store(monkeypatch, **readings):
    data = {"status|" + ACC: {"status": "clear", "status_text": "", "at": "2026-09-22 08:52:04"}}
    for i, r in enumerate(readings.values()):
        data[f"{ACC}|2026-09-01|2026-09-{10 + i:02d}"] = r
    monkeypatch.setattr(sp, "_load_all", lambda: data)


def test_a_status_followed_by_failed_reads_is_not_refreshed(monkeypatch):
    _store(monkeypatch,
           ok=_reading("2026-09-22 08:52:04", state="ok", status="clear"),
           failed=_reading("2026-09-29 11:44:43", error="the profile is open by hand"))
    got = sp.get_status(ACC)
    assert got["status"] == "clear"
    assert got["stale"] == "1"
    assert got["tried_at"] == "2026-09-29 11:44:43"
    assert "open by hand" in got["tried_error"]
    assert got["at_short"] == "22.09.26", "с годом: старое состояние не путается с прошлогодним"


def test_the_read_that_wrote_the_status_does_not_make_it_stale(monkeypatch):
    _store(monkeypatch, ok=_reading("2026-09-22 08:52:04", state="ok", status="clear"))
    assert sp.get_status(ACC)["stale"] == ""


def test_a_failure_before_the_status_does_not_make_it_stale(monkeypatch):
    _store(monkeypatch,
           failed=_reading("2026-09-20 10:00:00", error="grid"),
           ok=_reading("2026-09-22 08:52:04", state="ok", status="clear"))
    assert sp.get_status(ACC)["stale"] == ""


def test_another_accounts_failure_does_not_touch_this_one(monkeypatch):
    other = _reading("2026-09-29 11:44:43", error="x")
    other["account"] = "FacebookDoc21 - Example"
    _store(monkeypatch, other=other)
    assert sp.get_status(ACC)["stale"] == ""


# ── экран ───────────────────────────────────────────────────────────────

def _render(status: dict) -> str:
    from jinja2 import Environment, FileSystemLoader

    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(ROOT / "src" / "web" / "templates")), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": ACC, "has_data": False, "ready": True,
                         "in_workbook": True, "status": status, "to_do": ""}]}
    html = env.get_template("_account_map_country.html").render(
        mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {},
              "splits": {}, "fallback": {}, "dates": [], "date_from": "2026-09-01",
              "date_to": "2026-09-28", "span": ""})
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


def test_the_map_shows_when_the_status_was_read():
    cell = _render({"status": "clear", "at": "2026-09-22 08:52:04", "at_short": "22.09.26", "stale": ""})
    assert "amap-st ok" in cell and "no warning" in cell
    assert ">22.09.26</small>" in cell


def test_an_old_no_warning_is_marked_as_not_refreshed():
    cell = _render({"status": "clear", "at": "2026-09-22 08:52:04", "at_short": "22.09.26", "stale": "1",
                    "tried_at": "2026-09-29 11:44:43", "tried_error": "the profile is open by hand"})
    assert "amap-st ok" not in cell, "устаревшее «no warning» выглядит как свежее"
    assert "amap-st dim" in cell
    assert "not refreshed" in cell
    assert "the latest read on 2026-09-29 11:44:43 failed" in cell


def test_a_disabled_account_is_red_on_the_map():
    cell = _render({"status": "disabled", "status_text": "Account disabled",
                    "at": "2026-09-30 10:00:00", "at_short": "30.09.26", "stale": ""})
    assert "amap-st bad" in cell and ">disabled</span>" in cell
    assert "Account disabled" in cell
