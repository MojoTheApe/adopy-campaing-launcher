# -*- coding: utf-8 -*-
"""Второе объявление в новом редакторе: снимок для реализации (ACL-1353, шаг 1).

Ни в одном снимке нового редактора (27–28.09) нет кнопки добавления
объявления: в дереве у строк только «default» (быстрое действие, в прежнем
мастере — «Duplicate») и «Action menu», а надпись «Create ad» — вариант радио
«Ad setup» в разделе объявления. Поэтому робот сначала снимает дерево (до
любого нажатия), потом нажимает «Create ad» — кнопку дерева или пункт меню
«Action menu» строки группы — ОДИН раз, снимает открывшееся и останавливает
шаг с причиной `simple_create_more_ads:captured-for-implementation`.

Страница — живая вырезка `tests/fixtures/ads_manager/simple_create_2026_09_27.html`
(дерево, три раздела, футер Publish). Меню «Action menu» и то, что делает
«Create ad», не сняты ни разу — их поведение здесь синтетическое, по
прежнему мастеру (`meta_dom.create_additional_ad`): меню с пунктами Create
ad / Duplicate / Delete, после Create ad в дереве новая строка объявления.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture as pc  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "ads_manager" / "simple_create_2026_09_27.html").read_text(
    encoding="utf-8")

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create"
              "?act=1&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4")

CREATE_SURFACE = "/am/editor_drawer/lib:campaign-structure-item-action-menu-create"

# Поведение, которого в снимках нет: меню строки дерева и «Create ad».
# window.clicks — каждое нажатие на кнопку/пункт/радио: «surface|надпись».
_SCRIPT = """
<style>
  body { margin: 0; }
  /* Как в живом дереве: строка фиксированной высоты, кнопка без размера,
     пока на строку не наведено, — без сдвига раскладки. */
  .hover-only [role=rowheader] { position: relative; height: 46px; overflow: hidden; }
  .hover-only [data-surface*="action-menu-item"] { position: absolute; right: 10px; top: 8px;
                                                    width: 0; height: 0; overflow: hidden; }
  .hover-only [role=rowheader]:hover [data-surface*="action-menu-item"] { width: 30px; height: 30px; }
</style>
<script>
  window.clicks = [];
  const norm = (s) => (s || '').replace(/[\\s\\u200b]+/g, ' ').trim();
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[role=button], [role=menuitem], button, label, input, [role=radio]');
    if (!t) return;
    window.clicks.push((t.getAttribute('data-surface') || '') + '|'
                       + (norm(t.getAttribute('aria-label')) || norm(t.textContent)).slice(0, 40));
  }, true);
  window.menuMode = 'full';
  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-surface*="editor_tree:adset/lib:ads-campaign-structure-action-menu-item"]');
    if (opener && !document.querySelector('[role=menu]')) {
      const m = document.createElement('div');
      m.setAttribute('role', 'menu');
      m.style.cssText = 'position:fixed; left:320px; top:150px; width:200px; background:#fff; z-index:5';
      const items = window.menuMode === 'no_create' ? ['Duplicate', 'Delete']
                                                     : ['Create ad', 'Duplicate', 'Delete'];
      for (const label of items) {
        const it = document.createElement('div');
        it.setAttribute('role', 'menuitem');
        if (label === 'Create ad') it.setAttribute('data-surface', '%CREATE%');
        it.style.cssText = 'height:30px';
        it.textContent = label;
        m.appendChild(it);
      }
      document.body.appendChild(m);
      if (window.menuMode === 'covered') {
        const pub = document.createElement('div');
        pub.setAttribute('role', 'button');
        pub.setAttribute('data-surface', '/am/lib:syd_simple_create_publish');
        pub.style.cssText = 'position:fixed; left:0; right:0; top:100px; height:200px; background:#eee; z-index:9';
        pub.textContent = 'Publish';
        document.body.appendChild(pub);
      }
    }
    const create = e.target.closest('[data-surface="%CREATE%"]');
    if (create) {
      const rows = document.querySelectorAll('[role=rowheader][aria-label="Ad"]');
      const row = rows[rows.length - 1].closest('[role=row]');
      const copy = row.cloneNode(true);
      copy.querySelector('[id^="default-button-for-action-menu_"] span').textContent = 'New Leads Ad';
      row.parentElement.appendChild(copy);
      const m = document.querySelector('[role=menu]');
      if (m) m.remove();
    }
  }, true);
