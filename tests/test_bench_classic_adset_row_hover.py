# -*- coding: utf-8 -*-
"""Классический редактор: «Action menu» строки группы открывается наведением (ACL-1691).

07.10, Facebook_5265 (SA_ARC_All_Image_Day_35_07.10, f2c513f0a22d, снимки
07/08/10/11/13/14_create_additional_ad) и KW_TRL на 9403A: объявления 2-4 не
созданы, `create_menu_item_not_found`, `menuSurfaces: []`, точка нажатия
(1311, 22) лежит на тексте объявления из шапки-«хлебных крошек».

Два корня на снимке (`classic_editor_tree_hover_only_2026_10_07.html`):

1. Как и в новом редакторе (ACL-1630), строки дерева несут на месте кнопок
   пустой `<div></div>`: «Action menu» в DOM нет, пока над строкой нет курсора.
   Шаг ждал кнопку 30 с (`_wait_tree_ready`), не дожидался и шёл по координатам.
2. Координаты брались от первого текста с именем группы, отсортированного по
   `y`: выше строки дерева стоит шапка с тем же именем (`y` = 22), и «строкой
   группы» оказывалась она. Нажатие приходилось на хлебную крошку объявления.

Здесь: шапка стоит над деревом в левой трети окна (как живьём), кнопка
рисуется на `mouseover` строки дерева. Курсор наводится без нажатия и без
фокуса, «Action menu» и «Create ad» — единственные нажатия. Строка, которая и
под курсором кнопки не показала, — отказ с английской причиной и без нажатий.
"""
from __future__ import annotations

import time

import pytest

from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401

from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import page_capture as pc

FIXTURE = FIXTURES / "classic_editor_tree_hover_only_2026_10_07.html"
ADSET = "Sample Campaign 01"
URL = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone"

_BUTTON = ('<div role="none"><div aria-busy="false" role="button" tabindex="0" '
           'data-surface="/am/editor_drawer/editor_tree:adset/lib:campaign-structure-action-menu-item/CAMPAIGN">'
           '<span><div><div>Action menu</div></div></span></div></div>')

# Шапка над деревом, оба в левой трети окна; дерево — узкая колонка под ней.
_CSS = """<style>
  body { margin: 0; }
  #crumbs { height: 40px; width: 420px; overflow: hidden; }
  #crumbs div { display: inline-block; }
  /* Крошки друг под другом, обе в левой трети окна, как живьём (y ~ 0-40). */
  #crumbs div[data-testid] { display: block; height: 20px; overflow: hidden; }
  #tree { width: 400px; }
  [role=row] { height: 46px; }
  [role=rowheader] { height: 46px; }
</style>"""

# Поведение Meta: кнопка строки группы рисуется на входе курсора в строку.
# Все нажатия и переходы фокуса пишутся на уровне документа, в фазе перехвата.
_PAGE_JS = """
<script>
  window.hover_mounts = true;
  window.entries = 0;
  window.mounted = 0;
  window.presses = [];
  window.focuses = [];
  window.presses_at_mount = null;
  window.focus_at_mount = null;
  window.menu_opened = 0;
  window.fail_first_menu = false;
  window.failed_once = false;
  window.press_pts = [];
  const desc = (t) => !t || !t.getAttribute ? String(t)
    : (t.tagName.toLowerCase() + '|' + (t.getAttribute('role') || '') + '|'
       + (t.getAttribute('aria-label') || (t.textContent || '').trim()).slice(0, 30));
  window.openMenu = (btn) => btn.addEventListener('click', () => {
    window.menu_opened++;
    if (window.fail_first_menu && !window.failed_once) {
      // Выбор группы перемонтирует дерево: кнопка пропадает, меню не открыто.
      window.failed_once = true;
      btn.parentElement.parentElement.innerHTML = '<div></div>';
      return;
    }
    const item = document.createElement('div');
    item.setAttribute('role', 'menuitem');
    item.setAttribute('data-surface', '/am/editor_drawer/lib:campaign-structure-item-action-menu-create');
    item.textContent = 'Create ad';
    item.addEventListener('click', () => {
      const name = document.createElement('input');
      name.placeholder = 'Enter your ad name here...';
      document.body.appendChild(name);
    });
    document.body.appendChild(item);
  });
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click',
                      'auxclick', 'dblclick', 'contextmenu']) {
    document.addEventListener(type, (e) => { window.presses.push(type + ':' + desc(e.target)); }, true);
  }
  document.addEventListener('mousedown', (e) => { window.press_pts.push([e.clientX, e.clientY]); }, true);
  document.addEventListener('focusin', (e) => { window.focuses.push(desc(e.target)); }, true);
  document.addEventListener('mouseover', (e) => {
    const row = e.target.closest('[role=rowheader][aria-label="Ad set"]');
    if (!row || (e.relatedTarget && row.contains(e.relatedTarget))) return;
    window.entries++;
    if (!window.hover_mounts || row.querySelector('[data-surface*="action-menu-item"]')) return;
    window.presses_at_mount = window.presses.slice();
    window.focus_at_mount = window.focuses.slice();
    const slot = row.querySelector('[id^="default-button-for-action-menu_"]').parentElement.nextElementSibling;
    slot.innerHTML = '%BUTTON%';
    window.mounted++;
    window.openMenu(slot.querySelector('[role=button]'));
  }, true);
</script>
"""


