# -*- coding: utf-8 -*-
"""Категория плейсментов, свёрнутая Meta посреди простановки, раскрывается снова (ACL-1313).

Живой прогон 25.09 09:43 (CL_ML_All_Video_Day, run 10402d9c2daf): шаг
`placements` вернул False — «Messenger Stories» и «WhatsApp Status» были в
`subitems_seen` категории «Stories, Status, Reels», но оба захода и перечитка
ответили «строка не найдена». Тот же отказ — 19.09 (921824496d79: «Messenger
Stories» снялась, отказала следующая за ней «WhatsApp Status»), 21.09
(ca89176131c2, af0204b30dd4), 22.09 (8587f7668f86: хвост «Feeds» после трёх
снятых галочек). Отказывает хвост категории после кликов: Meta после
сохранения правки перерисовывает раздел, раскрытая категория сворачивается, её
строки уходят из DOM. Код раскрывал категорию один раз.

Разметка — `tests/fixtures/ads_manager/placements_node_2026_09_25.html`,
вырезана из снимка того прогона (01_placements): узел «Placements», пять
категорий, у «In-stream ads for reels» — две настоящие строки подпунктов.
Строки «Feeds» и «Stories, Status, Reels» в снимке не смонтированы (категории
свёрнуты), поэтому тест строит их клоном настоящей строки In-stream из того же
снимка, с подписями из `subitems_seen` отказа. Поведение Meta (гармошка
категорий, aria-checked после отрисовки, сворачивание после клика) — скрипт
теста; Meta JS в снимке нет.
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

FIXTURE = ROOT / "tests" / "fixtures" / "ads_manager" / "placements_node_2026_09_25.html"

# Подписи — ровно `subitems_seen` отказа 25.09, по категориям (порядок строк
# как на странице). Третье поле — недоступная галочка («Facebook Business
# Explore» Meta держит снятой и трогать не даёт).
_ROWS = {
    "Feeds": [
        ("Facebook Feed", True, False),
        ("Facebook profile feed", True, False),
        ("Instagram feed", True, False),
        ("Instagram profile feed", True, False),
        ("Facebook Marketplace", True, False),
        ("Facebook right column", True, False),
        ("Instagram Explore home", True, False),
        ("Facebook Business Explore", False, True),
        ("Threads feed", True, False),
        ("Facebook Notifications", True, False),
    ],
    "Stories, Status, Reels": [
        ("Instagram Stories", True, False),
        ("Facebook Stories", True, False),
        ("Messenger Stories", True, False),
        ("Instagram Reels", True, False),
        ("Facebook Reels", True, False),
        ("WhatsApp Status", True, False),
    ],
    "In-stream ads for reels": [
        ("Facebook in-stream reels", True, False),
        ("Ads on Facebook Reels", True, False),
    ],
}

# Поведение Meta поверх снимка. `collapse`: "never" — категория держится
# раскрытой; "first" — первый клик по подпункту категории сворачивает её через
# 30 мс (перерисовка после сохранения), один раз на категорию; "every" — так
# после каждого клика.
_SIM_JS = r"""
(cfg) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const leaf = (text) => [...document.querySelectorAll('*')].find(e =>
    e.children.length === 0 && norm(e.textContent) === text);
  const state = {};
  const owner = {};
  for (const [cat, rows] of Object.entries(cfg.rows))
    for (const [label, checked, disabled] of rows) {
      state[label] = { checked, disabled };
      owner[label] = cat;
    }
  const buttons = {};
  for (const cat of Object.keys(cfg.rows)) {
    buttons[cat] = leaf(cat).closest('[role=button][aria-expanded]');
    // Состояние на входе в шаг — отчёт отказа: before: true у всех трёх.
    const cb = buttons[cat].querySelector('input[type=checkbox]');
    cb.checked = true; cb.setAttribute('aria-checked', 'true');
  }
  const liveRegion = document.querySelector('[id="facebook/instream_video_check"]').closest('[role=region]');
  const rowTpl = liveRegion.querySelector('[id="facebook/instream_video_check"]').cloneNode(true);
  const regionTpl = liveRegion.cloneNode(true);
  const tplLabel = 'Facebook in-stream reels';
  liveRegion.remove();
  let seq = 0;
  const collapsed = new Set();

  const buildRow = (label) => {
    const row = rowTpl.cloneNode(true);
    row.id = 'sim-row-' + (++seq);
    for (const e of row.querySelectorAll('*'))
      if (e.children.length === 0 && norm(e.textContent) === tplLabel) e.textContent = label;
    const cb = row.querySelector('input[type=checkbox]');
    const st = state[label];
    cb.id = 'sim-cb-' + seq;
    cb.checked = st.checked;
    cb.setAttribute('aria-checked', String(st.checked));
    cb.disabled = st.disabled;
    cb.setAttribute('aria-disabled', String(st.disabled));
    cb.dataset.simLabel = label;
    const lab = row.querySelector('label[for]');
    if (lab) lab.setAttribute('for', cb.id);
    return row;
  };
  const close = (cat) => {
    const b = buttons[cat];
    b.setAttribute('aria-expanded', 'false');
    const r = b.parentElement.querySelector(':scope > [role=region]');
    if (r) r.remove();
  };
  const open = (cat) => {
    for (const other of Object.keys(buttons)) if (other !== cat) close(other);
    const b = buttons[cat];
    b.setAttribute('aria-expanded', 'true');
    const region = regionTpl.cloneNode(true);
    const first = region.querySelector('[id="facebook/instream_video_check"]');
    const holder = first.parentElement;
    holder.replaceChildren(...cfg.rows[cat].map(([label]) => buildRow(label)));
    b.parentElement.appendChild(region);
  };
  for (const [cat, b] of Object.entries(buttons)) {
    b.addEventListener('click', (e) => {
      if (e.target.closest('input, label')) return;   // галочка категории, не раскрыватель
      if (b.getAttribute('aria-expanded') === 'false') open(cat); else close(cat);
    });
  }
  document.addEventListener('click', (e) => {
    const cb = e.target;
    if (!(cb instanceof HTMLInputElement) || cb.type !== 'checkbox') return;
    setTimeout(() => cb.setAttribute('aria-checked', String(cb.checked)), 0);
    const label = cb.dataset.simLabel;
    if (!label) return;
    state[label].checked = cb.checked;
    const cat = owner[label];
    if (cfg.collapse === 'every' || (cfg.collapse === 'first' && !collapsed.has(cat))) {
      collapsed.add(cat);
      setTimeout(() => close(cat), 30);
    }
  }, true);
  open('In-stream ads for reels');
  window.__sim = { state };
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
    # Паузы шага — короткие, но настоящие: скрипт страницы живёт на таймерах.
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: time.sleep(min(base, 0.05)))
    monkeypatch.chdir(tmp_path)          # output/debug_placements_fill.txt — не в репозиторий
    (tmp_path / "output").mkdir()
    ctx = browser.new_context(viewport={"width": 1000, "height": 800})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _load(page, collapse: str) -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    page.set_content(f"<html><body style='margin:0;padding-top:120px'>{html}</body></html>")
    page.evaluate(_SIM_JS, {"rows": {k: [list(r) for r in v] for k, v in _ROWS.items()},
                            "collapse": collapse})


