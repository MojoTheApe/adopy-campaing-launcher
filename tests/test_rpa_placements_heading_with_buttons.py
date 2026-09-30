# -*- coding: utf-8 -*-
"""Карточка Placements, у которой кнопки лежат внутри заголовка (Facebook_6540, 28.09).

Живой прогон 28.09 (6925f39bdc67, 8058b54f78d0 — обе кампании аккаунта,
классический редактор ad set): шаг `placements` падал за секунду,
черновик уходил в DO NOT PUBLISH. На других аккаунтах тот же шаг проходил
(`tag_info={'tagged': True, 'depth': 9, ...}`).

Причина — разметка заголовка. Meta положила группу кнопок «Explore placement
opportunities» / «More options» ВНУТРЬ `role=heading`:
`<div role=heading><span>Placements<div>…<div role=group>…</div></div></span></div>`.
Листа, чей текст целиком «Placements», в карточке больше нет. Единственный
такой лист — строка правой панели Campaign score («Placements · You're
including all available placements for all ad sets.»), а до предка с
«Account controls» от неё 33 уровня — `tagged: false`, пустое False.

Разметка — `tests/fixtures/ads_manager/placements_heading_group_2026_09_28.html`,
вырезана из снимка 6925f39bdc67/01_placements: карточка целиком и строка
правой панели. Meta JS в снимке нет; «Account controls» в тесте подменён
заглушкой — окно настроек аккаунта здесь не при чём.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ads_manager" / "placements_heading_group_2026_09_28.html"

# Кнопки, которые шаг не имеет права нажимать: рекомендации Advantage+,
# меню заголовка и крестик уведомления «WhatsApp Status included».
_FORBIDDEN = ("Explore placement opportunities", "More options", "Close", "Create a rule set")

_WATCH_CLICKS_JS = r"""
() => {
  window.__clicks = [];
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[role=button], a, button') || e.target;
    window.__clicks.push((b.textContent || '').replace(/[\s​]+/g, ' ').trim().slice(0, 60));
  }, true);
}
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: time.sleep(min(base, 0.02)))
    monkeypatch.chdir(tmp_path)          # output/debug_placements_fill.txt — не в репозиторий
    (tmp_path / "output").mkdir()
    calls: list[int] = []

    def _no_account_controls(pg, report=None):
        calls.append(1)
        return False

    monkeypatch.setattr(md, "ensure_account_placement_exclusions", _no_account_controls)
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    p.account_controls_calls = calls
    try:
        yield p
    finally:
        ctx.close()


def _load(page, html: str) -> None:
    page.set_content(
        "<html><body style='margin:0;padding-top:120px;display:flex;gap:40px'>"
        f"{html}</body></html>"
    )
    page.evaluate(_WATCH_CLICKS_JS)


def _debug() -> str:
    return Path("output/debug_placements_fill.txt").read_text(encoding="utf-8")


def test_the_fixture_is_the_failing_layout(page):
    """Снимок тот самый: в карточке нет листа «Placements», в правой панели — есть."""
    _load(page, FIXTURE.read_text(encoding="utf-8"))
    got = page.evaluate(
        r"""() => {
          const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
          const leaves = (root) => [...root.querySelectorAll('*')].filter(e =>
            e.children.length === 0 && norm(e.textContent) === 'Placements').length;
          const h = document.querySelector('[data-fixture=editor] [role=heading][aria-level="3"]');
          return {
            editor: leaves(document.querySelector('[data-fixture=editor]')),
            panel: leaves(document.querySelector('[data-fixture=right_panel]')),
            heading: norm(h.textContent),
            groupInHeading: !!h.querySelector('[role=group]'),
          };
        }"""
    )
    assert got["editor"] == 0
    assert got["panel"] == 1
    assert got["heading"].startswith("PlacementsExplore placement opportunities")
    assert got["groupInHeading"]


def test_the_card_is_marked_not_the_campaign_score_row(page):
    _load(page, FIXTURE.read_text(encoding="utf-8"))
    report: dict = {}
    # Категорий в снимке нет (раздел свёрнут под «Show more settings», а Meta
    # JS, который их показал бы, не сохранён) — шаг честно заканчивается на
    # «категории не достать». Важно другое: он дошёл до карточки.
    md.fill_placements(page, report)
    dbg = _debug()
    assert "tagged': True" in dbg, dbg
    assert "'via': 'heading'" in dbg, dbg
    marked = page.evaluate(
        r"""() => {
          const m = document.querySelector('[data-rpa-placements-marker]');
          if (!m) return null;
          const t = (m.textContent || '').replace(/\s+/g, ' ');
          return {
            inEditor: !!m.closest('[data-fixture=editor]'),
            accountControls: t.includes('Account controls'),
            showMore: t.includes('Show more settings'),
            scoreRow: t.includes("You're including all available placements"),
          };
        }"""
    )
    assert marked == {"inEditor": True, "accountControls": True, "showMore": True,
                      "scoreRow": False}, marked
    assert report.get("why") != "placements_card_not_found", report
    assert "show_more_clicked=True" in dbg, dbg
    clicks = page.evaluate("() => window.__clicks")
    assert not [c for c in clicks if c.startswith(_FORBIDDEN)], clicks


def test_without_the_card_the_report_names_the_headings_seen(page):
    """Карточки нет — в отчёте причина и заголовки, которые шаг видел, а не пустое False."""
    html = FIXTURE.read_text(encoding="utf-8")
    panel = html[html.index('<div data-fixture="right_panel">'):]
    _load(page, '<div data-fixture="editor"><div role="heading" aria-level="3">Budget &amp; schedule</div></div>'
                + panel)
    report: dict = {}
    assert md.fill_placements(page, report) is False
    assert report.get("why") == "placements_card_not_found", report
    assert "Budget & schedule" in report["placements_card"]["headings_seen"], report
    assert page.locator("[data-rpa-placements-marker]").count() == 0
