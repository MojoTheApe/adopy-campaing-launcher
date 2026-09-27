# -*- coding: utf-8 -*-
"""«Further limit the reach of your ads»: строка, которую редактор ещё не
разложил, и терпимость как до ACL-1324 (ACL-1332).

Живой отказ 27.09 (Facebook_6540, f2272afec9fb): карточка Audience у нижнего
края окна, строка-кнопка с шестерёнкой ниже экрана. Редактор Meta не
раскладывает карточку за экраном — у строки размер 0×0, и разметочный путь
ACL-1324 её отбрасывал (`found:0` в трёх прогонах из трёх). Урезанный текстовый
путь (3 попытки по 3 с) крутил колесо там, где стояла мышь Playwright, — не над
редактором — и не дотянул: жёсткий `further_limit_reach`, повтор всей
кампании, лишний черновик.

Здесь колонка редактора — свой прокручиваемый блок, справа панель
рекомендаций; мышь оставлена над панелью, как после CDP-кликов. Раскладку
Meta эмулирует IntersectionObserver: строка 0×0 (или display:none), пока
карточка не попала в видимую часть редактора; в другом случае карточка вовсе
не смонтирована, пока редактор не прокрутят.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_suggest_an_audience_layout import (  # noqa: E402,F401
    FRAG, _DIALOG, _META_FURTHER, _RIGHT_PANEL, _clicked, _never_the_forbidden_buttons,
    browser, page)

_ROW = '[data-surface$="toggle-automation-button"]'


def _editor(card: str, *, filler_px: int, tail_px: int = 900) -> str:
    # Страница сама не прокручивается: прокручивается колонка редактора, как у
    # Meta. Колесо над правой панелью редактор не двигает.
    return f"""
<style>
  html, body {{ margin: 0; height: 100%; overflow: hidden; }}
  #wrap {{ display: flex; height: 100vh; }}
  #editor {{ width: 700px; height: 100vh; overflow-y: auto; }}
  #side {{ width: 480px; height: 100vh; overflow: hidden; }}
  .unlaid-size {_ROW} {{ width: 0 !important; height: 0 !important; max-width: 0 !important;
     max-height: 0 !important; min-width: 0 !important; min-height: 0 !important;
     overflow: hidden !important; padding: 0 !important; border: 0 !important; }}
  .unlaid-none {_ROW} {{ display: none !important; }}
</style>
<div id="wrap">
  <div id="editor">
    <div style="height:{filler_px}px">Budget &amp; schedule</div>
    {card}
    <div style="height:{tail_px}px"></div>
  </div>
  <div id="side">{_RIGHT_PANEL}</div>
