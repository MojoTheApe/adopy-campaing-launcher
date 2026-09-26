# -*- coding: utf-8 -*-
"""Раскладка «Suggest an audience» карточки Audience (ACL-1324, живой прогон
26.09, Facebook_6540, Colombia, CO_bnc_*).

Что было:

* CO_bnc_All_Video_FX трижды (ed5426c321d5, 97fc8899a15c, 97370c743f44)
  отказал на `further_limit_reach_click`: строка-кнопка с шестерёнкой
  «Further limit the reach of your ads» (справа «Save audience») стояла на
  экране, а клик Playwright по тексту пять раз падал по таймауту проверок
  действия — 28–47 с, окно «Reach your preferred audiences» не открылось.
  Живую причину таймаута снимок не сохранил; здесь она смоделирована слоем
  поверх строки, в который упирается проверка попадания Playwright.
* CO_bnc_All_Video_GMB (7cfdc772376d) прошёл Further limit, но кончил с
  «Advantage+ on», возрастом «35 - 65+ Your suggestion», пустыми языками и
  `age: []` в итоговой проверке: галочку «Use as a suggestion» снимали по
  уже свёрнутому полю, где её нет.

Разметка — fixtures/ads_manager/audience_suggest_layout_2026_09_26.html
(фрагменты `suggest_classic`, `suggest_fx`, `controls_suggestion_on`,
`languages_open_empty`, `controls_off`, `age_open_suggestion`). Поведение
Meta (окно, раскрытие строки Age, галочка, подсказки языков) эмулируется
здесь же.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

FIX = (Path(__file__).parent / "fixtures" / "ads_manager"
       / "audience_suggest_layout_2026_09_26.html").read_text(encoding="utf-8")
FRAG = {m.group(1): m.group(2).strip() for m in re.finditer(
    r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", FIX, re.S)}

# Правая панель редактора: кнопки рекомендаций Advantage+, которые робот не
# жмёт никогда (решение владельца).
_RIGHT_PANEL = """
<aside style="margin-top:20px">
  <div role="heading">Advantage+ leads campaign</div><div>On</div>
  <div role="button" id="apply-now">Apply now</div>
  <div role="button" id="view-in-ad-set">View in ad set</div>
  <div role="button" id="use-recommended">Use recommended setup</div>
</aside>
"""

_DIALOG = """
<div role="dialog" style="position:fixed;top:40px;left:40px;width:600px;background:#fff;z-index:20">
  <div role="heading">Reach your preferred audiences</div>
  <div><div role="radio" aria-checked="true"></div><div><span>Stay in current setup</span></div></div>
  <div><div role="radio" aria-checked="false"></div><div><span>Further limit the reach of your ads</span>
       <div>Set the controls to limit who can see your ads.</div></div></div>
  <div><div role="radio" aria-checked="false"></div><div><span>Switch to original audience options</span></div></div>
  <div role="button">Continue</div>
