# -*- coding: utf-8 -*-
"""Второй вариант основного текста (ACL-1344): робот жмёт «Add text option».

Разметка поля — настоящая: `fixtures/ads_manager/ad_copy_text_options_2026_09_25.html`
(блок креатива из снимка 36a1bbf663c9/02_ad_media_ready). Кнопки «Add text
option» в снимке НЕТ (снимок сделан до ввода текста) — её и поведение Meta
(новое поле-вариант под первым) тест добавляет сам, по скриншотам прогонов
21.09/22.09: кнопка «⊕ Add text option» под полем, ниже раздел «Advantage+
creative text generation». Что именно должен показать живой снимок, чтобы
заменить синтетику, — в README фикстур.

Проверяется в офлайн-Chromium:

* второй текст добавлен новым полем и сверен вместе с первым; набирается
  тем же `_type_human`, переносы строк на месте;
* повтор `fill_ad_copy` не добавляет третье поле;
* нет кнопки — текст 1 стоит, отказ второго мягкий и читается
  «second primary text not added»;
* раздел генерации Meta: его «Add text option» и его варианты не трогаются,
  включённая генерация выключается;
* объявление без второго текста не нажимает ничего нового.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import failure_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "ads_manager" / "ad_copy_text_options_2026_09_25.html"

# Настоящее раскрытие полей — до того, как фикстура `page` подменит его.
REAL_REVEAL = md._reveal_ad_copy_fields

TEXT1 = "Aprende a invertir paso a paso.\nSin experiencia previa."
TEXT2 = "Segunda opción del texto.\n\nRegístrate hoy — cupos limitados."
TITLE = "Regístrate ahora"

# Синтетика по скриншотам: кнопка под полем основного текста; нажатие кладёт
# новое пустое поле-вариант (копия первого) между полем и кнопкой.
_ADD_CONTROL_JS = r"""
() => {
  const host = document.querySelector('[data-surface$="/message_field"]');
  const slot = host.querySelector(':scope > div');
  const btn = document.createElement('div');
  btn.setAttribute('role', 'button');
  btn.tabIndex = 0;
  btn.id = 'add-option';
  btn.style.cssText = 'display:inline-block;padding:6px 10px;border:1px solid #999';
  btn.innerHTML = '<i></i><span>Add text option</span>';
  slot.appendChild(btn);
  window.__adds = 0;
  btn.addEventListener('click', () => {
    window.__adds++;
    const first = host.querySelector('[contenteditable="true"]').closest('[role="presentation"]');
    const box = first.cloneNode(true);
    const ed = box.querySelector('[contenteditable="true"]');
    ed.innerHTML = '<div data-contents="true"><div data-block="true"><div><span><br data-text="true"></span></div></div></div>';
    ed.setAttribute('data-option', String(window.__adds + 1));
    for (const a of ['data-rpa-field', 'data-rpa-pre', 'data-rpa-opt']) ed.removeAttribute(a);
    slot.insertBefore(box, btn);
  });
}
"""

# Раздел генерации Meta рядом с нашим полем: свой «Add text option», своё
# сгенерированное поле-вариант, переключатель. Ничего из этого не наше.
_GENERATION_JS = r"""
(on) => {
  const surface = document.querySelector('[data-surface$="/text_liquidity_toggle_field"]');
  // Вложенность как у Meta (обёртки div): поиск створки в
  // `disable_text_generation` поднимается на семь уровней и не должен
  // дотянуться до полей текста.
  const sec = document.createElement('div');
  sec.id = 'gen';
  sec.innerHTML = '<div><div><div><div><div id="gen-head"><div><div><span>Advantage+ creative text generation</span></div>'
    + '<div role="button" aria-expanded="true" style="width:16px;height:16px;display:inline-block"></div>'
    + '<div role="switch" id="gen-switch" aria-checked="' + (on ? 'true' : 'false') + '"'
    + ' style="width:24px;height:24px;display:inline-block;border:1px solid"></div></div></div>'
    + '<div><div contenteditable="true" aria-label="Tell people what your ad is about" id="meta-suggestion">'
    + 'Texto sugerido por Meta</div>'
    + '<div role="button" id="trap-add" style="display:inline-block;padding:4px">Add text option</div>'
    + '<div role="button" id="trap-use" style="display:inline-block;padding:4px">Add</div></div>'
    + '</div></div></div></div>';
  surface.parentElement.insertBefore(sec, surface);
  window.__trap = 0;
  for (const id of ['trap-add', 'trap-use']) {
    document.getElementById(id).addEventListener('click', () => { window.__trap++; });
  }
  document.getElementById('gen-switch').addEventListener('click', (e) =>
    e.target.setAttribute('aria-checked', e.target.getAttribute('aria-checked') === 'true' ? 'false' : 'true'));
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
def telemetry(monkeypatch, tmp_path):
    path = tmp_path / "telemetry.jsonl"
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", path)
    return path


@pytest.fixture
def typed(monkeypatch):
    """Настоящий `_type_human`, но с записью того, что через него прошло."""
    seen: list[str] = []
    real = md._type_human

    def spy(page, text):
        seen.append(text)
        real(page, text)

    monkeypatch.setattr(md, "_type_human", spy)
    return seen


@pytest.fixture
def page(browser, monkeypatch, telemetry, typed):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_human_jitter", lambda *a, **k: 0.0)
    monkeypatch.delenv("FB_RPA_HUMAN_TYPING", raising=False)
    monkeypatch.delenv("FB_RPA_TYPING_LEAD", raising=False)
    # Раскрытие полей подменено; путь с настоящим — в тестах ревью PR #346 ниже.
    monkeypatch.setattr(md, "_reveal_ad_copy_fields", lambda p, **kw: True)
    monkeypatch.setattr(md, "_await_creative_settled", lambda p, **kw: True)
    # Отказ «Add text option» снимает блок креатива — в тестах не на диск.
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: "")
    monkeypatch.setattr(md, "_focus_via_cdp_click", lambda p, loc: (loc.focus(), loc.element_handle())[1])
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.evaluate(
        "([x, y]) => { const e = document.elementFromPoint(x, y); if (e) e.click(); }", [x, y]))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    p.set_content("<!doctype html><html><body style='width:640px'>"
                  + FIXTURE.read_text(encoding="utf-8") + "</body></html>")
    try:
        yield p
    finally:
        ctx.close()


