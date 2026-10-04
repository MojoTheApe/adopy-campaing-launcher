# -*- coding: utf-8 -*-
"""Возраст ставится только после выключения Advantage+ и сверяется по экрану (acl-1208).

Живой прогон ceab1a4bf031 (FacebookDQ501, 16.09 12:18–12:19): шаг возраста
кликнул 18 и 65+, потом 35 — и записал провал; на скриншоте в этот момент
«Advantage+ on» на карточке Audience и «Advantage+ leads campaign — On»
справа. В этом режиме выпадашки возраста — подсказки, клик на 35 не
берётся. В трёх других прогонах дня возраст ставился: дело не в селекторах,
а в том, что переключатель оказался включён к моменту шага.

ACL-1313 (25.09): `_ensure_manual_audience` читает состояние по бейджу
карточки Audience (`#advantage-state-audience-section-header`) и без него
ничего не кликает, поэтому бейдж синтетических страниц `_PAGE` и
`_MENU_PAGE` обёрнут в этот заголовок, как в живой раскладке 09.2026. Сами
утверждения о выключении не менялись: при бейдже «on» выключение по-прежнему
кликается и пишется.

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
  <div id="advantage-state-audience-section-header"><span id="badge" %(badge_style)s>Advantage+ %(badge)s</span></div>
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
  <div id="advantage-state-audience-section-header"><span id="badge">Advantage+ on</span></div>
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


def test_a_label_split_into_several_text_nodes_is_still_found(page, tmp_path):
    """Так рендерит React: «Advantage+» и « leads campaign» — два текстовых
    узла. `text()` видит только первый; сохранённый HTML склеивает их, поэтому
    на дампе путь находил строку, а вживую — нет, пять прогонов подряд."""
    page.set_content(_MENU_PAGE)
    page.evaluate("""() => {
      const h = document.querySelector('[role=heading]');
      h.textContent = '';
      h.appendChild(document.createTextNode('Advantage+'));
      h.appendChild(document.createTextNode(' leads campaign'));
    }""")
    assert page.evaluate("document.querySelector('[role=heading]').childNodes.length") == 2
    assert meta_dom._ensure_manual_audience(page) is False
    assert page.evaluate("window.picked") == ["Off"]


def test_a_row_inside_a_fixed_panel_is_still_clickable(page, tmp_path):
    """Живая раскладка: правая панель `position: fixed`, `offsetParent` у
    строки — null, а видна она прекрасно. Четыре прогона подряд путь не
    находил её только поэтому."""
    fixed = _MENU_PAGE.replace('<div id="card">', '<div id="card" style="position:fixed;right:0;top:0;width:400px">')
    page.set_content(fixed)
    assert meta_dom._ensure_manual_audience(page) is False
    assert page.evaluate("window.picked") == ["Off"]


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

def test_a_switch_that_stays_on_does_not_block_settable_age_controls(page, tmp_path, monkeypatch):
    """Переключатель «Advantage+ leads campaign» — кампанийный и остаётся On
    даже после «Further limit the reach», когда выпадашки возраста уже живые
    (прогон 2182744d1126). Запрет по нему отказывал в каждом прогоне вечера;
    решает сверка экрана после установки."""
    page.set_content(_html(on=True, amin="35", amax="65+"))
    monkeypatch.setattr(meta_dom, "_turn_off_advantage_plus_leads", lambda p: False)
    monkeypatch.setattr(meta_dom, "_pick_advantage_plus_leads_off", lambda p: False)
    touched = []
    monkeypatch.setattr(meta_dom, "_fill_audience_age_controls", lambda p, a, b: touched.append((a, b)) or True)
    assert meta_dom.fill_audience_age(page, 35, 65) is True
    assert touched == [(35, 65)]
    rows = [r for r in _rows(tmp_path) if r["step"] == "advantage_plus_off"]
    # ACL-1544: на синтетической странице нет ни контролов, ни строки Further
    # limit — режим карточки не прочитать, и шаг не гадает: `unconfirmed`.
    assert rows and rows[-1]["note"] == "unconfirmed", "попытка выключить всё равно записана"
    assert rows[-1]["ok"] is None


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


# ── 16.09, e79f678f1450: «Off» у Dynamic creative — не наш пункт ───────────

_DYNAMIC_PAGE = """
<!doctype html><html><body>
<div id="card">
  <div>Audience</div><span id="badge">Advantage+ on</span>
  <div role="button" onclick="document.getElementById('checklist').style.display='block'">
    <span role="heading">Advantage+ leads campaign</span><span>On</span>
  </div>
  <div id="checklist" style="display:none"><div>Budget</div><div>Audience</div></div>
  <div role="button"><div role="heading">Dynamic creative</div>
    <label onclick="openDialog()">Off</label><input type="checkbox" role="switch" aria-label="Off"></div>
  <div><div>Age</div><div><div class="v">18</div><div class="v">65+</div></div></div>
</div>
<div id="dlg" role="dialog" style="display:none">
  <div>How to create ads using dynamic creative</div>
  <div role="button" onclick="window.dlg.push('cancel');document.getElementById('dlg').style.display='none'">Cancel</div>
  <div role="button" onclick="window.dlg.push('continue')">Continue</div>
