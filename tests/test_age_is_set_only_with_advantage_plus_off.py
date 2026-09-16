# -*- coding: utf-8 -*-
"""Возраст ставится только после выключения Advantage+ и сверяется по экрану (acl-1208).

Живой прогон ceab1a4bf031 (FacebookDQ501, 16.09 12:18–12:19): шаг возраста
кликнул 18 и 65+, потом 35 — и записал провал; на скриншоте в этот момент
«Advantage+ on» на карточке Audience и «Advantage+ leads campaign — On»
справа. В этом режиме выпадашки возраста — подсказки, клик на 35 не
берётся. В трёх других прогонах дня возраст ставился: дело не в селекторах,
а в том, что переключатель оказался включён к моменту шага.

Проверяется на синтетической странице в офлайн-Chrome: детектор видит оба
признака Advantage+ и различает «выключено» и «не нашёл»; выключение
переключает и пишет телеметрию; чтение показывает, что стоит в выпадашках;
а сам `fill_audience_age` — не трогает контролы, пока Advantage+ включён, и
не говорит «готово», пока экран показывает не то.
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
from src.fb_ads_rpa import run_telemetry  # noqa: E402

_PAGE = """
<!doctype html><html><body>
<div id="card">
  <div>Audience</div>
  <span id="badge" %(badge_style)s>Advantage+ %(badge)s</span>
  <div>
    <div>Advantage+ leads campaign</div>
    <div id="sw" role="switch" aria-checked="%(checked)s" onclick="flip()">toggle</div>
  </div>
  <div id="agerow">
    <div>Age</div>
    <div><div class="v" id="amin">%(amin)s</div><div class="v" id="amax">%(amax)s</div></div>
  </div>
</div>
<script>
  function flip() {
    const sw = document.getElementById('sw');
    const on = sw.getAttribute('aria-checked') === 'true';
    sw.setAttribute('aria-checked', on ? 'false' : 'true');
    document.getElementById('badge').textContent = on ? 'Advantage+ off' : 'Advantage+ on';
  }
</script>
</body></html>
"""


def _html(*, on: bool, amin="18", amax="65+", badge_hidden=False) -> str:
    return _PAGE % {
        "badge": "on" if on else "off",
        "badge_style": 'style="display:none"' if badge_hidden else "",
        "checked": "true" if on else "false",
        "amin": amin, "amax": amax,
    }


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
    monkeypatch.setattr(meta_dom, "_sleep_h", lambda s: None)
    monkeypatch.setattr(meta_dom, "_human_jitter", lambda base, spread: 0.0)
    # Телеметрия — во временный файл, не в живой output/.
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path):
    import json
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── детектор ──────────────────────────────────────────────────────────────

def test_the_badge_alone_means_advantage_plus_is_on(page):
    page.set_content(_html(on=True))
    page.evaluate("document.getElementById('sw').remove()")
    assert meta_dom._advantage_plus_audience_on(page) is True


def test_the_switch_alone_means_advantage_plus_is_on(page):
    page.set_content(_html(on=True, badge_hidden=True))
    assert meta_dom._advantage_plus_audience_on(page) is True


def test_off_is_off_and_absent_is_unknown(page):
    page.set_content(_html(on=False))
    assert meta_dom._advantage_plus_audience_on(page) is False
    page.set_content("<div>Age</div><div>18</div><div>65+</div>")
    assert meta_dom._advantage_plus_audience_on(page) is None


# ── выключение ────────────────────────────────────────────────────────────

def test_switching_off_flips_the_switch_and_records_it(page, tmp_path):
    page.set_content(_html(on=True))
    assert meta_dom._ensure_manual_audience(page) is False
    assert page.get_attribute("#sw", "aria-checked") == "false"
    rows = [r for r in _rows(tmp_path) if r["step"] == "advantage_plus_off"]
    assert rows and rows[-1]["ok"] is True and rows[-1]["note"] == "switched"


def test_already_off_is_recorded_not_touched(page, tmp_path):
    page.set_content(_html(on=False))
    assert meta_dom._ensure_manual_audience(page) is False
    assert page.get_attribute("#sw", "aria-checked") == "false"
    rows = [r for r in _rows(tmp_path) if r["step"] == "advantage_plus_off"]
    assert rows and rows[-1]["ok"] is None and rows[-1]["note"] == "already_off"


_MENU_PAGE = """
<!doctype html><html><body>
<div id="card">
  <div>Audience</div>
  <span id="badge">Advantage+ on</span>
  <div id="trig" role="button" tabindex="-1" onclick="openMenu()">
    <span role="heading">Advantage+ leads campaign</span>
    <span id="state">On</span>
    <span role="presentation">⌄</span>
  </div>
  <div role="button" id="close-btn" onclick="window.picked.push('CLOSE')">Close</div>
  <div id="menu" style="display:none">
    <div role="menuitem" onclick="pick('On')">On</div>
    <div role="menuitem" onclick="pick('Off')">Off — Turn off Advantage+</div>
  </div>
  <div><div>Age</div><div><div class="v">18</div><div class="v">65+</div></div></div>