def _options(page) -> list[str]:
    """Тексты полей основного текста в `message_field`, по порядку."""
    return page.evaluate(
        "() => [...document.querySelectorAll('[data-surface$=\"/message_field\"] [contenteditable=true]')]"
        ".map(e => e.innerText)")


def _rows(path: Path, step: str) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("step") == step]


# ── основной путь ─────────────────────────────────────────────────────────

def test_the_real_fixture_has_one_primary_text_box_and_no_add_control(page):
    """Снимок честный: поле одно, кнопки нет — она синтетика теста."""
    state = md._text_options(page)
    assert len(state["boxes"]) == 1
    assert state["add_found"] is False


def test_second_text_is_added_as_a_new_option_and_both_are_verified(page, typed, telemetry):
    page.evaluate(_ADD_CONTROL_JS)
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True, report
    assert report["second_text"]["ok"] is True, report["second_text"]
    assert page.evaluate("window.__adds") == 1
    opts = _options(page)
    assert len(opts) == 2, opts
    assert md._copy_matches(opts[0], TEXT1), opts
    assert md._copy_matches(opts[1], TEXT2), opts
    # Переносы строк второго текста на месте, а не склеены в одну строку.
    assert "\n" in opts[1].strip(), repr(opts[1])
    # Тем же человеческим набором, что и первый текст.
    assert TEXT2 in typed and TEXT1 in typed
    assert page.input_value('[data-surface$="/headline_field"] input') == TITLE
    assert [r["ok"] for r in _rows(telemetry, "ad_copy_second_text")] == [True]


