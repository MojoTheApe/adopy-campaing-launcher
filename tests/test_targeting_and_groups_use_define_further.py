# -*- coding: utf-8 -*-
"""Таргет из двух AND-групп собирается через «Define further» (Mix, Mix2…).

16.09, Doc20, KW_trl_All_Video_Mix_35_16.09: все шестнадцать интересов обеих
групп легли в один список «Include people who match», кнопка «Define further»
осталась не нажатой. Библиотека отдаёт таргет как «группа1 [AND] группа2», а
записанный поток делил строку только по запятым — «[AND]» склеивался с
соседним интересом, кнопку никто не жал, и вместо «финансы И люкс»
получалось «финансы ИЛИ люкс» — обратная аудитория.

Проверяется на синтетической странице в офлайн-Chrome: поле интересов,
кнопка «Define further», которая добавляет второе поле; интересы второй
группы должны попасть во второе поле.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import recipe_player as rp  # noqa: E402

_PAGE = """
<!doctype html><html><body>
<div id="groups">
  <div class="group" role="presentation"><input placeholder="Add demographics, interests or behaviours" data-group="1"></div>
</div>
<div role="button" id="define" onclick="addGroup()">Define further</div>
<script>
  window.filled = [];
  function addGroup() {
    const n = document.querySelectorAll('.group').length + 1;
    const d = document.createElement('div'); d.className = 'group'; d.setAttribute('role', 'presentation');
    d.innerHTML = '<input placeholder="Add demographics, interests or behaviours" data-group="' + n + '">';
    document.getElementById('groups').appendChild(d);
  }
  document.addEventListener('input', (e) => {
    window.filled.push([e.target.dataset.group, e.target.value]);
  });