</div>
"""

# Как у Meta: нажатие строки-кнопки с шестерёнкой открывает окно; выбор
# варианта ставит его переключатель. Все нажатия пишутся в window.clicked.
_META_FURTHER = r"""
(dialog) => {
  window.clicked = [];
  const norm = s => (s || '').replace(/[\s​]+/g, ' ').trim();
  document.addEventListener('click', (e) => {
    const ctl = e.target.closest('[role=button], [role=radio], button, a, label');
    window.clicked.push(norm((ctl || e.target).textContent).slice(0, 60));
    const row = e.target.closest('[data-surface$="toggle-automation-button"]');
    if (row && !document.querySelector('[role=dialog]')) document.body.insertAdjacentHTML('beforeend', dialog);
    const opt = e.target.closest('[role=dialog] > div');
    if (opt && opt.querySelector('[role=radio]')) {
      document.querySelectorAll('[role=dialog] [role=radio]').forEach(r => r.setAttribute('aria-checked', 'false'));
      opt.querySelector('[role=radio]').setAttribute('aria-checked', 'true');
    }
  }, true);
}
"""

# Слой поверх строки: проверка попадания Playwright в него упирается, клик
# по тексту падает по таймауту — как живой клик 26.09.
_COVER_ROW = r"""
() => {
  const row = document.querySelector('[data-surface$="toggle-automation-button"]');
  row.scrollIntoView({block: 'center'});
  const r = row.getBoundingClientRect();
  const c = document.createElement('div');
  c.id = 'cover';
  c.style.cssText = `position:absolute;left:${r.left + scrollX}px;top:${r.top + scrollY}px;`
    + `width:${r.width}px;height:${r.height}px;z-index:9`;
  document.body.appendChild(c);
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
def page(browser, monkeypatch):
    rows = []
    monkeypatch.setattr(md, "_sleep_h", lambda base, *a, **k: time.sleep(min(base, 0.3)))
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **k: rows.append({"step": step, "ok": ok, **k}))
    ctx = browser.new_context(viewport={"width": 1200, "height": 800})
    ctx.route("**/*", lambda r: r.abort() if not r.request.url.startswith("data:") else r.continue_())
    p = ctx.new_page()
    p.rows = rows
    try:
        yield p
    finally:
        ctx.close()


def _card(fragment: str, *, filler_px: int = 0) -> str:
    # Сверху — «пустой» редактор нужной высоты: строка стоит ниже экрана,
    # как на снимке ed5426c321d5 (Audience у нижнего края).
    return (f'<div style="height:{filler_px}px">Budget &amp; schedule</div>'
            f'<main style="width:640px">{FRAG[fragment]}</main>{_RIGHT_PANEL}')


def _clicked(page) -> list[str]:
    return page.evaluate("window.clicked || []")


def _never_the_forbidden_buttons(page):
    forbidden = re.compile(r"^(Save audience|Apply now|Use recommended setup|"
                           r"Switch to recommended setup|View in ad set)$")
    assert not [c for c in _clicked(page) if forbidden.match(c)], _clicked(page)


# ── Further limit the reach: обе раскладки ─────────────────────────────────

@pytest.mark.parametrize("fragment", ["suggest_classic", "suggest_fx"])
def test_the_gear_row_opens_the_dialog_in_both_layouts(page, fragment):
    page.set_content(_card(fragment, filler_px=1400))
    page.evaluate(_META_FURTHER, _DIALOG)
    assert md._open_further_limit(page) is True
    assert md._reach_dialog_open(page)
    assert _clicked(page) == ["Further limit the reach of your ads"]
    row = [r for r in page.rows if r["step"] == "further_limit_row"][-1]
    assert row["ok"] is True and row["note"] == "cdp"
    assert row["diag"]["surface"] == "lib:targeting-individual-setting-toggle-automation-button"
    # Дальше — как в классическом пути: вариант «Further limit…» в окне.
    assert md._pick_further_limit_radio(page) is True
    assert page.evaluate("""() => [...document.querySelectorAll('[role=dialog] [role=radio]')]
        .map(r => r.getAttribute('aria-checked'))""") == ["false", "true", "false"]
    _never_the_forbidden_buttons(page)


def test_a_covered_gear_row_is_still_opened_by_its_own_element(page):
    """Живой отказ 26.09: строка на экране, а клик Playwright упирается и
    падает по таймауту. Строка жмётся сама по себе — не «Save audience»
    рядом и не то, что её накрыло."""
    page.set_content(_card("suggest_fx", filler_px=1400))
    page.evaluate(_META_FURTHER, _DIALOG)
    page.evaluate(_COVER_ROW)
    started = time.monotonic()
    assert md._open_further_limit(page) is True
    assert time.monotonic() - started < 15
    assert md._reach_dialog_open(page)
    row = [r for r in page.rows if r["step"] == "further_limit_row"][-1]
    assert row["note"] == "dom" and row["diag"]["hit"] is False
    _never_the_forbidden_buttons(page)


