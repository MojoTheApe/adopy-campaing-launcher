# -*- coding: utf-8 -*-
"""Мастер после загрузки проходится до конца и закрывается (acl-1207).

Живой прогон b31b45bc8599 (FacebookDQ501, 16.09 12:35–12:40): загрузка
прошла — на скриншоте «Creative setup ✓ · Media ✓ · Trim» и кнопка
«Skip and continue», — а шаг записал провал. Обход мастера знал только
Done/Save/Next, четыре раза не нашёл ни одной, вышел и оставил модалку.
Принудительное закрытие искало заголовок «Set up your creative», который Meta
убрала 24.08, — и не трогало ничего. Под открытой модалкой сломались
«None» в Browser add-ons, имя второго объявления и тексты.

Тесты идут в настоящем офлайн-Chrome на синтетической странице: мастер как
у Meta по форме — ряд шагов сверху, кнопка «Skip and continue», крестик в углу,
не role=dialog. Проверяется: обход знает новую кнопку и доходит до закрытия;
детектор видит мастер на шаге Trim и не видит его на обычной странице
объявления; принудительное закрытие работает на новой форме; старые
подписи кнопок работают как прежде.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom  # noqa: E402

STEPS = ["Creative setup", "Media", "Trim", "Crop", "Text", "Image generation", "Enhancements", "Translation"]

# Модалка Meta: полоса шагов сверху, кнопка вперёд с подписью, зависящей от
# шага, крестик в правом верхнем углу. Кнопка вперёд двигает активный шаг;
# после последнего модалка убирается и появляется «Edit media».
_WIZARD_HTML = """
<!doctype html><html><body style="margin:0">
<div id="ad">
  <div>Creative setup (0/1)</div>
  <div id="empty">Please specify the media to run with this ad.</div>
  <div id="filled" style="display:none">Edit media</div>
</div>
<div id="wiz" style="position:fixed;left:5vw;top:5vh;width:90vw;height:90vh;background:#fff;border:1px solid #888">
  <div style="display:flex;gap:16px;padding:8px">
    %(steps)s
    <span id="x" style="margin-left:auto;width:24px;height:24px;display:inline-block" onclick="closeWiz()">✕</span>
  </div>
  <div style="padding:16px">
    <button id="cancel">Cancel</button>
    <button id="fwd" onclick="advance()">%(first)s</button>
  </div>
</div>
<script>
  const labels = %(labels)s;
  let at = %(start)d;
  const clicks = [];
  window.clicks = clicks;
  function paint() {
    document.querySelectorAll('[data-step]').forEach((e, i) => e.style.fontWeight = i === at ? 'bold' : 'normal');
    document.getElementById('fwd').textContent = labels[at] || 'Done';
  }
  function advance() {
    clicks.push(document.getElementById('fwd').textContent);
    at += 1;
    if (at >= labels.length) return closeWiz();
    paint();
  }
  function closeWiz() {
    clicks.push('close');
    document.getElementById('wiz').remove();
    document.getElementById('empty').style.display = 'none';
    document.getElementById('filled').style.display = 'block';
  }
  paint();
</script>
</body></html>
"""


def _page_html(labels: list[str], start: int = 2) -> str:
    steps = "".join(f'<span data-step>{s}</span>' for s in STEPS)
    import json as _json
    return _WIZARD_HTML % {"steps": steps, "first": labels[start], "labels": _json.dumps(labels), "start": start}


# Подписи кнопок «вперёд» по шагам, как их показывает Meta с 16.09:
# на Trim/Crop/Text/… — «Skip and continue», в конце — «Done».
NEW_SHAPE = ["Next", "Next", "Skip and continue", "Skip and continue", "Skip and continue",
             "Skip and continue", "Skip and continue", "Done"]
OLD_SHAPE = ["Next"] * 7 + ["Done"]


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
    # Человеческие паузы здесь ни к чему: страница синтетическая.
    monkeypatch.setattr(meta_dom, "_sleep_h", lambda s: None)
    monkeypatch.setattr(meta_dom, "_human_jitter", lambda base, spread: 0.0)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _open(page, labels, start=2):
    page.set_content(_page_html(labels, start))


# ── то, что было сломано ──────────────────────────────────────────────────

def test_the_detector_sees_the_wizard_on_its_trim_step(page):
    _open(page, NEW_SHAPE)
    assert meta_dom._creative_wizard_open(page) is True


def test_the_walk_knows_skip_and_continue_and_gets_out(page):
    """Ровно экран из прогона: Trim, «Skip and continue»."""
    _open(page, NEW_SHAPE)
    assert meta_dom._walk_creative_wizard(page) is True
    assert not meta_dom._creative_wizard_open(page)
    clicks = page.evaluate("window.clicks")
    assert "Skip and continue" in clicks
    assert clicks[-1] == "close"


def test_the_media_is_kept_once_the_wizard_is_gone(page):
    _open(page, NEW_SHAPE)
    meta_dom._walk_creative_wizard(page)
    assert page.locator("#filled").is_visible()
    assert not page.locator("#empty").is_visible()


def test_force_close_works_on_the_new_shape(page):
    """Между попытками повтор закрывает мастер — раньше возвращал False."""
    _open(page, NEW_SHAPE)
    assert meta_dom._close_creative_setup_modal(page) is True
    assert not meta_dom._creative_wizard_open(page)


def test_force_close_falls_back_to_the_corner_icon_for_an_unknown_button(page):
    """Кнопку с незнакомой подписью не жмём наугад — выходим крестиком."""
    _open(page, ["Proceed"] * 8, start=2)
    assert meta_dom._close_creative_setup_modal(page) is True
    assert not meta_dom._creative_wizard_open(page)
    assert page.evaluate("window.clicks") == ["close"]


# ── ничего из прежнего не сломано ─────────────────────────────────────────

def test_the_old_labels_still_walk_the_wizard(page):
    _open(page, OLD_SHAPE, start=1)
    assert meta_dom._walk_creative_wizard(page) is True
    assert page.evaluate("window.clicks")[:-1] == OLD_SHAPE[1:]


def test_a_plain_ad_page_is_not_mistaken_for_the_wizard(page):
    page.set_content('<div>Creative setup (0/1)</div><div>Please specify the media to run with this ad.</div>'
                     '<button>Next</button>')
    assert meta_dom._creative_wizard_open(page) is False
    assert meta_dom._close_creative_setup_modal(page) is False


def test_a_walk_that_cannot_advance_says_so(page):
    _open(page, ["Proceed"] * 8, start=2)
    assert meta_dom._walk_creative_wizard(page) is False
    assert meta_dom._creative_wizard_open(page)


def test_the_upload_diagnostic_names_the_wizard_outcome():
    """`dialogs: 1` на СЛЕДУЮЩЕМ шаге — не диагностика. Шаг говорит сам."""
    import inspect
    src = inspect.getsource(meta_dom.upload_ad_creative)
    assert '_diag["wizard"]' in src and "left_open" in src