</script>
</body></html>
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
    monkeypatch.setattr(md, "_sleep_h", lambda s: None)
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md, "open_detailed_targeting_field", lambda p: True)
    monkeypatch.setattr(md, "pick_first_suggestion", lambda p: True)
    monkeypatch.setattr(rp, "pause_short", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    p.set_content(_PAGE)
    try:
        yield p
    finally:
        ctx.close()


def _player(page):
    player = rp.RecipePlayer.__new__(rp.RecipePlayer)
    player.page = page
    player.result = type("R", (), {"steps": []})()
    return player


def test_the_second_group_goes_through_define_further_into_its_own_field(page):
    player = _player(page)
    assert player._run_targeting("Private banking, Wealth management [AND] Business jet, Yacht club") is True
    filled = page.evaluate("window.filled")
    assert filled == [["1", "Private banking"], ["1", "Wealth management"],
                      ["2", "Business jet"], ["2", "Yacht club"]], filled


def test_the_and_marker_never_lands_inside_a_term(page):
    player = _player(page)
    player._run_targeting("Personal finance [AND] Frequent international travelers")
    values = [v for _, v in page.evaluate("window.filled")]
    assert not any("[AND]" in v for v in values), values


def test_a_single_group_does_not_touch_define_further(page):
    player = _player(page)
    assert player._run_targeting("Day trading, FxPro") is True
    assert page.evaluate("document.querySelectorAll('.group').length") == 1


def test_define_further_that_does_not_open_a_group_fails_the_step(page):
    page.evaluate("document.getElementById('define').onclick = null")
    player = _player(page)
    assert player._run_targeting("A [AND] B") is False
    assert any(s.action == "define_further" and not s.ok for s in player.result.steps)


# ── путь, которым идёт живой поток: вставка группы целиком ────────────────
# Живой прогон 17:29 показал: записанный поток не зовёт `_run_targeting`, он
# вставляет строку таргета через `paste_targeting_interests`. Проверяем его.

_PASTE_PAGE = _PAGE.replace(
    "document.addEventListener('input'",
    """document.addEventListener('paste', (e) => {
    window.filled.push([e.target.dataset.group, e.clipboardData.getData('text/plain')]);
    const chip = document.createElement('li');
    chip.textContent = e.clipboardData.getData('text/plain').split(',')[0].trim();
    chip.insertAdjacentHTML('beforeend', '<button>Close</button>'); e.target.parentElement.appendChild(chip);
  });
  document.addEventListener('input'""",
)


def test_the_live_paste_path_puts_each_group_into_its_own_field(page, monkeypatch):
    page.set_content(_PASTE_PAGE)
    page.focus("input[data-group='1']")
    monkeypatch.setattr(md, "_telemetry", type("T", (), {"record": staticmethod(lambda *a, **k: None)})())
    ok = md.paste_targeting_groups(
        page, "Investment banking, Asset management [AND] Business jet, Yacht club"
    )
    assert ok is True
    assert page.evaluate("window.filled") == [
        ["1", "Investment banking, Asset management"],
        ["2", "Business jet, Yacht club"],
    ]


def test_the_group_splitter_reads_all_three_spellings():
    assert md.targeting_groups("A, B [AND] C") == ["A, B", "C"]
    assert md.targeting_groups("A | B") == ["A", "B"]
    assert md.targeting_groups("A \\ B") == ["A", "B"]
    assert md.targeting_groups("Day trading, FxPro") == ["Day trading, FxPro"]


def test_the_click_is_confirmed_by_a_new_field_not_by_the_click(page):
    assert md.targeting_input_count(page) == 1
    assert md.click_define_further(page) is True
    assert md.targeting_input_count(page) == 2


# ── интерес засчитывается только в своей группе (ACL-1219) ──────────────────

_CHIPS_PAGE = """
<!doctype html><html><body>
<div id="groups">
  <div class="group" role="presentation">
    <div>Include people who match</div>
    <div class="chips"><li>Investment banking<button aria-label="Close"></button></li><li>Asset management<button aria-label="Close"></button></li></div>
    <input placeholder="Add demographics, interests or behaviours" data-group="1">
  </div>
</div>
<div role="button" id="define" onclick="addGroup()">Define further</div>
<script>
  window.dropTerms = false;
  function addGroup() {
    const n = document.querySelectorAll('.group').length + 1;
    const d = document.createElement('div'); d.className = 'group'; d.setAttribute('role', 'presentation');
    d.innerHTML = '<div>And must also match</div><div class="chips"></div>' +
      '<input placeholder="Add demographics, interests or behaviours" data-group="' + n + '">';
    document.getElementById('groups').appendChild(d);
  }
  // Вставка кладёт плашки в свою группу, если Meta её приняла.
  document.addEventListener('paste', (e) => {
    const inp = e.target;
    if (window.dropTerms) return;
    const chips = inp.parentElement.querySelector('.chips');
    for (const t of (e.clipboardData.getData('text/plain') || '').split(',')) {
      const s = document.createElement('li'); s.textContent = t.trim(); s.insertAdjacentHTML('beforeend', '<button aria-label="Close"></button>'); chips.appendChild(s);
    }
  });
</script>
</body></html>
"""


def test_an_interest_of_the_first_group_does_not_confirm_an_empty_second(page, monkeypatch):
    """16.09 Doc20 Mix: вторая группа осталась пустой, а шаг отчитался успехом."""
    notes = []
    monkeypatch.setattr(md, "_telemetry",
                        type("T", (), {"record": staticmethod(lambda *a, **k: notes.append(k.get("note")))})())
    page.set_content(_CHIPS_PAGE)
    page.focus("input[data-group='1']")
    page.evaluate("window.dropTerms = true")  # Meta не приняла вставку второй группы
    # Термины второй группы уже видны на странице — плашками первой. Старая
    # проверка («термин виден где угодно») на этом и обманывалась.
    ok = md.paste_targeting_groups(
        page, "Investment banking [AND] Investment banking, Asset management")
    assert ok is False
    assert "group_empty" in notes


def test_a_filled_second_group_is_confirmed_in_its_own_box(page, monkeypatch):
    monkeypatch.setattr(md, "_telemetry", type("T", (), {"record": staticmethod(lambda *a, **k: None)})())
    page.set_content(_CHIPS_PAGE)
    page.focus("input[data-group='1']")
    assert md.paste_targeting_groups(page, "Investment banking [AND] Business jet, Yacht club") is True
    assert page.evaluate(
        "[...document.querySelectorAll('.group')].map(g => g.querySelector('.chips').textContent)"
    ) == ["Investment bankingAsset managementInvestment banking", "Business jetYacht club"]


def test_typed_text_and_suggestions_are_not_counted_as_selected(page):
    """Ревью 18.09: набранное в поле и подсказки под ним — не выбранные интересы."""
    page.set_content(
        "<div class='group' role='presentation'><div>Include people who match</div>"
        "<div class='chips'></div>"
        "<input placeholder='Add demographics, interests or behaviours' value='Yacht club'>"
        "<ul role='listbox'><li role='option'>Yacht club</li></ul></div>"
    )
    res = md._group_has_terms(page, ["Yacht club"])
    assert res["found"] is True
    assert res["hits"] == [] and res["missing"] == ["Yacht club"], res


def test_a_selected_interest_chip_is_counted(page):
    page.set_content(
        "<div class='group' role='presentation'><div>Include people who match</div>"
        "<div class='chips'><li>Yacht club<button aria-label=\"Close\"></button></li></div>"
        "<input placeholder='Add demographics, interests or behaviours'></div>"
    )
    assert md._group_has_terms(page, ["Yacht club"])["hits"] == ["Yacht club"]


# ── реальная разметка: снимок 991d22be57ef, CR_ait_All_Video_Mix_35_18.09 ─────
# Блок AND-групп сохранён целиком page_capture.capture(blocks=...) на живом
# прогоне 18.09. Группа 1 — финансы, группа 2 — люкс; в группе 2 Meta не
# распознала «Frequent international travelers».

_REAL = (Path(__file__).parent / "fixtures" / "meta" / "and_groups_two_filled.html").read_text(encoding="utf-8")
_GROUP1 = ["Asset management", "Personal finance", "Private banking", "Investment banking"]
_GROUP2 = ["Frequent international travelers", "Business class", "Business jet",
           "Luxury resorts", "Yacht club"]


def test_real_markup_selected_interests_are_the_chips_of_the_last_group(page):
    page.set_content(_REAL)
    res = md._group_has_terms(page, _GROUP2)
    assert res["by"] == "chips", res
    assert set(res["hits"]) == {"Business class", "Business jet", "Luxury resorts", "Yacht club"}, res
    assert res["missing"] == ["Frequent international travelers"], res


def test_real_markup_first_group_interests_do_not_confirm_the_second(page):
    """Сам баг ACL-1219: интересы группы 1 не засчитываются группе 2."""
    page.set_content(_REAL)
    res = md._group_has_terms(page, _GROUP1)
    assert res["hits"] == [], res


def test_real_markup_an_emptied_second_group_is_empty(page):
    """Вторая группа без плашек — пустая, даже если группа 1 полна."""
    page.set_content(_REAL)
    page.evaluate("""() => {
      const inputs = [...document.querySelectorAll('input')];
      let box = inputs[inputs.length - 1].parentElement;
      for (let i = 0; i < 12 && box; i++, box = box.parentElement) {
        if (box.querySelectorAll('input').length > 1) break;
        box.querySelectorAll('li').forEach(li => li.remove());
      }
    }""")
    res = md._group_has_terms(page, _GROUP2 + _GROUP1)
    assert res["hits"] == [] and res["selected"] == [], res


@pytest.mark.parametrize("kind", ["plain_text", "hidden_chip", "prefix_chip"])
def test_unselected_or_different_real_group_evidence_is_rejected(page, kind):
    page.set_content(_REAL)
    page.evaluate("""kind => {
      const inputs = [...document.querySelectorAll('input')];
      const box = inputs[inputs.length - 1].closest('[role=presentation]');
      box.querySelectorAll('li').forEach(e => e.remove());
      if (kind === 'plain_text') box.insertAdjacentHTML('afterbegin', '<div>Business jet</div>');
      if (kind === 'hidden_chip') box.insertAdjacentHTML('afterbegin', '<ul><li hidden>Business jet<button>Close</button></li></ul>');
      if (kind === 'prefix_chip') box.insertAdjacentHTML('afterbegin', '<ul><li>Business jet enthusiasts<button>Close</button></li></ul>');
    }""", kind)
    assert md._group_has_terms(page, ["Business jet"])["hits"] == []


def test_missing_group_evidence_fails_the_live_paste_path(page, monkeypatch):
    monkeypatch.setattr(md, "paste_targeting_interests", lambda *a: True)
    monkeypatch.setattr(md, "_group_has_terms", lambda *a: {"found": False})
    assert md.paste_targeting_groups(page, "Business jet") is False


def test_failed_recipe_suggestion_is_not_a_selected_interest(page, monkeypatch):
    monkeypatch.setattr(md, "pick_first_suggestion", lambda *a: False)
    assert _player(page)._run_targeting("Business jet") is False


def test_an_aria_hidden_group_does_not_count_as_define_further_success(page):
    page.evaluate("""() => document.getElementById('define').onclick = () => {
      addGroup(); document.querySelector('.group:last-child').setAttribute('aria-hidden', 'true');
    }""")
    assert md.click_define_further(page) is False
    assert md.targeting_input_count(page) == 1


def test_a_single_group_does_not_borrow_a_chip_from_elsewhere(page):
    page.set_content("""<aside><ul><li>Business jet<button>Close</button></li></ul></aside>
      <div role="presentation"><input placeholder="Add demographics, interests or behaviours"></div>""")
    assert md._group_has_terms(page, ["Business jet"])["hits"] == []


# ── реальная разметка после вставки: снимок 199fa985d531, CR_ait IPO, 19.09 ──
_IPO = ["Initial Public Offering", "Venture Capital", "Angel Investor",
        "Investment Banking", "Private Equity fund"]
_AFTER_PASTE = (Path(__file__).parent / "fixtures" / "meta" / "targeting_group_after_paste.html")


def test_all_pasted_interests_are_seen_on_the_real_markup(page):
    """Категория — тоже li с Close, но она не интерес; все пять интересов на месте."""
    page.set_content(_AFTER_PASTE.read_text(encoding="utf-8"))
    res = md._group_has_terms(page, _IPO)
    assert res["found"] is True
    assert sorted(res["hits"]) == sorted(_IPO) and res["missing"] == [], res


def test_the_check_waits_for_meta_to_draw_the_interests(page):
    """19.09: сразу после вставки есть только заголовок категории — не «пусто»."""
    page.set_content(
        "<div role='presentation'><ul><li><span>Interests &gt; Additional interests</span>"
        "<button>Close</button></li><li><ul id='items'></ul></li></ul>"
        "<input placeholder='Add demographics, interests or behaviours'></div>"
        "<script>setTimeout(() => { document.getElementById('items').innerHTML ="
        " '<li><span>Initial public offering</span><button>Close</button></li>'; }, 1200);</script>")
    assert md._group_has_terms(page, ["Initial Public Offering"])["hits"] == []
    res = md._await_group_terms(page, ["Initial Public Offering"], timeout_s=5)
    assert res["hits"] == ["Initial Public Offering"], res


def test_an_empty_group_still_fails_after_the_wait(page):
    page.set_content("<div role='presentation'><ul></ul>"
                     "<input placeholder='Add demographics, interests or behaviours'></div>")
    res = md._await_group_terms(page, ["Initial Public Offering"], timeout_s=1)
    assert res["found"] is True and res["hits"] == []


def test_an_unconfirmed_group_does_not_cost_the_next_group(monkeypatch):
    """19.09: ложное «пусто» у группы 1 теряло всю группу 2 в черновике."""
    pasted, checks = [], iter([{"found": True, "hits": []}, {"found": True, "hits": ["Business jet"], "missing": []}])
    monkeypatch.setattr(md, "click_define_further", lambda page: True)
    monkeypatch.setattr(md, "_focus_last_targeting_input", lambda page: True)
    monkeypatch.setattr(md, "paste_targeting_interests", lambda page, g: pasted.append(g) or True)
    monkeypatch.setattr(md, "_await_group_terms", lambda page, terms: next(checks))
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    import src.fb_ads_rpa.page_capture as pc
    monkeypatch.setattr(pc, "capture", lambda *a, **k: None)
    assert md.paste_targeting_groups(object(), "Investment banking [AND] Business jet") is False
    assert pasted == ["Investment banking", "Business jet"]


# ── «Some detailed targeting options have been combined» (19.09 CR_ait Mix2) ──

def test_combined_options_are_removed_with_remove_all(page, monkeypatch):
    page.set_content(
        "<div id='n'><span>Some detailed targeting options have been combined</span>"
        "<span>Before you can publish this ad set you need to update the detailed targeting options.</span>"
        "<div role='button' id='rm' onclick=\"document.getElementById('n').remove()\">Remove all</div></div>")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda pg, x, y: pg.evaluate(
        "([x, y]) => document.elementFromPoint(x, y).click()", [x, y]))
    res = md._remove_combined_targeting(page)
    assert res["cleared"] is True
    assert page.evaluate("!document.getElementById('n')")