def _html() -> str:
    return ("<!doctype html><html><head>" + _CSS + _PAGE_JS.replace("%BUTTON%", _BUTTON)
            + "</head><body><div id='crumbs'>" + fragment(FIXTURE, "breadcrumb")
            + "</div><div id='tree'>" + fragment(FIXTURE, "tree") + "</div></body></html>")


@pytest.fixture
def env(browser, monkeypatch, tmp_path):
    rows: list[tuple] = []
    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 0.05)))
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
    page.goto(URL)
    try:
        yield {"page": page, "rows": rows}
    finally:
        pc.forget_sections()
        ctx.close()


def test_the_captured_tree_has_no_buttons_and_the_crumbs_carry_the_same_name(env):
    page = env["page"]
    assert page.locator('[data-surface*="action-menu-item"]').count() == 0
    assert page.locator("[role=rowheader]").count() == 3
    assert page.get_by_text(ADSET, exact=True).count() == 4  # две крошки + две строки дерева


def test_a_tree_without_the_button_is_ready(env):
    """Кнопки нет до наведения — дерево готово, ждать её 30 с незачем."""
    page = env["page"]
    assert page.evaluate(md._TREE_READY_JS) is True
    started = time.time()
    assert md._wait_tree_ready(page, timeout_s=0.5) is True
    assert time.time() - started < 0.4


def test_ad_2_is_created_by_hovering_the_row_then_the_menu(env):
    page = env["page"]
    assert md.create_additional_ad(page, ADSET) is True
    assert page.evaluate("window.mounted") == 1
    assert page.evaluate("window.menu_opened") == 1
    # За наведение — ни нажатия, ни фокуса.
    assert page.evaluate("window.presses_at_mount") == []
    assert page.evaluate("window.focus_at_mount") == []
    presses = page.evaluate("window.presses")
    # Мышь двигалась, но не нажималась: нет ни mousedown, ни pointerdown.
    assert [p for p in presses if not p.startswith("click:")] == [], presses
    # Нажатий (DOM-клик) ровно два — «Action menu» и «Create ad»; ни в шапку,
    # ни в строки дерева не нажато.
    assert len(presses) == 2, presses
    assert "Action menu" in presses[0] and "Create ad" in presses[1], presses
    assert page.evaluate("window.focuses") == []
    assert md.last_create_block() == ""
    assert page.get_by_placeholder("Enter your ad name here...").is_visible()
    assert [r for r in env["rows"] if r[0] == "create_additional_ad"] == []


def test_the_point_is_the_tree_row_not_the_breadcrumb(env):
    page = env["page"]
    pt = md._eval_bounded(page, md._CLASSIC_ROW_POINT_JS)
    crumb_bottom = page.evaluate("document.getElementById('crumbs').getBoundingClientRect().bottom")
    assert pt["y"] > crumb_bottom, (pt, crumb_bottom)
    hit = page.evaluate(
        "([x, y]) => !!document.elementFromPoint(x, y).closest('[data-surface$=\"editor_tree:adset\"]')",
        [pt["x"], pt["y"]])
    assert hit is True