</div>
<script>
  window.picked = [];
  function openMenu() { document.getElementById('menu').style.display = 'block'; }
  function pick(v) {
    window.picked.push(v);
    document.getElementById('menu').style.display = 'none';
    document.getElementById('state').textContent = v;
    document.getElementById('badge').textContent = v === 'Off' ? 'Advantage+ off' : 'Advantage+ on';
  }
</script>
</body></html>
"""


def test_the_menu_shaped_control_is_opened_and_off_is_picked(page, tmp_path):
    """Раскладка 2026-09: не переключатель, а кнопка «On ˅» с меню (дамп прогона 86b7cd7edda9)."""
    page.set_content(_MENU_PAGE)
    assert meta_dom._ensure_manual_audience(page) is False
    assert page.evaluate("window.picked") == ["Off"]
    rows = [r for r in _rows(tmp_path) if r["step"] == "advantage_plus_off"]
    assert rows[-1]["note"] == "switched" and rows[-1]["diag"]["picked_off"] is True


# ── чтение экрана ─────────────────────────────────────────────────────────

def test_reading_the_dropdowns_gives_what_they_show(page):
    page.set_content(_html(on=False, amin="35", amax="65+"))
    assert meta_dom._age_triggers_read(page) == ["35", "65+"]


def test_a_value_inside_an_open_list_is_not_a_reading(page):
    page.set_content(_html(on=False) + '<div role="listbox"><div>35</div><div>36</div></div>')
    assert meta_dom._age_triggers_read(page) == ["18", "65+"]


# ── сам шаг ───────────────────────────────────────────────────────────────

def test_the_controls_are_not_touched_while_advantage_plus_stays_on(page, tmp_path, monkeypatch):
    """Кликать по подсказкам бессмысленно — и вредно: клик мог уйти куда угодно."""
    page.set_content(_html(on=True))
    monkeypatch.setattr(meta_dom, "_turn_off_advantage_plus_leads", lambda p: False)
    touched = []
    monkeypatch.setattr(meta_dom, "_fill_audience_age_controls", lambda p, a, b: touched.append((a, b)) or True)
    assert meta_dom.fill_audience_age(page, 35, 65) is False
    assert touched == []
    rows = _rows(tmp_path)
    assert any(r["step"] == "audience_age" and r["note"] == "advantage_plus_still_on" for r in rows)


def test_clicked_35_is_not_the_same_as_shows_35(page, tmp_path, monkeypatch):
    """Ровно случай ceab1a4bf031: контролы «отработали», экран показывает 18."""
    page.set_content(_html(on=False, amin="18", amax="65+"))
    monkeypatch.setattr(meta_dom, "_fill_audience_age_controls", lambda p, a, b: True)
    assert meta_dom.fill_audience_age(page, 35, 65) is False
    row = [r for r in _rows(tmp_path) if r["step"] == "audience_age"][-1]
    assert row["note"] == "read_back_mismatch"
    assert row["diag"]["wanted"] == ["35", "65+"] and row["diag"]["seen"] == ["18", "65+"]


def test_the_happy_path_is_unchanged(page, monkeypatch):
    page.set_content(_html(on=False, amin="35", amax="65+"))
    monkeypatch.setattr(meta_dom, "_fill_audience_age_controls", lambda p, a, b: True)
    assert meta_dom.fill_audience_age(page, 35, 65) is True


def test_advantage_plus_is_switched_off_before_the_controls_run(page, monkeypatch):
    order = []
    page.set_content(_html(on=True, amin="35", amax="65+"))
    real = meta_dom._turn_off_advantage_plus_leads

    def flip(p):
        order.append("off")
        return real(p)

    monkeypatch.setattr(meta_dom, "_turn_off_advantage_plus_leads", flip)
    monkeypatch.setattr(meta_dom, "_fill_audience_age_controls", lambda p, a, b: order.append("controls") or True)
    assert meta_dom.fill_audience_age(page, 35, 65) is True
    assert order == ["off", "controls"]