def test_a_second_pass_does_not_add_a_third_option(page):
    """Повтор `fill_ad_copy` (в `write_ad_copy` он бывает) видит свой второй
    текст на месте и сверяет его, а не жмёт «Add text option» ещё раз."""
    page.evaluate(_ADD_CONTROL_JS)
    assert md.fill_ad_copy(page, TITLE, TEXT1, {}, ad_body2=TEXT2) is True
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True, report
    assert report["second_text"]["ok"] is True, report["second_text"]
    assert report["second_text"].get("reused_box") is True
    assert page.evaluate("window.__adds") == 1
    opts = _options(page)
    assert len(opts) == 2 and md._copy_matches(opts[0], TEXT1) and md._copy_matches(opts[1], TEXT2), opts


def test_missing_add_control_is_a_soft_failure_and_text_one_stays(page, telemetry):
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True, "текст 1 не должен падать"
    assert report["second_text"]["ok"] is False
    assert report["second_text"]["why"] == "add_control_not_found"
    assert report["second_text"]["boxes"] == 1
    opts = _options(page)
    assert len(opts) == 1 and md._copy_matches(opts[0], TEXT1)
    rows = _rows(telemetry, "ad_copy_second_text")
    assert [(r["ok"], r.get("note")) for r in rows] == [(False, "add_control_not_found")]
    line = md.second_text_soft_fail("ad", "2046 - Text6", TEXT2, report)
    assert line == "ad_second_text:2046 - Text6 second primary text not added (add_control_not_found)"
    assert failure_policy.severity(line) == failure_policy.WARNING


def test_an_add_control_that_adds_nothing_is_a_soft_failure(page):
    page.evaluate(_ADD_CONTROL_JS)
    page.evaluate("() => { const b = document.getElementById('add-option');"
                  " b.replaceWith(b.cloneNode(true)); }")          # кнопка без действия
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True
    assert report["second_text"]["ok"] is False
    assert report["second_text"]["why"] == "option_box_not_added"
    assert len(_options(page)) == 1


def test_both_options_are_checked_after_the_second_is_typed(page, telemetry):
    """Второе поле приняло текст, а первое Meta при этом стёрла (перерисовка):
    сверка второго поля этого не видит — нужна итоговая сверка обоих."""
    page.evaluate(_ADD_CONTROL_JS)
    page.evaluate("""() => document.addEventListener('input', (e) => {
        if (!e.target.closest || !e.target.closest('[data-option]')) return;
        const first = document.querySelector('[data-surface$="/message_field"] [contenteditable=true]');
        first.innerHTML = '<div data-contents="true"><div data-block="true"><div><span><br data-text="true"></span></div></div></div>';
    }, true)""")
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True
    assert report["second_text"]["ok"] is False
    assert report["second_text"]["why"] == "options_not_verified"
    assert report["second_text"]["text1_ok"] is False and report["second_text"]["text2_ok"] is True
    assert [r["ok"] for r in _rows(telemetry, "ad_copy_second_text")] == [False]


# ── генерация текста Meta ─────────────────────────────────────────────────

def test_meta_generated_options_are_never_taken_and_generation_is_switched_off(page):
    page.evaluate(_ADD_CONTROL_JS)
    page.evaluate(_GENERATION_JS, True)
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True, report
    assert report["second_text"]["ok"] is True, report["second_text"]
    # Кнопки раздела генерации не нажимались — нажата только наша.
    assert page.evaluate("window.__trap") == 0
    assert page.evaluate("window.__adds") == 1
    # Сгенерированный вариант Meta не считается нашим полем и не тронут.
    assert page.inner_text("#meta-suggestion") == "Texto sugerido por Meta"
    assert page.get_attribute("#meta-suggestion", "data-rpa-field") is None
    # Генерация выключена и осталась выключенной после второго текста.
    assert page.get_attribute("#gen-switch", "aria-checked") == "false"
    assert report["generation_off"] is True
    assert report["generation_off_after_text2"] is None


def test_zone_blocks_a_lone_add_control_inside_meta_generation(page):
    """Единственная «Add text option» на странице — в разделе генерации:
    это не наша кнопка, второй текст — мягкий отказ, ничего не нажато."""
    page.evaluate(_GENERATION_JS, False)
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__trap") == 0


