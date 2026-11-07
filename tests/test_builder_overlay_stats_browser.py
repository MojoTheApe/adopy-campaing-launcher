"""Статистика Workbench/Home перестраивается по ширине своего контейнера.

Открытый билдер отъедает полэкрана, а окно остаётся широким: прежнее
`@media (max-width: 900px)` не срабатывало, и четыре плитки сжимались до
~100px, налезая друг на друга. Теперь ступени (4 → 2 → 1 колонка) считает
`@container`, поэтому проверяется ширина контейнера, а не окна.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_acl1799_workbench_browser import _html, browser  # noqa: E402,F401

# (ширина контейнера, ожидаемое число колонок)
STEPS = [(400, 1), (600, 2), (900, 4)]

MEASURE = """(sel) => {
    const grid = document.querySelector(sel.grid);
    const tiles = [...grid.querySelectorAll(sel.tile)];
    const rects = tiles.map(t => t.getBoundingClientRect());
    const overlaps = [];
    for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) {
        const a = rects[i], b = rects[j];
        if (a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5)
            overlaps.push([i, j]);
    }
    const clipped = tiles.filter(t => t.scrollWidth > t.clientWidth + 1).length;
    return {
        n: tiles.length, overlaps, clipped,
        cols: getComputedStyle(grid).gridTemplateColumns.split(' ').length,
        pageScroll: document.documentElement.scrollWidth - innerWidth,
    };
}"""


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1900, "height": 900})
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _html()

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    yield p
    assert not errors, errors
    ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,cols", STEPS)
def test_workbench_kpi_tiles_do_not_overlap_at_container_width(page, width, cols, theme):
    page.goto("http://acl.test/#country-saudi-arabia-workbench")
    page.evaluate("t => document.documentElement.dataset.theme = t", theme)
    # Окно остаётся 1900px: сжимаем только контейнер, как это делает открытый билдер.
    page.evaluate("w => { document.querySelector('.cw-panel.active').style.width = w + 'px'; }", width)
    m = page.evaluate(MEASURE, {"grid": ".cw-panel.active .cw-kpis", "tile": ".cw-kpi"})
    assert m["n"] == 4
    assert m["cols"] == cols, m
    assert m["overlaps"] == [], m
    assert m["clipped"] == 0, m
    assert m["pageScroll"] <= 0, m


@pytest.mark.parametrize("width,cols", [(400, 1), (600, 2), (900, 3)])
def test_home_strip_steps_down_with_container_width(page, width, cols):
    page.goto("http://acl.test/")
    # Полоса Home зависит от данных периода; в тестовой выборке её нет, поэтому
    # кладём ту же разметку руками — проверяется CSS страницы, а не шаблонный цикл.
    page.evaluate("""() => {
        const tile = k => '<div class="hk-tile"><div class="hk-label"><span>' + k + ' vs Others</span><span class="hk-chip">You</span></div>'
            + '<div class="hk-you">$1,234.56<small>of 99 attributed</small></div>'
            + '<div class="hk-others"><span>Others</span><b>$27</b></div><div class="hk-cap">0 FTD%</div></div>';
        const row = document.createElement('section');
        row.className = 'hk-row'; row.id = 'home-kpis';
        row.innerHTML = '<div class="hk-grid">' + ['a', 'b', 'c', 'd', 'e', 'f'].map(tile).join('') + '</div>';
        document.body.appendChild(row);
    }""")
    page.evaluate("w => { document.querySelector('#home-kpis').style.width = w + 'px'; }", width)
    m = page.evaluate(MEASURE, {"grid": "#home-kpis .hk-grid", "tile": ".hk-tile"})
    assert m["cols"] == cols, m
    assert m["overlaps"] == [], m