def _state(page) -> dict[str, bool]:
    return {k: v["checked"] for k, v in page.evaluate("() => window.__sim.state").items()}


def _assert_wanted(state: dict[str, bool]) -> None:
    by_fold = {k.casefold(): v for k, v in state.items()}
    wrong = [x for x in md._SUBITEMS_ON if by_fold.get(x.casefold()) is not True]
    wrong += [x for x in md._SUBITEMS_OFF if by_fold.get(x.casefold()) is not False]
    assert not wrong, f"не в нужном состоянии: {wrong}"


def test_the_fixture_is_the_failing_layout(page):
    """Снимок — тот самый вариант раздела: пять категорий и раскрыватель над строкой."""
    _load(page, "never")
    got = page.evaluate(
        "() => [...document.querySelectorAll('[role=button][aria-expanded]')]"
        ".map(b => b.getAttribute('aria-expanded'))")
    assert len(got) >= 5
    assert "Account controls" in page.inner_text("body")


def test_a_category_meta_collapsed_after_a_click_is_opened_again(page):
    """25.09: после клика категория свернулась — хвост подпунктов ставится после перераскрытия."""
    _load(page, "first")
    report: dict = {}
    ok = md.fill_placements(page, report)
    assert report.get("subitems_missing") == [], report.get("subitems_failed")
    assert ok is True, report
    _assert_wanted(_state(page))
    reopened = report.get("reopened") or {}
    assert {"Feeds", "Stories, Status, Reels"} <= set(reopened), reopened
    assert all(r[0]["was"] == "false" for r in reopened.values()), reopened
    # Встали после перераскрытия — в отчёте об отказе их больше нет.
    assert not report.get("subitems_failed"), report.get("subitems_failed")


def test_an_open_category_is_not_clicked_shut(page):
    """Meta держит категорию раскрытой — повторного раскрытия нет, всё ставится с первого захода."""
    _load(page, "never")
    report: dict = {}
    assert md.fill_placements(page, report) is True, report
    _assert_wanted(_state(page))
    assert "reopened" not in report, report.get("reopened")


def test_a_page_that_collapses_on_every_click_gets_an_honest_refusal(page):
    """Перераскрытие ограничено: против страницы, сворачивающей на каждый клик, — отказ, а не цикл."""
    _load(page, "every")
    report: dict = {}
    t0 = time.monotonic()
    assert md.fill_placements(page, report) is False
    assert time.monotonic() - t0 < 60
    assert report["subitems_missing"], report
    for name, tries in (report.get("reopened") or {}).items():
        assert len(tries) <= md._PLACEMENT_REOPENS, (name, tries)