def test_no_notice_means_nothing_is_clicked(page, monkeypatch):
    page.set_content("<div><span>Detailed targeting</span><div role='button'>Remove all</div></div>")
    clicks = []
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda *a: clicks.append(a))
    assert md._remove_combined_targeting(page) == {"notice": False}
    assert clicks == []


def test_an_open_suggestion_list_does_not_hide_the_group(page):
    """20.09 12:30: пока открыт список подсказок Meta, в нём живёт свой ввод с
    тем же placeholder. Из-за него группа 45 с читалась «недоступной», хотя на
    снимке секундой позже стояли все пять интересов."""
    page.set_content(_AFTER_PASTE.read_text(encoding="utf-8"))
    page.evaluate(
        """() => {
          const inp = [...document.querySelectorAll('input, textarea')].find(i =>
            /add demographics|interests/i.test(i.getAttribute('placeholder') || ''));
          const wrap = inp.closest('[role="presentation"]') || inp.parentElement;
          const pop = document.createElement('div');
          pop.setAttribute('role', 'listbox');
          const extra = document.createElement('input');
          extra.setAttribute('placeholder', 'Add demographics, interests or behaviors');
          pop.appendChild(extra);
          wrap.appendChild(pop);
        }"""
    )
    res = md._group_has_terms(page, _IPO)
    assert res["found"] is True, res
    assert sorted(res["hits"]) == sorted(_IPO), res


def test_interests_are_counted_when_meta_hides_their_close_buttons(page):
    """20.09 13:45: крестик у интереса Meta рисует только под курсором. Замер в
    ту же секунду видел 19 интересов, проверка — ни одного, и весь таргетинг
    кампании помечался как не добавленный."""
    page.set_content(_AFTER_PASTE.read_text(encoding="utf-8"))
    page.evaluate("() => { document.querySelectorAll('li button').forEach(b => b.style.display = 'none'); }")
    res = md._group_has_terms(page, _IPO)
    assert res["found"] is True, res
    assert sorted(res["hits"]) == sorted(_IPO), res
