# -*- coding: utf-8 -*-
"""Строка группы в дереве нового редактора без «Action menu» до наведения (ACL-1630).

06.10, Facebook_5265, три прогона SA_ARC (aa99ab40d397, fab05371305f,
da07f392ff1f): объявление 2 — `no_create_control`, телеметрия `tree`:
`{"rows": 3, "create": 0, "adset_menu": false}`. Снимок отказа
(`tests/fixtures/ads_manager/editor_tree_no_action_buttons_2026_10_06.html`,
aa99ab40d397/07_simple_create_more_ads/block_ad_tree): три строки дерева
(кампания, группа, объявление), и у каждой на месте блока кнопок («default» и
«Action menu» в снимках 24.09 и 30.09) — пустой `<div></div>`. Meta теперь
рисует кнопки строки, только когда над ней курсор; пока курсора нет, их нет в
DOM вовсе, и разведка дерева не находила, что нажимать. Диалога, мастера и
меню на снимке нет (`dialog: null`, `wizard_cleared: null`).

Здесь: на снимке 06.10 «Action menu» не находится (как живьём); после
наведения курсора на строку группы (движение мыши, не нажатие) Meta рисует
кнопки — в тесте это эмулируется на `mouseover`, разметка кнопки — по снимку
30.09 (`simple_create_more_ads_2026_09_30.html`); дальше путь прежний: «Action
menu» → «Create ad» → новая строка. Строка, которая и под курсором кнопок не
показала, — прежний отказ `no_create_control` со снимком.
"""
from __future__ import annotations

import time

import pytest

from tests import test_simple_create_more_ads as base
from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import page_capture as pc
from src.fb_ads_rpa import simple_create as sc

TREE = FIXTURES / "editor_tree_no_action_buttons_2026_10_06.html"

# Кнопка «Action menu» строки группы — как её рисует Meta по снимку 30.09.
_BUTTON = ('<div role="none"><div aria-busy="false" role="button" tabindex="0" '
           'data-surface="/am/editor_drawer/editor_tree:adset/lib:ads-campaign-structure-action-menu-item/CAMPAIGN">'
           '<span><div><div>Action menu</div></div></span></div></div>')

# Поведение Meta: кнопки строки появляются под курсором (`window.hover_mounts`
# = false — строка их не рисует и под курсором).
_HOVER = """
<script>
  window.hover_mounts = true;
  window.mounted = 0;
  document.addEventListener('mouseover', (e) => {
    const row = e.target.closest('[role=rowheader][aria-label="Ad set"]');
    if (!row || !window.hover_mounts || row.querySelector('[data-surface*="action-menu-item"]')) return;
    const slot = row.querySelector('[id^="default-button-for-action-menu_"]').parentElement.nextElementSibling;
    slot.innerHTML = '%BUTTON%';
    window.mounted++;
  }, true);
</script>
"""


def _html() -> str:
    page = base._html().replace(base.FRAG["tree_before"], fragment(TREE, "tree"))
    return page.replace("</head>", _HOVER.replace("%BUTTON%", _BUTTON) + "</head>")


@pytest.fixture
def env(browser, monkeypatch, tmp_path):
    rows: list[tuple] = []
    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 0.1)))
    monkeypatch.setattr(md, "_human_jitter", lambda base_, spread: 0.0)
    monkeypatch.setattr(md, "wait_editor_saved", lambda p, **k: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note", ""), kw.get("diag"))))
    monkeypatch.setattr(pc, "_root", lambda: tmp_path)
    monkeypatch.setattr(pc, "_taken", {})
    pc.forget_sections()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    html = _html()
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
               if route.request.url.startswith("https://adsmanager.facebook.com/") else route.abort())
    page.goto(base.SIMPLE_URL)
    try:
        yield {"page": page, "rows": rows}
    finally:
        pc.forget_sections()
        ctx.close()


def _tree(page) -> dict:
    return page.evaluate(sc._MORE_ADS_TREE_JS, [sc.CREATE_AD_RE, sc._ADSET_MENU_SURFACE])


def test_the_live_tree_has_no_buttons_at_all(env):
    """Снимок 06.10 как есть: три строки, ни «Action menu», ни «Create ad»."""
    got = _tree(env["page"])
    assert got["levels"] == {"Campaign": 1, "Ad set": 1, "Ad": 1}
    assert got["create"] == 0 and got["adset_menu"] is False
    assert got["labels"] == {}


def test_ad_2_is_created_once_the_row_is_hovered(env):
    page = env["page"]
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])
    assert got["ok"], got
    assert got["pressed"] == ["adset_action_menu", "menu_create_ad"]
    assert got["ads_before"] == 1 and got["ads_after"] == 2
    # Кнопки нарисованы наведением, не нажатием: нажатий ровно два.
    clicks = base._clicks(page)
    assert len(clicks) == 2 and sc._ADSET_MENU_SURFACE in clicks[0], clicks
    assert clicks[1] == base.CREATE_SURFACE + "|Create ad"
    assert base._forbidden(clicks) == []
    assert page.evaluate("window.mounted") == 1
    assert got["tree"]["hover"]["woke"] is True
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"]
    assert len(row) == 1 and row[0][1] is True and row[0][2] == "created"


def test_a_row_that_shows_no_buttons_even_under_the_cursor_is_still_named(env):
    page = env["page"]
    page.evaluate("() => { window.hover_mounts = false; }")
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])
    assert got["ok"] is False and got["why"] == "no_create_control"
    assert got["tree"]["hover"]["woke"] is False and got["tree"]["hover"]["tries"] >= 1
    assert base._clicks(page) == []
    assert got["capture"], "отказ сохраняет снимок дерева"


def test_a_tree_that_already_has_the_menu_is_not_hovered(env):
    """Прежняя раскладка (кнопка в DOM): наведение не нужно и не делается."""
    page = env["page"]
    page.evaluate("""(btn) => {
      const row = document.querySelector('[role=rowheader][aria-label="Ad set"]');
      row.querySelector('[id^="default-button-for-action-menu_"]').parentElement.nextElementSibling.innerHTML = btn;
    }""", _BUTTON)
    got = sc.create_next_ad(page, tag="ad2", known_names=["Sample Ad 01"])
    assert got["ok"], got
    assert page.evaluate("window.mounted") == 0
    assert "hover" not in got["tree"]