def test_without_the_gear_row_nothing_else_is_clicked(page):
    """Строки нет (только «Save audience») — ни одного нажатия, честный отказ."""
    page.set_content(_card("suggest_fx").replace("Further limit the reach of your ads", ""))
    page.evaluate(_META_FURTHER, _DIALOG)
    assert md._open_further_limit(page) is False
    assert _clicked(page) == []
    assert page.rows[-1]["step"] == "further_limit_row" and page.rows[-1]["note"] == "not_opened"


def test_two_gear_rows_are_not_guessed_between(page):
    page.set_content(_card("suggest_fx") + FRAG["suggest_classic"])
    page.evaluate(_META_FURTHER, _DIALOG)
    assert md._open_further_limit(page) is False
    assert _clicked(page) == []
    assert page.rows[-1]["diag"]["found"] == 2


# Ревью PR #328 (F4): фраза лежит внутри одной большой кнопки вместе с «Save
# audience», и центр этой кнопки приходится на «Save audience». Такая кнопка —
# не строка шестерёнки: её нажатие создало бы в кабинете сохранённую
# аудиторию. Разметка строк — из `suggest_fx` (те же data-surface), обёрнутых
# в общую role=button.
_MIXED_ROW = """
<main style="width:640px">
  <div role="heading" aria-level="3">Audience</div>
  <div aria-busy="false" role="button" tabindex="0" id="big" style="display:flex;width:600px">
    <div style="width:160px"><div role="presentation"><i></i></div>
      <div>Further limit the reach of your ads</div></div>
    <div id="save-aud" style="flex:1;text-align:center" aria-busy="false" role="button" tabindex="0"
      data-surface="/am/editor_drawer/editor:ad_set/adset_targeting_static_extended_pgd_section/lib:mtc-save-audience-button">
      <span><div><div><div>Save audience</div></div></div></span></div>
  </div>
</main>
"""


def test_the_phrase_inside_a_save_audience_button_is_not_the_gear_row(page):
    page.set_content(_MIXED_ROW + _RIGHT_PANEL)
    page.evaluate("""() => { window.clicked = [];
        document.addEventListener('click', (e) => window.clicked.push(
            e.target.closest('#save-aud') ? 'Save audience' : 'other'), true); }""")
    # Центр большой кнопки — на «Save audience»: мутация без проверки попала бы туда.
    assert page.evaluate("""() => { const r = document.getElementById('big').getBoundingClientRect();
        return !!document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2).closest('#save-aud'); }""")
    assert page.evaluate(md._FURTHER_LIMIT_ROW_JS)["found"] == 0
    assert page.evaluate("document.querySelectorAll('[data-acl-further-limit]').length") == 0
    assert md._open_further_limit(page) is False
    assert _clicked(page) == []
    row = page.rows[-1]
    assert row["step"] == "further_limit_row" and row["note"] == "not_opened"
    assert row["diag"]["found"] == 0 and row["diag"]["mixed"] == 1


# ── после Further limit: возраст — условие, бейдж «Advantage+ off» ─────────