# ── объявления без второго текста ─────────────────────────────────────────

def test_an_ad_without_second_text_clicks_nothing_new(page, telemetry):
    page.evaluate(_ADD_CONTROL_JS)
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report) is True
    assert page.evaluate("window.__adds") == 0
    assert "second_text" not in report
    assert "generation_off_after_text2" not in report
    assert _rows(telemetry, "ad_copy_second_text") == []
    assert md.second_text_soft_fail("ad", "x", "", report) is None


def test_first_of_many_is_only_used_for_ads_with_a_second_text(page):
    """С двумя полями в `message_field` пометка по умолчанию отказывает
    (неоднозначно, как раньше); `first_of_many` берёт первое."""
    page.evaluate(_ADD_CONTROL_JS)
    page.click("#add-option")
    key, label = "message_field", md._AD_COPY_FIELDS["message_field"]
    assert md.mark_ad_copy_field(page, key, label, "body") is False
    assert md.mark_ad_copy_field(page, key, label, "body", first_of_many=True) is True
    marked = page.evaluate("() => document.querySelector('[data-rpa-field=body]').hasAttribute('data-option')")
    assert marked is False, "помечено добавленное поле, а не основное"


# ── политика отказов ──────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "ad_second_text:Ad 1 second primary text not added (add_control_not_found)",
    "ad2_second_text:Ad 2 second primary text not added (option_box_not_added)",
    "ad7_second_text",
])
def test_second_text_failure_is_a_warning_not_a_block(line):
    assert failure_policy.severity(line) == failure_policy.WARNING
    assert "unclassified" not in failure_policy.reason(line)
    blocking, warnings = failure_policy.split([line, "ad2_copy:Ad 2 has no text"])
    assert warnings == [line] and blocking == ["ad2_copy:Ad 2 has no text"]


# ── ревью PR #346 ─────────────────────────────────────────────────────────

def _real_reveal(page, monkeypatch):
    """Настоящий `_reveal_ad_copy_fields`; перезагрузка перехвачена и считается."""
    reloads: list[int] = []
    monkeypatch.setattr(md, "_reveal_ad_copy_fields", REAL_REVEAL)
    monkeypatch.setattr(md, "_scroll_until_mounted", lambda *a, **k: None)
    monkeypatch.setattr(md, "_recover_facebook_error_page", lambda *a, **k: None)
    monkeypatch.setattr(md, "_review_errors_visible", lambda *a, **k: False)
    monkeypatch.setattr(md, "_browser_addon_none_checked", lambda *a, **k: None)
    monkeypatch.setattr(page, "reload", lambda *a, **k: reloads.append(1))
    monkeypatch.setattr(page, "wait_for_load_state", lambda *a, **k: None)
    return reloads


def _failing(monkeypatch, key, calls_to_fail=10**6):
    """`mark_ad_copy_field` не находит поле `key` в первых `calls_to_fail` вызовах fill_ad_copy."""
    real_mark, real_fill = md.mark_ad_copy_field, md.fill_ad_copy
    calls = {"fill": 0}

    def fill(*a, **k):
        calls["fill"] += 1
        return real_fill(*a, **k)

    def mark(pg, k, label, m, *a, **kw):
        if k == key and m != "probe" and calls["fill"] <= calls_to_fail:
            return False
        return real_mark(pg, k, label, m, *a, **kw)

    monkeypatch.setattr(md, "mark_ad_copy_field", mark)
    monkeypatch.setattr(md, "fill_ad_copy", fill)