</div>
<script>
  window.dlg = [];
  function openDialog() { window.dlg.push('opened'); document.getElementById('dlg').style.display = 'block'; }
</script>
</body></html>
"""


def test_the_dynamic_creative_off_label_is_never_clicked(page):
    page.set_content(_DYNAMIC_PAGE)
    assert meta_dom._pick_advantage_plus_leads_off(page) is False
    assert page.evaluate("window.dlg") == []


def test_an_open_dynamic_creative_dialog_is_cancelled_not_continued(page, tmp_path):
    page.set_content(_DYNAMIC_PAGE)
    page.evaluate("openDialog()")
    assert meta_dom._cancel_dynamic_creative_dialog(page) is True
    assert page.evaluate("window.dlg") == ["opened", "cancel"]
    assert not page.is_visible("#dlg")


def test_the_age_step_believes_the_field_over_its_own_boundary_report(page, monkeypatch):
    """17.09 22:12 CR_ait FX_Y: на экране «35 – 65+», а шаг отказал и не снял галочку."""
    monkeypatch.setattr(meta_dom, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(meta_dom, "_mark_age_trigger", lambda *a, **k: False)
    page.set_content(
        "<div><div>Age</div><div><div>35</div><div>65+</div></div>"
        "<div><span>Use as a suggestion</span><input id='sug' type='checkbox' checked></div></div>"
    )
    assert meta_dom.fill_audience_age(page, 35, 65) is True
    assert page.eval_on_selector("#sug", "el => el.checked") is False


def test_unreadable_age_does_not_count_as_verified(page, tmp_path, monkeypatch):
    page.set_content('<div>Advantage+ off</div><div>Age</div>')
    monkeypatch.setattr(meta_dom, '_fill_audience_age_controls', lambda p, a, b: True)
    assert meta_dom.fill_audience_age(page, 35, 65) is False
    row = [r for r in _rows(tmp_path) if r['step'] == 'audience_age'][-1]
    assert row['note'] == 'read_back_unavailable'
    assert row['diag'] == {'wanted': ['35', '65+'], 'seen': []}


def test_clock_18_is_untouched_by_the_integrated_age_setter(page, monkeypatch):
    """Exercise the real setter and mandatory readback together after extraction."""
    monkeypatch.setattr(meta_dom, '_ensure_manual_audience', lambda p: False)
    import src.fb_ads_rpa.screenshots as screenshots
    monkeypatch.setattr(screenshots, 'capture', lambda *a, **k: None)
    page.set_content("""
      <div>Start time</div><button id="clock" onclick="window.clockClicked=true">18</button>
      <div id="age-row"><div>Age</div>
        <button id="minimum" onclick="document.getElementById('options').style.display='block'">18</button>
        <button id="maximum" onclick="window.maximumClicked=true">65+</button>
      </div>
      <div role="listbox" id="options" style="display:none">
        <div role="option" onclick="document.getElementById('minimum').textContent='35';this.parentElement.style.display='none'">35</div>
      </div>
    """)
    assert meta_dom.fill_audience_age(page, 35, 65) is True
    assert meta_dom._age_triggers_read(page) == ['35', '65+']
    assert page.evaluate('window.clockClicked === undefined')
    assert page.evaluate('window.maximumClicked === undefined')


def test_the_age_field_is_read_the_same_way_the_click_finds_it(page, monkeypatch):
    """18.09 CL_ml Trading: «35 – 65+» на экране, а старое чтение отвечало «нет»."""
    monkeypatch.setattr(meta_dom, "_sleep_h", lambda *a, **k: None)
    # Живая страница (дамп 9b73f875e3b4): значения видно, а прежняя проверка
    # отвечала «нет» на обе границы.
    page.set_content(
        "<div><div>Age</div>"
        "<div><div><div><div><div><div><div><div>35</div><div>65+</div>"
        "</div></div></div></div></div></div></div>"
    )
    assert meta_dom._age_shows_value(page, "35") is True
    assert meta_dom._age_shows_value(page, "65+") is True


def test_wrong_age_is_not_confirmed_by_an_unrelated_budget(page, monkeypatch):
    page.set_content('<section><div>Age</div><span>18</span><span>65+</span></section>'
                     '<section><div>Daily budget</div><span>35</span></section>')
    monkeypatch.setattr(meta_dom, '_ensure_manual_audience', lambda p: False)
    monkeypatch.setattr(meta_dom, '_fill_audience_age_controls', lambda *a: True)
    assert meta_dom.fill_audience_age(page, 35, 65) is False


def test_age_reader_does_not_combine_incomplete_age_with_budget(page):
    page.set_content('<section><div><div>Age</div><span>35</span></div>'
                     '<div><div>Daily budget</div><span>65</span></div></section>')
    assert meta_dom._age_triggers_read(page) == []


def test_deep_age_label_and_fixed_field_still_read_the_two_boundaries(page):
    page.set_content('<section style="position:fixed;top:0"><div>' * 1
                     + '<div>' * 10 + '<span>Age</span>' + '</div>' * 10
                     + '<span>35</span><span>65+</span></div></section>')
    assert meta_dom._age_triggers_read(page) == ['35', '65+']