# Meta: строка Age (кнопка aria-expanded=false) раскрывает поле с границами и
# галочкой «Use as a suggestion» (стоит); снятие галочки превращает возраст в
# условие — бейдж аудитории «Advantage+ off», метка «Your suggestion» уходит.
_META_AGE = r"""
([openAge, flips]) => {
  window.clicked = window.clicked || [];
  const norm = s => (s || '').replace(/[\s​]+/g, ' ').trim();
  const tpl = document.createElement('div');
  tpl.innerHTML = openAge;
  const open = tpl.querySelector('[data-surface$="age_editor_wrapper"]');
  const cb = open.querySelector('input[type=checkbox]');
  cb.setAttribute('aria-checked', 'true');
  cb.checked = true;
  const badge = (on) => document.querySelectorAll('#advantage-state-audience-section-header div, '
      + '#advantage-state-audience-section-header [role=status]').forEach(el => {
    if (el.children.length === 0 && /Advantage\+ (on|off)/.test(el.textContent))
      el.textContent = on ? 'Advantage+ on' : 'Advantage+ off';
  });
  document.addEventListener('click', (e) => {
    const ctl = e.target.closest('[role=button], button, a, label');
    window.clicked.push(norm((ctl || e.target).textContent).slice(0, 60));
    const wrap = e.target.closest('[data-surface$="age_editor_wrapper"]');
    const row = e.target.closest('[role=button][aria-expanded="false"]');
    if (wrap && row && !wrap.querySelector('input[type=checkbox]')) {
      wrap.replaceWith(open.cloneNode(true));
      return;
    }
    const box = e.target.closest('label');
    const input = box && box.querySelector('input[type=checkbox]');
    if (input && wrap) {
      e.preventDefault();
      const on = input.getAttribute('aria-checked') !== 'true';
      input.setAttribute('aria-checked', on ? 'true' : 'false');
      input.checked = on;
      if (flips) badge(on);
    }
  }, true);
}
"""


def _age_card(page, fragment: str, *, flips: bool = True):
    page.set_content(_card(fragment))
    page.evaluate(_META_AGE, [FRAG["age_open_suggestion"], flips])


def _checkbox(page):
    return page.evaluate("""() => { const c = document.querySelector('[data-surface$="age_editor_wrapper"] input[type=checkbox]');
        return c ? c.getAttribute('aria-checked') : null; }""")


def test_a_collapsed_suggestion_age_is_opened_unchecked_and_the_badge_goes_off(page):
    """7cfdc772376d/02: «35 - 65+ Your suggestion», бейдж «on», галочки на
    экране нет. Строка Age раскрывается, галочка снимается — «off»."""
    _age_card(page, "controls_suggestion_on")
    assert md._audience_badge_on(page) is True
    assert md.verify_audience_advantage_off(page) is True
    assert md._audience_badge_on(page) is False
    assert _checkbox(page) == "false"
    row = [r for r in page.rows if r["step"] == "advantage_plus_audience"][-1]
    assert row["ok"] is True and row["note"] == "off"
    assert row["diag"]["retried_suggestion"] is True
    assert row["diag"]["your_suggestion"] is True and row["diag"]["age_row_opened"] is True
    assert row["diag"]["unchecked"] is True
    assert row["diag"]["age"] == ["35", "65+"]
    _never_the_forbidden_buttons(page)


def test_a_suggestion_that_meta_keeps_on_fails_with_the_age_read(page):
    _age_card(page, "controls_suggestion_on", flips=False)
    assert md.verify_audience_advantage_off(page) is False
    row = [r for r in page.rows if r["step"] == "advantage_plus_audience"][-1]
    assert row["note"] == "still_on" and row["diag"]["age"] == ["35", "65+"]
    _never_the_forbidden_buttons(page)


def test_the_suggestion_checkbox_is_found_only_inside_the_age_field(page):
    """Открытое поле Age из живой разметки: галочка найдена по своей подписи
    (`aria-labelledby` → «Use as a suggestion»), и она одна."""
    page.set_content(_card("age_open_suggestion"))
    got = md._age_suggestion_read(page)
    assert got["checkbox"] is False and got["count"] == 1
    assert md._uncheck_age_suggestion(page) is True
    assert md._age_triggers_read(page) == ["35", "65+"]


def test_controls_mode_reads_off_with_the_age_and_the_language_from_the_summary(page):
    """8c6496e50815/01 (тот же аккаунт, удачный прогон): карточка свёрнута —
    «Advantage+ off», «35 - 65+», «Spanish (All)». Проверка проходит сразу, а
    возраст и язык читаются из сводок."""
    page.set_content(_card("controls_off"))
    page.evaluate("window.clicked = []; document.addEventListener('click', e => window.clicked.push(e.target.textContent), true)")
    assert md.verify_audience_advantage_off(page) is True
    row = [r for r in page.rows if r["step"] == "advantage_plus_audience"][-1]
    assert row["diag"] == {"retried_suggestion": False, "age": ["35", "65+"]}
    assert md._languages_read(page, ["Spanish"]) == ["Spanish (All)"]
    assert md.fill_audience_languages(page, ["Spanish"]) is True
    assert page.rows[-1]["step"] == "languages" and page.rows[-1]["note"] == "already_selected"
    assert _clicked(page) == []


