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
  <div class="group"><input placeholder="Add demographics, interests or behaviours" data-group="1"></div>
</div>
<div role="button" id="define" onclick="addGroup()">Define further</div>
<script>
  window.filled = [];
  function addGroup() {
    const n = document.querySelectorAll('.group').length + 1;
    const d = document.createElement('div'); d.className = 'group';
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
    const chip = document.createElement('div');
    chip.textContent = e.clipboardData.getData('text/plain').split(',')[0].trim();
    e.target.parentElement.appendChild(chip);
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