</script>
""".replace("%CREATE%", CREATE_SURFACE)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def env(browser, monkeypatch, tmp_path):
    rows: list[tuple] = []
    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 0.1)))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note", ""), kw.get("diag"))))
    monkeypatch.setattr(pc, "_root", lambda: tmp_path)
    monkeypatch.setattr(pc, "_taken", {})
    monkeypatch.delenv(sc.MORE_ADS_PRESS_SETTING, raising=False)
    pc.forget_sections()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()

    def open_page(*, hover_only: bool = False, menu: str = "full", extra: str = "") -> None:
        body = FIXTURE.replace("<!-- fragment:", "<!-- f:", 1)
        if extra:
            body = body.replace('<div aria-label="grid" role="grid" tabindex="0">',
                                extra + '<div aria-label="grid" role="grid" tabindex="0">', 1)
        html = ("<!doctype html><html><head>" + _SCRIPT + "</head><body"
                + (' class="hover-only"' if hover_only else "") + ">" + body + "</body></html>")
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
                   if route.request.url.startswith("https://adsmanager.facebook.com/") else route.abort())
        page.goto(SIMPLE_URL)
        page.evaluate("(m) => { window.menuMode = m; }", menu)

    try:
        yield {"page": page, "open": open_page, "rows": rows, "dir": tmp_path}
    finally:
        pc.forget_sections()
        ctx.close()


def _clicks(page) -> list[str]:
    return page.evaluate("window.clicks")


def _captures(env) -> list[Path]:
    return sorted(p for p in env["dir"].rglob("context.json"))


def _context(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _forbidden(clicks: list[str]) -> list[str]:
    bad = ("publish", "duplicate", "delete", "discard", "default", "close")
    return [c for c in clicks if any(w in c.lower().split("|", 1)[1] for w in bad)
            or "syd_simple_create_publish" in c]


def test_the_menu_create_ad_is_pressed_once_and_every_stage_is_captured(env):
    env["open"]()
    got = sc.capture_more_ads(env["page"], 2)
    clicks = _clicks(env["page"])
    assert got["reason"] == sc.MORE_ADS_CAPTURED == "captured-for-implementation"
    assert got["stop"] == "create_ad_pressed"
    assert got["pressed"] == ["adset_action_menu", "menu_create_ad"]
    # Ровно два нажатия: «Action menu» строки группы и пункт «Create ad».
    assert len(clicks) == 2, clicks
    assert "editor_tree:adset/lib:ads-campaign-structure-action-menu-item" in clicks[0]
    assert clicks[1] == CREATE_SURFACE + "|Create ad"
    assert _forbidden(clicks) == []
    # Три снимка: до нажатия, меню, после «Create ad».
    ctxs = [_context(p) for p in _captures(env)]
    assert [c["diag"]["stage"] for c in ctxs] == ["before_click", "action_menu", "after_create_ad"]
    assert ctxs[0]["blocks"]["ad_tree"]["found"] is True
    assert ctxs[0]["blocks"]["menu"]["found"] is False, "до нажатия меню было открыто"
    assert ctxs[0]["diag"]["tree"]["levels"] == {"Campaign": 1, "Ad set": 1, "Ad": 1}
    assert ctxs[0]["diag"]["tree"]["create"] == 0
    assert ctxs[1]["blocks"]["menu"]["found"] is True
    assert ctxs[1]["diag"]["menu"]["labels"] == ["Create ad", "Duplicate", "Delete"]
    assert ctxs[1]["diag"]["menu"]["create_surface"] == CREATE_SURFACE
    assert ctxs[2]["diag"]["after"]["ads"] == 2
    menu_block = (_captures(env)[1].parent / "block_menu.html").read_text(encoding="utf-8")
    assert "Create ad" in menu_block and "Duplicate" in menu_block
    tree_block = (_captures(env)[0].parent / "block_ad_tree.html").read_text(encoding="utf-8")
    assert "editor_tree:ad" in tree_block
    # Строка телеметрии называет снимки.
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"]
    assert len(row) == 1 and row[0][1] is False and row[0][2] == "captured-for-implementation"
    assert row[0][3]["captures"] == got["captures"] and len(got["captures"]) == 3
    assert "page capture saved" in row[0][3]["capture_note"]
    assert all("/" in c and ":" not in c for c in got["captures"]), "в имени снимка полный путь"


def test_nothing_is_closed_after_the_menu_without_create_ad(env):
    env["open"](menu="no_create")
    got = sc.capture_more_ads(env["page"], 1)
    assert got["stop"] == "no_create_item"
    assert got["pressed"] == ["adset_action_menu"]
    assert len(_clicks(env["page"])) == 1
    # Меню осталось открытым: ни Escape, ни клика мимо.
    assert env["page"].evaluate("!!document.querySelector('[role=menu]')")
    assert [c["diag"]["stage"] for c in map(_context, _captures(env))] == ["before_click", "action_menu"]
    assert got["reason"] == "captured-for-implementation"


def test_the_ad_setup_create_ad_radio_is_not_the_control(env):
    """«Create ad» в разделе объявления — радио «Ad setup», не добавление."""
    env["open"]()
    sc.capture_more_ads(env["page"], 1)
    clicks = _clicks(env["page"])
    assert not [c for c in clicks if c.startswith("|Create ad") or "INLINE" in c], clicks
    assert env["page"].evaluate("document.getElementById('syn_130').checked") is True


def test_a_create_ad_button_in_the_tree_is_pressed_directly(env):
    env["open"](extra='<div role="button" tabindex="0" style="width:120px;height:30px">Create ad</div>')
    got = sc.capture_more_ads(env["page"], 1)
    clicks = _clicks(env["page"])
    assert got["pressed"] == ["tree_create_ad"] and got["stop"] == "create_ad_pressed"
    assert clicks == ["|Create ad"], "в дерево была кнопка — меню открывать не нужно"
    assert [c["diag"]["stage"] for c in map(_context, _captures(env))] == ["before_click", "after_create_ad"]


def test_a_longer_label_is_not_create_ad(env):
    """«Create ad set» / «Create ad in…» — не та кнопка."""
    env["open"](extra='<div role="button" tabindex="0" style="width:120px;height:30px">Create ad set</div>')
    got = sc.capture_more_ads(env["page"], 1)
    assert "tree_create_ad" not in got["pressed"]
    assert "|Create ad set" not in _clicks(env["page"])


def test_a_hover_only_action_menu_is_opened_after_a_hover(env):
    """Кнопки строк дерева рисуются только при наведении (прежний мастер, 30.07)."""
    env["open"](hover_only=True)
    got = sc.capture_more_ads(env["page"], 1)
    assert got["pressed"] and got["pressed"][0] in ("adset_action_menu", "adset_action_menu_dom"), got["stop"]
    assert got["stop"] == "create_ad_pressed"
    assert _forbidden(_clicks(env["page"])) == []


def test_the_guard_keeps_create_ad_under_publish_unpressed(env):
    """Пункт меню накрыт футером/Publish — охрана `_press_allowed` не пускает."""
    env["open"](menu="covered")
    got = sc.capture_more_ads(env["page"], 1)
    clicks = _clicks(env["page"])
    assert got["stop"] == "create_ad_refused"
    assert got["pressed"] == ["adset_action_menu"]
    assert _forbidden(clicks) == [] and len(clicks) == 1, clicks
    assert ("click_refused", False) in [(r[0], r[1]) for r in env["rows"]]
    assert got["reason"] == "captured-for-implementation"


def test_the_switch_leaves_only_the_capture_before_any_press(env, monkeypatch):
    monkeypatch.setenv(sc.MORE_ADS_PRESS_SETTING, "0")
    env["open"]()
    got = sc.capture_more_ads(env["page"], 2)
    assert got["stop"] == "press_off" and got["pressed"] == []
    assert _clicks(env["page"]) == []
    assert len(got["captures"]) == 1


def test_no_capture_is_named_not_created(env, monkeypatch):
    env["open"]()
    monkeypatch.setattr(pc, "capture", lambda *a, **k: "")
    monkeypatch.setenv(sc.MORE_ADS_PRESS_SETTING, "0")
    got = sc.capture_more_ads(env["page"], 1)
    assert got["reason"] == sc.MORE_ADS_NOT_CAPTURED
    row = [r for r in env["rows"] if r[0] == "simple_create_more_ads"][0]
    assert row[2] == "not-created" and row[3]["capture_note"] == "no page capture was saved"


def test_the_sentry_section_is_the_tree_and_the_open_menu(env):
    """Раздел для события Sentry — дерево и меню, а не карточка с фокусом."""
    env["open"](menu="no_create")
    sc.capture_more_ads(env["page"], 1)
    parts = pc._snapshot_take(env["page"], pc.section_titles_for("simple_create_more_ads"),
                              tree=pc.section_is_tree("simple_create_more_ads"))
    assert [p["name"] for p in parts][:2] == ["editor tree", "menu"]
    assert "editor_tree:adset" in parts[0]["html"] and "Duplicate" in parts[1]["html"]
    assert pc.section_is_tree("simple_create_more_ads") and not pc.section_is_tree("ad2_copy")
