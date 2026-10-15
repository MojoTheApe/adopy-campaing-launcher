# -*- coding: utf-8 -*-
"""Свёрнутая карточка Audience в окне `editor_drawer` (ACL-1611).

05.10, Facebook_9403A, KW_TRL_All_Video_Mix_35_05.10 (прогоны 6ebd77ccaf6a,
e89bda52e7a5, 34a6c60bd4ed, 3f7c5979e020): `further_limit_row` ok=null
`not_opened` (пять попыток, строка найдена, но 0x0 — `unlaid`), затем
`further_limit_reach_click` ok=false. Строка лежала в скрытом теле карточки
Audience, свёрнутой в сводку (Custom audiences / Locations / Age / Gender /
Advantage+ detailed targeting / Languages, бейдж «Advantage+ on», кнопки
«Edit» нет). Карточка раскрывается кликом по самой карточке. Строки
`simple_card` в телеметрии прогонов нет вовсе: адрес
`adsets/edit/standalone`, не `simple_create`, флаг `simple` был ложным, а
`open_card("Audience")` звался только при `simple`.

Снимки — `audience_card_collapsed_drawer_2026_10_05.html` (card_run_a —
6ebd77ccaf6a/02_further_limit_reach_click, card_run_b —
e89bda52e7a5/03_further_limit_reach_click; настоящие снимки, не
сконструированные). Шапка и сводка — две кнопки `aria-pressed=false` с одной
подписью; в шапке ссылка «About audiences».
"""
from __future__ import annotations

import ast
import re

import pytest

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import run_telemetry
from src.fb_ads_rpa import simple_create as sc
from tests.bench_support import FIXTURES, ROOT, browser, fragment  # noqa: F401

FIXTURE = FIXTURES / "audience_card_collapsed_drawer_2026_10_05.html"
EXPANDED = FIXTURES / "audience_controls_badge_on_2026_10_03.html"

#: Эмуляция Meta: клик по кнопке карточки раскрывает тело; все нажатия
#: пишутся в window.log (что именно нажато), клик по ссылке — отдельно.
_META_JS = """
<script>
window.log = [];
document.addEventListener('click', (ev) => {
  const t = ev.target;
  const a = t.closest('a');
  const own = t.closest('[role=button]');
  window.log.push({ tag: t.tagName, link: !!a, role: own ? own.getAttribute('role') : '',
                    heading: !!t.closest('[role=heading]') });
  if (a) { ev.preventDefault(); return; }
  const btn = t.closest('[role=button][aria-pressed="false"]');
  if (btn && window.opens !== false) {
    const id = btn.getAttribute('aria-labelledby');
    document.querySelectorAll('[role=button][aria-pressed]').forEach(b => {
      if (b.getAttribute('aria-labelledby') === id) b.setAttribute('aria-pressed', 'true'); });
    if (window.bodyStays !== true) document.querySelectorAll('[hidden]').forEach(h => { h.hidden = false; });
  }
}, true);
</script>
"""


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    rows: list = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    p.rows = rows
    try:
        yield p
    finally:
        ctx.close()


def _load(page, name="card_run_a", *, fixture=FIXTURE):
    page.set_content("<html><body>" + fragment(fixture, name) + _META_JS + "</body></html>")


def _cards(page):
    return page.evaluate("[...document.querySelectorAll('[role=button][aria-pressed]')]"
                         ".map(b => b.getAttribute('aria-pressed'))")


def test_the_collapsed_card_is_detected(page):
    _load(page)
    state = page.evaluate(sc._CARD_JS, "Audience")
    assert state["found"] is True and state["pressed"] is False
    assert state["body"] is True, "тело со строкой Further limit не найдено"
    assert _cards(page) == ["false", "false"]


@pytest.mark.parametrize("name", ["card_run_a", "card_run_b"])
def test_the_card_is_opened_by_a_click_on_the_card_and_its_controls_are_verified(page, name):
    _load(page, name)
    before = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert not before.get("laid"), "строка разложена до раскрытия — вырезка не та"
    assert md._open_audience_card(page) == "opened"
    assert _cards(page) == ["true", "true"]
    row = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert row["found"] == 1 and row["laid"], "после клика строка Further limit не на экране"
    step, ok, kw = [r for r in page.rows if r[0] == "simple_card"][-1]
    assert ok is True and kw["note"] == "Audience:opened"
    assert kw["diag"]["how"] == "cdp" and kw["diag"]["controls"] >= 1


def test_nothing_but_the_card_is_pressed(page):
    """Ровно один клик — по заголовку карточки; ни ссылка «About audiences»,
    ни кнопка внутри, ни «Save audience» не нажимаются."""
    _load(page)
    md._open_audience_card(page)
    log = page.evaluate("window.log")
    assert len(log) == 1, log
    assert log[0]["link"] is False and log[0]["heading"] is True and log[0]["role"] == "button"


def test_the_link_in_the_card_header_is_never_the_click_target(page):
    """Ссылка «About audiences» лежит ровно там, где раньше жали (центр
    шапки): клик по ней открыл бы справку Meta в новой вкладке."""
    _load(page)
    old = page.evaluate("""() => { const bar = document.querySelector('[role=toolbar]');
        const r = bar.getBoundingClientRect(); return [r.x + Math.min(r.width / 2, 120), r.y + r.height / 2]; }""")
    page.evaluate("""([x, y]) => { const a = document.querySelector('a');
        a.style.cssText = 'position:fixed;z-index:9999;display:block;width:80px;height:30px;left:'
          + (x - 40) + 'px;top:' + (y - 15) + 'px'; }""", old)
    assert md._open_audience_card(page) == "opened"
    log = page.evaluate("window.log")
    assert [e for e in log if e["link"]] == [], log


def test_a_card_that_opens_without_controls_is_reported(page):
    """Нажатие прошло (кнопка нажата), а тело так и скрыто — не «opened»."""
    _load(page)
    page.evaluate("window.bodyStays = true")
    assert md._open_audience_card(page) == "no_controls"
    step, ok, kw = [r for r in page.rows if r[0] == "simple_card"][-1]
    assert ok is False and kw["note"] == "Audience:no_controls"


def test_a_card_that_does_not_open_is_reported(page):
    _load(page)
    page.evaluate("window.opens = false")
    assert md._open_audience_card(page) == "failed"


def test_an_open_card_is_not_clicked_again(page):
    _load(page)
    assert md._open_audience_card(page) == "opened"
    page.evaluate("window.log = []")
    assert md._open_audience_card(page) == "open"
    assert page.evaluate("window.log") == []


def test_the_expanded_card_of_1443_is_unchanged(page):
    """Раскрытая карточка 1443 (режим контролов): ничего не нажимается."""
    _load(page, "card_run_a", fixture=EXPANDED)
    assert md._open_audience_card(page) in ("open", "absent")
    assert page.evaluate("window.log") == []
    assert [r for r in page.rows if r[0] == "simple_card"] == []


def _calls(func_name):
    tree = ast.parse((ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", "")) == func_name]


def test_the_audience_steps_open_the_card_in_every_layout():
    """Оба места, где шаг начинается с Audience, зовут помощника без `if simple`
    (прежде — `if simple: open_card`, и в окне `editor_drawer` не открывали)."""
    src = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    assert not re.search(r"if simple:\s*\n\s*_simple\.open_card\(page, \"Audience\"\)", src)
    assert len(_calls("_open_audience_card")) >= 2