def test_the_age_summary_is_read_but_does_not_count_as_an_open_field(page):
    """`_age_triggers_read` остаётся «только открытое поле» — по нему
    `_open_collapsed_age_row` решает, раскрыто ли поле."""
    page.set_content(_card("controls_suggestion_on"))
    assert md._age_triggers_read(page) == []
    assert md._age_range_read(page) == ["35", "65+"]


# ── языки: открытое пустое поле, подсказки приходят с задержкой ────────────

# Meta: ввод в настоящий <input> токенайзера показывает подсказки через
# `delay` мс (вкладка тормозила: tab_frozen минутой раньше); выбор подсказки
# ставит чип над полем и чистит ввод.
_META_LANG = r"""
(delay) => {
  window.clicked = window.clicked || [];
  const inp = document.querySelector('input[placeholder="Search languages"]');
  let timer = null;
  inp.addEventListener('input', () => {
    clearTimeout(timer);
    document.querySelectorAll('#lang-list').forEach(e => e.remove());
    if (!inp.value) return;
    timer = setTimeout(() => {
      const list = document.createElement('ul');
      list.id = 'lang-list';
      list.setAttribute('role', 'listbox');
      for (const name of ['Spanish (Spain)', 'Spanish (Mexico)', 'Spanish (All)']) {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.textContent = name;
        li.addEventListener('click', () => {
          window.clicked.push(name);
          const chip = document.createElement('span');
          chip.textContent = name;
          document.getElementById('LANGUAGE').prepend(chip);
          inp.value = '';
          list.remove();
        });
        list.appendChild(li);
      }
      document.getElementById('LANGUAGE').appendChild(list);
    }, delay);
  });
}
"""


def test_late_language_suggestions_are_waited_for_and_the_all_variant_picked(page):
    page.set_content(_card("languages_open_empty"))
    page.evaluate(_META_LANG, 2500)
    assert md._languages_selected(page, ["Spanish"]) == []
    assert md.fill_audience_languages(page, ["Spanish"]) is True
    assert _clicked(page) == ["Spanish (All)"]
    assert md._languages_read(page, ["Spanish"]) == ["Spanish (All)"]
    _never_the_forbidden_buttons(page)


def test_the_real_input_is_used_so_an_unmatched_search_is_cleared(page, monkeypatch):
    """Плейсхолдер «Search languages» стоит и на обёртке-<span>, и на <input>.
    Подходящей подсказки нет — шаг отказывает, а набранное не остаётся в
    поиске (открытая выпадашка накрыла бы соседние поля). Очистка по
    обёртке-<span> падала, и текст оставался."""
    monkeypatch.setattr(md, "LANGUAGE_OPTIONS_WAIT_S", 1.0)
    page.set_content(_card("languages_open_empty"))
    assert page.locator("[placeholder='Search languages']").count() == 2
    page.evaluate(_META_LANG, 0)
    assert md.fill_audience_languages(page, ["Catalan"]) is False
    assert page.eval_on_selector("input[placeholder='Search languages']", "e => e.value") == ""
    assert _clicked(page) == []


# ── разметка фикстуры ──────────────────────────────────────────────────────

def test_the_fixture_carries_no_account_names_or_interests():
    assert "CO_bnc" not in FIX and "act=" not in FIX
    for real in ("Online gambling", "Forex", "FxPro", "InstaForex"):
        assert real not in FIX
    assert "js_" not in FIX
    json.dumps(FRAG)  # фрагменты — обычный текст