def test_write_ad_copy_retry_after_a_failed_headline_does_not_reload(page, monkeypatch):
    """HIGH ревью: заголовок не встал в первом проходе (как 25.09,
    `click_via_cdp_failed`) — повтор `write_ad_copy` с НАСТОЯЩИМ раскрытием
    полей проходит без перезагрузки редактора, текст 2 добавлен один раз."""
    page.evaluate(_ADD_CONTROL_JS)
    reloads = _real_reveal(page, monkeypatch)
    monkeypatch.setattr(md, "_heal_failed_component", lambda *a, **k: False)
    monkeypatch.setattr(md, "_back_on_the_ad", lambda *a, **k: None)
    monkeypatch.setattr(md, "_on_the_named_ad", lambda *a, **k: True)
    import src.fb_ads_rpa.browser as br
    monkeypatch.setattr(br, "ensure_editor_window", lambda *a, **k: True)
    _failing(monkeypatch, "headline_field", calls_to_fail=1)
    report: dict = {}
    assert md.write_ad_copy(page, TITLE, TEXT1, ad_name="x", body2=TEXT2, report=report) is True, report
    assert reloads == [], "повтор перезагрузил редактор Leads-объявления"
    assert report.get("retry") is True
    assert report["second_text"]["ok"] is True, report["second_text"]
    assert page.evaluate("window.__adds") == 1
    opts = _options(page)
    assert len(opts) == 2 and md._copy_matches(opts[0], TEXT1) and md._copy_matches(opts[1], TEXT2), opts


def test_a_pass_over_two_existing_options_does_not_reload(page, monkeypatch):
    """Два поля уже стоят (текст 2 добавлен раньше): настоящее раскрытие
    принимает их, если у объявления есть второй текст."""
    page.evaluate(_ADD_CONTROL_JS)
    reloads = _real_reveal(page, monkeypatch)
    assert md.fill_ad_copy(page, TITLE, TEXT1, {}, ad_body2=TEXT2) is True
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True, report
    assert reloads == []
    assert report["second_text"]["ok"] is True and report["second_text"].get("reused_box") is True
    assert md._reveal_ad_copy_fields(page, text_options=True) is True
    assert reloads == []


def test_a_failed_headline_adds_no_second_option(page, monkeypatch):
    page.evaluate(_ADD_CONTROL_JS)
    _failing(monkeypatch, "headline_field")
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is False
    assert page.evaluate("window.__adds") == 0, "текст 2 добавлен при упавшем заголовке"
    assert report["second_text"] == {"ok": False, "why": "headline_not_typed"}
    assert len(_options(page)) == 1


def test_a_failed_primary_text_adds_no_second_option(page, monkeypatch):
    page.evaluate(_ADD_CONTROL_JS)
    _failing(monkeypatch, "message_field")
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is False
    assert page.evaluate("window.__adds") == 0, "текст 2 добавлен без текста 1"
    assert report["second_text"] == {"ok": False, "why": "primary_text_not_typed"}


def test_an_ad_without_second_text_still_refuses_two_primary_boxes(page):
    """Без второго текста два поля в `message_field` — неоднозначность,
    как до ACL-1344: не печатаем ни в одно."""
    page.evaluate(_ADD_CONTROL_JS)
    page.click("#add-option")
    report: dict = {}
    assert md.fill_ad_copy(page, "", TEXT1, report) is False
    assert all(not o.strip() for o in _options(page)), _options(page)


def test_a_bare_add_outside_the_primary_text_block_is_not_ours(page):
    """Голое «Add» ищется только в блоке основного текста."""
    page.evaluate(_STRAY_ADD_JS)
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True
    assert report["second_text"]["why"] == "add_control_not_found"
    assert page.evaluate("window.__stray") == 0


def test_a_bare_add_inside_the_primary_text_block_is_used(page):
    page.evaluate(_ADD_CONTROL_JS)
    page.evaluate("() => { document.querySelector('#add-option span').textContent = 'Add'; }")
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True
    assert report["second_text"]["ok"] is True
    assert report["second_text"]["add_how"] == "bare_add_in_field"


_STRAY_ADD_JS = r"""
() => {
  const head = document.querySelector('[data-surface$="/headline_field"]').parentElement;
  const b = document.createElement('div');
  b.setAttribute('role', 'button'); b.id = 'stray-add'; b.textContent = 'Add';
  b.style.cssText = 'display:inline-block;padding:6px';
  head.appendChild(b);
  window.__stray = 0; b.addEventListener('click', () => window.__stray++);
}
"""

