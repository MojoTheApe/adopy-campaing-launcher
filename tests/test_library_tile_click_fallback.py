# -*- coding: utf-8 -*-
"""Плитка медиатеки: Playwright не дождался «устойчивости» — щёлкаем через CDP (ACL-1313).

Прогон 24.09 (SA Mix, da86e97cbf8b, телеметрия `ad_creative_reuse` 20:19:29):
копия файла в медиатеке нашлась (`tiles_found: 1`), а `tile.click` упал по
таймауту — Playwright ждёт, пока плитка перестанет двигаться, виртуальная
сетка под нагрузкой ему этого не даёт. Защита от повторной заливки после
этого оставила объявление без медиа.

`_click_library_tile` теперь при таком отказе щёлкает настоящим CDP-кликом по
точке плитки — но только если в этой точке сама плитка (не накладка поверх)
и выбор ещё не сдвинулся (второй клик по выбранной плитке снял бы выбор).

Разметка плитки — из диагностики `tile_shape.html` той записи телеметрии
(`fixtures/ads_manager/library_tile_2026_09_24.html`); «не устойчива» —
бесконечная CSS-анимация, как у дёргающейся сетки.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

TILE = (Path(__file__).parent / "fixtures" / "ads_manager" / "library_tile_2026_09_24.html").read_text(encoding="utf-8")

_JIGGLE = ("<style>@keyframes jiggle { from { transform: translateX(0) } to { transform: translateX(3px) } }"
           " [role=button] { animation: jiggle .15s linear infinite alternate; }</style>")
_OVERLAY = ("<div id='overlay' style='position:fixed; inset:0; z-index:10; background:rgba(0,0,0,.1)'"
            " onclick='window.__overlay++'></div>")


def _grid(*, counter: str = "0 selected", extra: str = "") -> str:
    return (f"<div role='dialog'><div><span>{counter}</span></div>"
            f"<div aria-label='grid' role='grid' style='position:relative'>{TILE}</div></div>{extra}")


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
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_human_jitter", lambda *a, **k: 0.0)
    cdp_clicks: list[tuple[float, float]] = []
    real_cdp = md._cdp_mouse_click

    def spy(pg, x, y):
        cdp_clicks.append((x, y))
        return real_cdp(pg, x, y)

    monkeypatch.setattr(md, "_cdp_mouse_click", spy)
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    p.cdp_clicks = cdp_clicks
    try:
        yield p
    finally:
        ctx.close()


def _load(page, html):
    page.set_content(html)
    page.evaluate("""() => {
      window.__tile = 0; window.__overlay = 0;
      document.querySelector('[role=button]').addEventListener('click', () => window.__tile++);
    }""")
    return page.locator("[role=button]").first


def test_a_steady_tile_is_clicked_the_ordinary_way(page):
    tile = _load(page, _grid())
    assert md._click_library_tile(page, tile, before=0) == ""
    assert page.evaluate("window.__tile") == 1
    assert page.cdp_clicks == [], "обычный клик прошёл — CDP не нужен"


def test_a_tile_that_never_stops_moving_is_clicked_through_cdp(page):
    """Ровно случай 24.09: `tile.click` — TimeoutError, плитка в точке своя."""
    tile = _load(page, _grid(extra=_JIGGLE))
    assert md._click_library_tile(page, tile, before=0) == ""
    assert len(page.cdp_clicks) == 1
    assert page.evaluate("window.__tile") == 1, "CDP-клик не дошёл до плитки"
    # Точка — на миниатюре (не на подписи): не ниже 48 px от верха плитки.
    box = tile.bounding_box()
    x, y = page.cdp_clicks[0]
    assert box["x"] - 4 <= x <= box["x"] + box["width"] + 4
    assert box["y"] <= y <= box["y"] + 48 + 1


def test_a_covered_tile_is_not_clicked_and_the_cover_is_named(page):
    """Поверх плитки накладка: клик по точке попал бы в неё — не щёлкаем."""
    tile = _load(page, _grid(extra=_OVERLAY))
    err = md._click_library_tile(page, tile, before=0)
    assert err.startswith("TimeoutError+blocked:DIV"), err
    assert len(err) <= 40
    assert page.cdp_clicks == []
    assert page.evaluate("window.__tile") == 0
    assert page.evaluate("window.__overlay") == 0


def test_a_selection_that_already_moved_is_not_clicked_again(page):
    """Клик Playwright упал, но выбор сдвинулся: второй клик снял бы выбор."""
    tile = _load(page, _grid(counter="1 selected", extra=_JIGGLE))
    assert md._click_library_tile(page, tile, before=0) == ""
    assert page.cdp_clicks == []
    assert page.evaluate("window.__tile") == 0


def test_a_tile_marked_selected_is_not_clicked_again(page):
    """Счётчик не сдвинулся, но плитка сама говорит «выбрана» — тоже наш выбор."""
    tile = _load(page, _grid(counter="1 selected", extra=_JIGGLE))
    page.evaluate("document.querySelector('[role=button]').parentElement"
                  ".setAttribute('data-test-decoration', 'selected')")
    assert md._click_library_tile(page, tile, before=1) == ""
    assert page.cdp_clicks == []


def test_every_library_tile_click_goes_through_the_fallback():
    """Все клики по плиткам в `_try_reuse_library_media` и снятии выбора —
    через `_click_library_tile`, без голого `tile.click`."""
    tree = ast.parse((ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("_try_reuse_library_media", "_library_undo_selection"):
        fn = fns[name]
        helper = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "_click_library_tile"]
        bare = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "click"
                and "tile" in ast.unparse(n.func.value)]
        assert helper, name
        assert not bare, (name, [ast.unparse(b) for b in bare])