def test_hovering_stops_as_soon_as_the_button_appears(env):
    page = env["page"]
    assert md._wake_classic_adset_row(page) == {"row": True, "tries": 1, "woke": True}
    assert page.evaluate("window.entries") == 1


def test_a_row_that_shows_no_button_under_the_cursor_blocks_with_a_reason(env):
    page = env["page"]
    page.evaluate("() => { window.hover_mounts = false; }")
    assert md.create_additional_ad(page, ADSET) is False
    # Ровно три наведения, каждое — новый вход курсора; ни нажатия, ни фокуса.
    assert page.evaluate("window.entries") == md._CLASSIC_WAKE_TRIES == 3
    assert page.evaluate("window.presses") == []
    assert page.evaluate("window.focuses") == []
    assert md.last_create_block() == "adset_action_menu_not_shown"
    row = [r for r in env["rows"] if r[0] == "create_additional_ad"][-1]
    assert row[1] is False and row[2] == "adset_action_menu_not_shown"
    assert "no Action menu button" in row[3]["reason"]
    assert row[3]["hover"] == {"row": True, "tries": 3, "woke": False}


def test_a_tree_that_already_has_the_button_is_not_hovered(env):
    page = env["page"]
    page.evaluate("""(btn) => {
      const row = document.querySelector('[role=rowheader][aria-label="Ad set"]');
      const slot = row.querySelector('[id^="default-button-for-action-menu_"]').parentElement.nextElementSibling;
      slot.innerHTML = btn;
      window.openMenu(slot.querySelector('[role=button]'));
    }""", _BUTTON)
    assert md.create_additional_ad(page, ADSET) is True
    assert page.evaluate("window.mounted") == 0
    assert page.evaluate("window.entries") == 0


def test_the_legacy_coordinate_point_is_the_tree_row_not_the_breadcrumb(env, monkeypatch):
    """Подвести к устаревшему пути по координатам при дереве на месте (ACL-1691, F2).

    Шапка несёт `data-testid="nav-header-*"` и то же имя группы на y~20; без
    исключения шапки «строкой группы» снова оказалась бы она."""
    page = env["page"]
    assert page.locator('[data-testid^="nav-header"]').count() == 3
    monkeypatch.setattr(md, "_wake_classic_adset_row", lambda p: {"row": False, "tries": 0, "woke": False})
    page.evaluate("() => { window.hover_mounts = false; }")
    md.create_additional_ad(page, ADSET)
    pts = page.evaluate("window.press_pts")
    assert pts, "the legacy coordinate click did not run"
    crumb_bottom = page.evaluate("document.getElementById('crumbs').getBoundingClientRect().bottom")
    x, y = pts[0]
    assert y > crumb_bottom, (pts, crumb_bottom)
    hit = page.evaluate(
        "([x, y]) => !!document.elementFromPoint(x, y).closest('[data-surface$=\"editor_tree:adset\"]')",
        [x, y])
    assert hit is True, pts


def test_a_tree_that_remounts_after_the_first_menu_is_hovered_again(env):
    """После перемонтирования дерева кнопка снова только под курсором (F4)."""
    page = env["page"]
    page.evaluate("() => { window.fail_first_menu = true; }")
    assert md.create_additional_ad(page, ADSET) is True
    assert page.evaluate("window.mounted") == 2
    assert page.evaluate("window.menu_opened") == 2
    assert page.evaluate("window.press_pts") == []  # по-прежнему ни одного нажатия мышью


def test_a_later_call_that_does_not_block_clears_the_reason(env):
    page = env["page"]
    page.evaluate("() => { window.hover_mounts = false; }")
    assert md.create_additional_ad(page, ADSET) is False
    assert md.last_create_block() == "adset_action_menu_not_shown"
    page.evaluate("() => { window.hover_mounts = true; }")
    assert md.create_additional_ad(page, ADSET) is True
    assert md.last_create_block() == ""