# Кнопка кладёт новое поле ВНЕ `message_field` и без подписи (так Meta тоже
# может сделать — разметки мы не видели), а раздел генерации между основным
# текстом и заголовком в тот же момент дорисовывает свой вариант. Поле берётся
# запасным ходом по положению.
_OUTSIDE_BOX_JS = r"""
(withGeneration) => {
  const host = document.querySelector('[data-surface$="/message_field"]');
  const wrap = host.parentElement;
  const btn = document.createElement('div');
  btn.setAttribute('role', 'button'); btn.id = 'add-option';
  btn.style.cssText = 'display:inline-block;padding:6px 10px;border:1px solid #999';
  btn.textContent = 'Add text option';
  wrap.appendChild(btn);
  let gen = null;
  if (withGeneration) {
    gen = document.createElement('div');
    gen.innerHTML = '<div><div><span>Advantage+ creative text generation</span></div><div class="slot"></div></div>';
    wrap.parentElement.insertBefore(gen, wrap.nextSibling);
  }
  window.__adds = 0;
  btn.addEventListener('click', () => {
    window.__adds++;
    const box = document.createElement('div');
    box.innerHTML = '<div contenteditable="true" id="outside-option" style="min-height:40px;border:1px solid"></div>';
    wrap.parentElement.insertBefore(box, wrap.nextSibling);
    if (gen) {
      gen.querySelector('.slot').innerHTML =
        '<div contenteditable="true" id="gen-option" style="min-height:40px">Texto generado</div>';
    }
  });
}
"""


def test_a_new_box_outside_the_field_is_taken_by_position_not_the_generated_one(page):
    page.evaluate(_OUTSIDE_BOX_JS, True)
    report: dict = {}
    assert md.fill_ad_copy(page, TITLE, TEXT1, report, ad_body2=TEXT2) is True
    assert report["second_text"]["ok"] is True, report["second_text"]
    assert md._copy_matches(page.inner_text("#outside-option"), TEXT2)
    assert page.inner_text("#gen-option") == "Texto generado", "в вариант Meta напечатан наш текст"
    assert page.get_attribute("#gen-option", "data-rpa-field") is None


_HIDDEN_HEADLINE_JS = r"""
() => {
  const host = document.querySelector('[data-surface$="/message_field"]');
  const btn = document.createElement('div');
  btn.setAttribute('role', 'button'); btn.id = 'add-option';
  btn.style.cssText = 'display:inline-block;padding:6px';
  btn.textContent = 'Add text option';
  host.parentElement.appendChild(btn);
  document.querySelector('[data-surface$="/headline_field"]').parentElement.style.display = 'none';
  btn.addEventListener('click', () => {
    const old = document.querySelector('[data-surface$="/link_description_field"] textarea');
    const t = document.createElement('textarea'); t.id = 'redrawn-description';
    old.replaceWith(t);
  });
}
"""


def test_without_a_visible_headline_the_position_fallback_takes_nothing(page):
    """Нижней границы нет (заголовок не виден) — перерисованное описание
    ниже не должно сойти за вариант 2."""
    page.evaluate(_HIDDEN_HEADLINE_JS)
    report: dict = {}
    assert md.fill_ad_copy(page, "", TEXT1, report, ad_body2=TEXT2) is True
    assert report["second_text"]["why"] == "option_box_not_added"
    assert page.input_value("#redrawn-description") == ""


def test_optimize_text_per_person_state_is_recorded_not_changed(page, telemetry):
    page.evaluate(_ADD_CONTROL_JS)
    before = page.inner_text('[data-surface$="/text_liquidity_toggle_field"]')
    assert md.fill_ad_copy(page, TITLE, TEXT1, {}, ad_body2=TEXT2) is True
    row = _rows(telemetry, "ad_copy_second_text")[-1]
    assert row["diag"]["optimize_text_per_person"] == "Disabled"
    assert page.inner_text('[data-surface$="/text_liquidity_toggle_field"]') == before