</div>
"""


# Строка раскладывается, только когда карточка попала в видимую часть редактора.
_LAY_OUT_ON_SIGHT = r"""
() => {
  const editor = document.getElementById('editor');
  const card = document.getElementById('card');
  new IntersectionObserver((es) => es.forEach(e => {
    if (e.isIntersecting) card.classList.remove('unlaid-size', 'unlaid-none');
  }), { root: editor }).observe(card);
}
"""

# Раздел аудитории стоит пустым местом; карточка монтируется, когда раздел
# попал в видимую часть редактора.
_MOUNT_ON_SIGHT = r"""
(html) => {
  const editor = document.getElementById('editor');
  const sec = document.getElementById('campaignTargetingStaticExtendedPGDSection');
  new IntersectionObserver((es) => es.forEach(e => {
    if (e.isIntersecting && !sec.children.length) sec.innerHTML = html;
  }), { root: editor }).observe(sec);
}
"""

# Карточка монтируется, только когда редактор прокрутили дальше 600 px.
_MOUNT_ON_SCROLL = r"""
(html) => {
  const editor = document.getElementById('editor');
  const slot = document.getElementById('slot');
  editor.addEventListener('scroll', () => {
    if (editor.scrollTop >= 600 && !slot.children.length) slot.innerHTML = html;
  });
}
"""


def _row_size(page):
    return page.evaluate(f"""() => {{ const r = document.querySelector('{_ROW}').getBoundingClientRect();
        return [r.width, r.height]; }}""")


def _park_mouse_over_the_side_panel(page):
    # После CDP-кликов прошлых шагов мышь Playwright может стоять где угодно;
    # здесь — над правой панелью рекомендаций.
    page.mouse.move(1100, 300)


def _last_row(page):
    return [r for r in page.rows if r["step"] == "further_limit_row"][-1]


# ── строка 0×0 до прокрутки ────────────────────────────────────────────────

@pytest.mark.parametrize("unlaid", ["unlaid-size", "unlaid-none"])
def test_an_unlaid_gear_row_is_centred_measured_and_clicked(page, unlaid):
    page.set_content(_editor(f'<main id="card" class="{unlaid}" style="width:640px">{FRAG["suggest_fx"]}</main>',
                             filler_px=1400))
    page.evaluate(_LAY_OUT_ON_SIGHT)
    page.evaluate(_META_FURTHER, _DIALOG)
    _park_mouse_over_the_side_panel(page)
    if unlaid == "unlaid-size":
        assert _row_size(page) == [0, 0]
    assert md._open_further_limit(page) is True
    assert md._reach_dialog_open(page)
    assert _clicked(page) == ["Further limit the reach of your ads"]
    row = _last_row(page)
    # Разметочный путь с первой попытки: строку нашли 0×0, прокрутили в
    # центр, перемерили и нажали доверенным кликом — не текстом.
    assert row["ok"] is True and row["note"] == "cdp", row
    assert row["diag"]["attempts"] == 1 and row["diag"]["unlaid"] is True
    assert row["diag"]["laid"] is True and row["diag"]["hit"] is True
    assert row["diag"]["surface"] == "lib:targeting-individual-setting-toggle-automation-button"
    _never_the_forbidden_buttons(page)


def test_the_row_search_reports_an_unlaid_row_that_stays_unlaid(page):
    """Строка так и не разложилась — найдена одна, но не нажата: `laid:false`."""
    page.set_content(_editor(f'<main id="card" class="unlaid-size" style="width:640px">{FRAG["suggest_fx"]}</main>',
                             filler_px=1400))
    got = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert got["found"] == 1 and got["laid"] is False and got["was_laid"] is False
    assert "x" not in got and "hit" not in got


def test_a_not_yet_mounted_audience_section_is_brought_into_view(page):
    """Строки нет вовсе, а раздел аудитории есть пустым местом: раздел в
    видимую часть — карточка монтируется — строка нажата с первой попытки."""
    page.set_content(_editor('<div id="campaignTargetingStaticExtendedPGDSection" style="min-height:500px"></div>',
                             filler_px=3000))
    page.evaluate(_MOUNT_ON_SIGHT, f'<main style="width:640px">{FRAG["suggest_fx"]}</main>')
    page.evaluate(_META_FURTHER, _DIALOG)
    _park_mouse_over_the_side_panel(page)
    assert page.evaluate(f"document.querySelectorAll('{_ROW}').length") == 0
    assert md._open_further_limit(page) is True
    row = _last_row(page)
    assert row["note"] == "cdp" and row["diag"]["attempts"] == 1, row
    assert _clicked(page) == ["Further limit the reach of your ads"]
    _never_the_forbidden_buttons(page)


# ── колесо — над колонкой редактора ────────────────────────────────────────

def test_the_wheel_turns_over_the_editor_column_not_where_the_mouse_was_left(page, monkeypatch):
    """f2272afec9fb: колесо крутилось там, где стояла мышь Playwright, и
    редактор не двигался. Карточка здесь монтируется, только когда редактор
    прокрутили, — найти её можно, лишь крутя колесо над ним."""
    monkeypatch.setattr(md, "FURTHER_LIMIT_TEXT_TIMEOUT_MS", 800)   # отсутствующий текст — быстрее
    page.set_content(_editor('<div id="slot"></div>', filler_px=1400))
    page.evaluate(_MOUNT_ON_SCROLL, f'<main style="width:640px">{FRAG["suggest_fx"]}</main>')
    page.evaluate(_META_FURTHER, _DIALOG)
    _park_mouse_over_the_side_panel(page)
    assert md._open_further_limit(page) is True
    assert page.evaluate("document.getElementById('editor').scrollTop") > 0
    row = _last_row(page)
    assert row["ok"] is True and row["diag"]["attempts"] > 1, row
    assert _clicked(page) == ["Further limit the reach of your ads"]
    _never_the_forbidden_buttons(page)


# ── терпимость как до ACL-1324 ─────────────────────────────────────────────

def test_the_text_fallback_is_as_patient_as_before_acl_1324():
    """До ACL-1324 (08c9364): 5 попыток, по 5 с на прокрутку и клик; окно
    затем ждали 0,6 + 1,5 с. ACL-1324 урезал до 3 × 3 с и окна за 2 с."""
    assert md.FURTHER_LIMIT_ATTEMPTS == 5
    assert md.FURTHER_LIMIT_TEXT_TIMEOUT_MS == 5000
    assert md.FURTHER_LIMIT_OPEN_WAIT_S == 3.0


def _fake_page(monkeypatch, *, failures: int, dialog_after_polls: int):
    """Классическая раскладка без строки-кнопки: разметка ничего не находит,
    текст первые `failures` раз не прокручивается (не разложен), окно
    приходит через `dialog_after_polls` опросов после клика."""
    state = {"clicked": False, "polls": 0, "scroll_timeouts": [], "click_timeouts": []}
    rows: list[dict] = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", MagicMock())
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **k: rows.append({"step": step, "ok": ok, **k}))
    monkeypatch.setattr(md, "_audience_already_manual", lambda p: False)

    def dialog_open(p):
        if state["clicked"]:
            state["polls"] += 1
        return state["clicked"] and state["polls"] >= dialog_after_polls

    monkeypatch.setattr(md, "_reach_dialog_open", dialog_open)
    page = MagicMock()
    page.evaluate.side_effect = lambda js, *a: (
        {"found": 0} if js == md._FURTHER_LIMIT_ROW_JS else
        [1200, 800] if "innerWidth" in js else None)
    loc = MagicMock()
    loc.first = loc

    def scroll(timeout=None):
        state["scroll_timeouts"].append(timeout)
        if len(state["scroll_timeouts"]) <= failures:
            raise TimeoutError("not visible")

    def click(timeout=None):
        state["click_timeouts"].append(timeout)
        state["clicked"] = True

    loc.scroll_into_view_if_needed.side_effect = scroll
    loc.click.side_effect = click
    page.get_by_text.return_value = loc
    return page, state, rows


def test_the_fifth_text_attempt_still_counts_and_each_waits_five_seconds(monkeypatch):
    """Классика, которой нужны 4+ попытки (CL/CR/GT/SA до 27.09): строка
    разложилась к пятой — шаг проходит, а не отказывает жёстко."""
    page, state, rows = _fake_page(monkeypatch, failures=4, dialog_after_polls=1)
    assert md._open_further_limit(page) is True
    assert state["scroll_timeouts"] == [5000] * 5
    assert state["click_timeouts"] == [5000]
    assert rows[-1]["note"] == "text" and rows[-1]["diag"]["attempts"] == 5
    # Между попытками — колесо, и каждый раз мышь сначала над редактором.
    wheel = [c for c in page.mouse.method_calls if c[0] in ("move", "wheel")]
    assert wheel == [("move", (540, 400), {}), ("wheel", (0, 400), {})] * 4


def test_the_dialog_is_waited_for_three_seconds_after_the_click(monkeypatch):
    """Окно пришло через ~2,4 с после клика: это успех того же клика, без
    второго нажатия (ACL-1324 ждал 2 с)."""
    page, state, rows = _fake_page(monkeypatch, failures=0, dialog_after_polls=9)
    assert md._open_further_limit(page) is True
    assert state["click_timeouts"] == [5000]
    assert rows[-1]["note"] == "text" and rows[-1]["diag"]["attempts"] == 1


def test_a_save_audience_button_still_blocks_the_text_path(monkeypatch):
    """Фраза внутри кнопки с «Save audience» (ревью PR #328, F4): все пять
    попыток — ни одного нажатия по тексту."""
    page, state, rows = _fake_page(monkeypatch, failures=0, dialog_after_polls=1)
    page.evaluate.side_effect = lambda js, *a: (
        {"found": 0, "mixed": 1} if js == md._FURTHER_LIMIT_ROW_JS else
        [1200, 800] if "innerWidth" in js else None)
    assert md._open_further_limit(page) is False
    assert state["scroll_timeouts"] == [] and state["click_timeouts"] == []
    assert rows[-1]["note"] == "not_opened" and rows[-1]["diag"]["mixed"] == 1


def test_two_rows_stop_at_once(monkeypatch):
    page, state, rows = _fake_page(monkeypatch, failures=0, dialog_after_polls=1)
    page.evaluate.side_effect = lambda js, *a: (
        {"found": 2, "mixed": 0} if js == md._FURTHER_LIMIT_ROW_JS else
        [1200, 800] if "innerWidth" in js else None)
    assert md._open_further_limit(page) is False
    assert state["scroll_timeouts"] == [] and page.mouse.method_calls == []
    assert rows[-1]["diag"]["found"] == 2 and rows[-1]["diag"]["attempts"] == 1


# ── ревью PR #334: корзины, ранжирование, повторное нажатие ────────────────

_PURE_COPY = ('<div role="button" tabindex="0" data-x="copy">'
              'Further limit the reach of your ads</div>')


def _marked_is_the_card_row(page) -> bool:
    return page.evaluate(f"""() => {{ const m = document.querySelectorAll('[data-acl-further-limit]');
        return m.length === 1 && m[0].matches('{_ROW}'); }}""")


def test_a_hidden_copy_does_not_turn_an_unlaid_row_into_two(page):
    """F2: настоящая строка 0×0 в потоке + скрытая (display:none) копия фразы
    где-то ещё. Строка в потоке старше скрытых — одна, нажата с первой
    попытки (прежде `found:2` и мгновенный жёсткий отказ)."""
    page.set_content(_editor(f'<main id="card" class="unlaid-size" style="width:640px">{FRAG["suggest_fx"]}</main>'
                             f'<div style="display:none">{_PURE_COPY}</div>', filler_px=1400))
    page.evaluate(_LAY_OUT_ON_SIGHT)
    page.evaluate(_META_FURTHER, _DIALOG)
    _park_mouse_over_the_side_panel(page)
    assert md._open_further_limit(page) is True
    row = _last_row(page)
    assert row["note"] == "cdp" and row["diag"]["found"] == 1 and row["diag"]["attempts"] == 1, row
    assert _marked_is_the_card_row(page)
    assert _clicked(page) == ["Further limit the reach of your ads"]


def test_a_laid_out_row_wins_over_unlaid_copies(page):
    """Разложенная строка старше неразложенных: 0×0-копия в потоке и скрытая
    копия её не делают «тремя строками»."""
    page.set_content(_editor(f'<main id="card" style="width:640px">{FRAG["suggest_fx"]}</main>'
                             f'<div style="width:0;height:0;overflow:hidden">{_PURE_COPY}</div>'
                             f'<div style="display:none">{_PURE_COPY}</div>', filler_px=1400))
    got = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert got["found"] == 1 and got["was_laid"] is True and got["laid"] is True, got
    assert _marked_is_the_card_row(page)


def test_the_phrase_inside_a_dialog_is_not_a_row(page):
    """Окно «Reach your preferred audiences» с той же фразой — не строка
    карточки, даже когда оно разложено рядом с ней."""
    page.set_content(_editor(f'<main id="card" style="width:640px">{FRAG["suggest_fx"]}</main>', filler_px=200)
                     + '<div role="dialog" style="position:fixed;top:20px;left:720px;width:400px;background:#fff">'
                     + _PURE_COPY + '</div>')
    got = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert got["found"] == 1, got
    assert _marked_is_the_card_row(page)


def test_a_visibility_hidden_row_with_a_size_is_not_pressed(page, monkeypatch):
    """Строка с размером, но visibility:hidden — не «разложена»: ни
    доверенного, ни DOM-клика; текстом Playwright её тоже не жмёт."""
    monkeypatch.setattr(md, "FURTHER_LIMIT_TEXT_TIMEOUT_MS", 300)
    page.set_content(_editor(f'<main id="card" style="width:640px">{FRAG["suggest_fx"]}</main>', filler_px=200))
    page.evaluate(f"""() => document.querySelector('{_ROW}').style.visibility = 'hidden'""")
    assert _row_size(page)[0] > 0
    page.evaluate(_META_FURTHER, _DIALOG)
    assert md._open_further_limit(page) is False
    assert _clicked(page) == []
    row = _last_row(page)
    assert row["note"] == "not_opened" and row["diag"]["laid"] is False, row


# Высокий предок: у строки (display:none) и её карточки нет размера, ближайший
# предок с размером выше окна. Раскладка — по метке в самом верху предка;
# строка лежит на 1500 px ниже метки.
_TALL = f"""
<div id="tall">
  <div id="sentinel" style="height:10px"></div>
  <div style="height:1500px">Include these custom audiences</div>
  <div id="card" class="unlaid-none" style="width:640px">
    <div role="button" tabindex="0"
      data-surface="/am/editor_drawer/editor:ad_set/adset_targeting_static_extended_pgd_section/lib:targeting-individual-setting-toggle-automation-button">
      <div>Further limit the reach of your ads</div></div>
  </div>
  <div style="height:1000px"></div>
</div>
"""

_LAY_OUT_ON_SENTINEL = r"""
() => {
  const editor = document.getElementById('editor');
  const card = document.getElementById('card');
  new IntersectionObserver((es) => es.forEach(e => {
    if (e.isIntersecting) card.classList.remove('unlaid-none');
  }), { root: editor }).observe(document.getElementById('sentinel'));
}
"""


def test_a_tall_ancestor_is_scrolled_to_its_top_and_the_row_recentred(page):
    """Предок выше окна — к его началу (`start`), не в центр: иначе верх
    предка уходит над экраном и строка не раскладывается. Разложилась ниже
    экрана — строку саму в центр, и тогда доверенный клик попадает."""
    page.set_content(_editor(_TALL, filler_px=1400))
    page.evaluate(_LAY_OUT_ON_SENTINEL)
    page.evaluate(_META_FURTHER, _DIALOG)
    _park_mouse_over_the_side_panel(page)
    assert md._open_further_limit(page) is True
    row = _last_row(page)
    assert row["note"] == "cdp" and row["diag"]["attempts"] == 1, row
    assert row["diag"]["hit"] is True and row["diag"]["unlaid"] is True
    assert _clicked(page) == ["Further limit the reach of your ads"]


def test_a_row_that_never_lays_out_still_gets_the_text_path(monkeypatch):
    """Строка найдена, но так и не разложилась (`laid:false`) — прежний путь
    по тексту, как до ACL-1324: Playwright ждёт её до 5 с и жмёт."""
    page, state, rows = _fake_page(monkeypatch, failures=0, dialog_after_polls=1)
    page.evaluate.side_effect = lambda js, *a: (
        {"found": 1, "laid": False, "was_laid": False, "surface": "x", "role": "button"}
        if js == md._FURTHER_LIMIT_ROW_JS else [1200, 800] if "innerWidth" in js else None)
    assert md._open_further_limit(page) is True
    assert state["click_timeouts"] == [5000]
    assert rows[-1]["note"] == "text" and rows[-1]["diag"]["attempts"] == 1
    assert rows[-1]["diag"]["unlaid"] is True and rows[-1]["diag"]["laid"] is False
    md._cdp_mouse_click.assert_not_called()


def test_a_dialog_that_opens_late_is_not_pressed_again(monkeypatch):
    """Окно пришло позже 3 с ожидания: в начале следующей попытки это
    замечается, и второго нажатия нет (второе могло бы закрыть окно)."""
    page, state, rows = _fake_page(monkeypatch, failures=0, dialog_after_polls=12)
    assert md._open_further_limit(page) is True
    assert state["click_timeouts"] == [5000]
    assert rows[-1]["note"] == "late" and rows[-1]["diag"]["attempts"] == 1


def test_a_disabled_row_is_not_a_row(page):
    """Строка с aria-disabled — не кандидат (ACL-1324): ни пометки, ни клика."""
    page.set_content(_editor(f'<main id="card" style="width:640px">{FRAG["suggest_fx"]}</main>', filler_px=200))
    page.evaluate(f"""() => document.querySelector('{_ROW}').setAttribute('aria-disabled', 'true')""")
    got = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert got["found"] == 0, got
    assert page.evaluate("document.querySelectorAll('[data-acl-further-limit]').length") == 0


def test_a_covered_centre_gets_no_trusted_click(page, monkeypatch):
    """Центр строки накрыт — доверенный клик туда ушёл бы в то, что сверху
    (вплоть до кнопок рекомендаций). Только DOM-клик по самой строке."""
    from tests.test_suggest_an_audience_layout import _COVER_ROW
    cdp = MagicMock()
    monkeypatch.setattr(md, "_cdp_mouse_click", cdp)
    page.set_content(_editor(f'<main id="card" style="width:640px">{FRAG["suggest_fx"]}</main>', filler_px=200))
    page.evaluate(_META_FURTHER, _DIALOG)
    page.evaluate(_COVER_ROW)
    assert md._open_further_limit(page) is True
    cdp.assert_not_called()
    row = _last_row(page)
    assert row["note"] == "dom" and row["diag"]["hit"] is False, row
