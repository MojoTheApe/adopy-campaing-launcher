"""Последнее чтение за другие даты — запасным и помеченным (ACL-1271).

Карта показывала чтение только за период на экране. Открыл 1–22 сентября,
а читали 1–21 — строка «—», «0/1 spend read», дерева нет, хотя вчерашнее
чтение есть. Теперь оно показывается приглушённо, со своими датами, и в итог
страны не входит.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.web import account_links as links  # noqa: E402
from src.web import app  # noqa: E402


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(app, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))

    def put(account, date_from, date_to, spend, finished, state="ok"):
        sp._put(sp.SpendReading(account=account, date_from=date_from, date_to=date_to,
                                spend=spend, currency="$", state=state, finished_at=finished))
    return put


def test_the_latest_ok_reading_of_any_period_is_found(store):
    store("A", "2026-09-01", "2026-09-20", 900.0, "2026-09-21 08:00:00")
    store("A", "2026-09-01", "2026-09-21", 1044.82, "2026-09-22 08:52:04")
    store("A", "2026-09-01", "2026-09-22", None, "2026-09-22 09:00:00", state="failed")
    got = sp.latest_readings()
    assert got["A"].spend == 1044.82 and got["A"].date_to == "2026-09-21"


def test_account_status_records_are_not_readings(store):
    sp._put(sp.SpendReading(account="A", date_from="2026-09-01", date_to="2026-09-21",
                            spend=5.0, currency="$", state="ok", finished_at="2026-09-22 08:00:00"))
    data = sp._load_all()
    data["status|A"] = {"status": "clear", "at": "2026-09-22 10:00:00", "state": "ok", "spend": 1}
    sp._save_all(data)
    assert set(sp.latest_readings()) == {"A"}
    assert sp.latest_readings()["A"].spend == 5.0


def _country(view, name="KW"):
    return next(c for c in view["countries"] if c["key"] == name)


def test_no_reading_for_the_dates_on_screen_shows_the_latest_marked(store):
    links.link("A", "country", "KW")
    store("A", "2026-09-01", "2026-09-21", 1044.82, "2026-09-22 08:52:04")
    view = app._build_account_map_view({"countries": []}, date_from="2026-09-01", date_to="2026-09-22")
    assert view["fallback"]["A"]["spend"] == 1044.82
    country = _country(view)
    assert country["live_read"] == 0 and country["live_sums"] == [], "запасное чтение попало в итог страны"
    html = app.templates.env.get_template("_account_map.html").render(amap=view, mlx={})
    assert 'data-read-state="fallback"' in html
    assert 'data-fb-from="2026-09-01" data-fb-to="2026-09-21"' in html
    assert "$1,044.82" in html and "09-01…09-21" in html
    assert "not the dates on screen" in html
    assert "0/1 spend read" in html


def test_an_exact_reading_wins_and_is_counted(store):
    links.link("A", "country", "KW")
    store("A", "2026-09-01", "2026-09-21", 1044.82, "2026-09-22 08:52:04")
    view = app._build_account_map_view({"countries": []}, date_from="2026-09-01", date_to="2026-09-21")
    assert "A" not in view["fallback"]
    assert _country(view)["live_read"] == 1


def test_never_read_stays_a_dash(store):
    links.link("A", "country", "KW")
    view = app._build_account_map_view({"countries": []}, date_from="2026-09-01", date_to="2026-09-22")
    assert view["fallback"] == {}
    html = app.templates.env.get_template("_account_map.html").render(amap=view, mlx={})
    assert 'data-read-state=""' in html


def test_all_dates_asks_for_no_fallback(store):
    links.link("A", "country", "KW")
    store("A", "2026-09-01", "2026-09-21", 1044.82, "2026-09-22 08:52:04")
    view = app._build_account_map_view({"countries": []}, span="all")
    assert view["fallback"] == {}



def test_an_old_format_record_does_not_hide_every_fallback(store):
    """Ревью: запись `account|day` без date_from/date_to ронила разбор, и
    запасного чтения не получал ни один аккаунт — так было на живом хранилище."""
    store("A", "2026-09-01", "2026-09-21", 1044.82, "2026-09-22 08:52:04")
    data = sp._load_all()
    data["KLD_BFL_X84|2026-09-06"] = {"account": "KLD_BFL_X84", "day": "2026-09-06",
                                      "state": "ok", "spend": 259.48, "currency": "$",
                                      "finished_at": "2026-09-07 10:00:00"}
    sp._save_all(data)
    got = sp.latest_readings()
    assert set(got) == {"A"} and got["A"].spend == 1044.82


def test_no_dates_on_screen_means_no_fallback(store, monkeypatch):
    """Без дат кнопка — «Select dates»; «not the dates on screen» там неправда."""
    links.link("A", "country", "KW")
    store("A", "2026-09-01", "2026-09-21", 1044.82, "2026-09-22 08:52:04")
    monkeypatch.setattr(app, "_month_to_date", lambda dates: ("", ""))
    view = app._build_account_map_view({"countries": []})
    assert view["date_from"] == "" and view["fallback"] == {}


# ── страница: итог страны и дерево из запасного чтения ────────────────────

def _page_with_fallback(browser_page, html):
    src = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")

    def fn(name):
        start = src.index("function " + name + "(")
        return src[start: src.index("\nfunction ", start + 10)]

    browser_page.set_content(html)
    browser_page.add_script_tag(content="var amapReadAllBusy = false;\n" + "\n".join(
        fn(n) for n in ("amapMoney", "amapSummaryMoney", "amapUpdateCountrySummaries", "amapSpendFailed")))


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        p = b.new_page()
        yield p
        b.close()


def test_the_country_total_leaves_the_fallback_out(page):
    _page_with_fallback(page, """
      <details class="amap-country" data-country="KW"><summary>
        <span class="amap-live-summary"><span class="amap-live-summary-values"></span>
        <i class="amap-live-summary-count"></i></span></summary>
        <table><tr><td class="amap-live" data-account="A" data-read-state="ok" data-read-spend="10" data-read-currency="USD"></td></tr>
        <tr><td class="amap-live" data-account="B" data-read-state="fallback" data-read-spend="" data-read-currency=""
            data-fb-from="2026-09-01" data-fb-to="2026-09-21"></td></tr></table></details>""")
    page.evaluate("() => amapUpdateCountrySummaries()")
    got = page.evaluate("() => document.querySelector('.amap-live-summary').textContent.replace(/\\s+/g, ' ').trim()")
    assert got == "USD 10.00 1/2 spend read"



def test_a_failed_reading_shows_the_old_one_too(store):
    """Решение владельца 22.09: при неудаче за эти даты — оба, неудача и
    запасное чтение под ней; в итог страны по-прежнему не входит."""
    links.link("A", "country", "KW")
    store("A", "2026-09-01", "2026-09-21", 1044.82, "2026-09-22 08:52:04")
    store("A", "2026-09-01", "2026-09-22", None, "2026-09-22 09:00:00", state="failed")
    view = app._build_account_map_view({"countries": []}, date_from="2026-09-01", date_to="2026-09-22")
    assert view["fallback"]["A"]["spend"] == 1044.82
    assert _country(view)["live_read"] == 0
    html = app.templates.env.get_template("_account_map.html").render(amap=view, mlx={})
    cell = html.split('class="num amap-live"', 1)[1].split("</td>", 1)[0]
    assert 'data-read-state="failed"' in cell and 'data-fb-from="2026-09-01"' in cell
    assert "amap-live-bad" in cell and "amap-live-old" in cell and "$1,044.82" in cell
    assert "failed · retry" in html


def test_a_live_failure_keeps_the_old_amount_in_the_cell(page):
    _page_with_fallback(page, """<table><tr><td class="amap-live" data-account="B" data-read-state="fallback"
        data-fb-from="2026-09-01" data-fb-to="2026-09-21"><span class="amap-live-old">$1,044.82<small>09-01…09-21</small></span></td>
        <td><button class="amap-read">Read spend</button></td></tr></table>""")
    page.evaluate("""() => amapSpendFailed(document.querySelector('.amap-read'),
                        document.querySelector('.amap-live'), 'profile is closed', '', '')""")
    got = page.evaluate("() => document.querySelector('.amap-live').innerText.replace(/\\s+/g, ' ').trim()")
    assert got.startswith("profile is closed") and "$1,044.82" in got



def test_the_old_amount_stands_on_its_own_line_under_a_failure(page):
    src = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    from _design_tokens import tokens_css  # ACL-1797: палитра — отдельный файл
    css = tokens_css() + src[src.index("    .amap-live-ok {"): src.index("    .amap-tree-head.old {")]
    page.set_content('<style>' + css + '</style><table><tr><td class="amap-live" style="width:200px">'
                     '<span class="amap-live-bad">profile is closed</span>'
                     '<span class="amap-live-old">$1,044.82<small>09-01…09-21</small></span></td></tr></table>')
    bad = page.evaluate("() => document.querySelector('.amap-live-bad').getBoundingClientRect().bottom")
    old = page.evaluate("() => document.querySelector('.amap-live-old').getBoundingClientRect().top")
    assert old >= bad - 1, "старое число прилипло к тексту неудачи"
