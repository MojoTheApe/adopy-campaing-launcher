"""ACL-1797: токены Adopy, шкала шрифтов и две темы на каждом маршруте.

Две половины. Первая — по исходникам: токены подключены в страницу один раз,
палитра лежит в одном файле, старые имена шаблона ссылаются на неё, мелкого
текста нет, пары цвета держат нормы из adopy-design-contrast-check.md. Вторая —
в Chromium на настоящей странице без сети: у каждой вкладки обеих тем нет
горизонтальной прокрутки страницы при 1280 px, переключатель темы работает
тем же ключом, а размеры и ширина колонки равны токенам.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _design_tokens import TOKENS_CSS, tokens  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "src" / "web" / "templates"
PAGE = (TEMPLATES / "report.html").read_text(encoding="utf-8")


# ───────────────────────────── исходники ─────────────────────────────

def test_the_tokens_are_loaded_once_and_defined_in_one_file():
    assert PAGE.count('{% include "adopy-tokens.css" %}') == 1, "токены подключены не один раз"
    assert PAGE.count("--adopy-primary:") == 0, "значения токенов продублированы в шаблоне"
    css = TOKENS_CSS.read_text(encoding="utf-8")
    assert "--adopy-primary: #2563eb;" in css and ':root[data-theme="dark"]' in css


@pytest.mark.parametrize("name", ["loading.html", "robot_health.html", "brief.html", "error.html"])
def test_the_standalone_pages_use_the_same_tokens(name):
    src = (TEMPLATES / name).read_text(encoding="utf-8")
    assert '{% include "adopy-tokens.css" %}' in src, f"{name}: токены не подключены"
    assert "var(--adopy-" in src


def test_the_legacy_names_point_at_tokens():
    """`--bg`, `--accent`… не хранят цвет, а ссылаются на --adopy-*."""
    light = tokens("light")
    dark = tokens("dark")
    for name in ("--bg", "--bg-elev", "--text", "--muted", "--accent", "--good", "--bad", "--warn", "--field-border"):
        assert name in light and name in dark, f"{name} пропал"
    assert light["--accent"] == "#2563eb" and dark["--accent"] == "#83aaff"
    assert light["--text"] == "#202a39" and dark["--text"] == "#edf2fa"
    assert dark["--bg"] != light["--bg"], "тёмная тема не перекрашивает фон"


def test_the_builder_does_not_redeclare_the_tokens():
    assert "--adopy-surface-subtle: #f7f8fa" not in PAGE
    assert ".cbd-root { color: var(--adopy-text); }" in PAGE


def _lum(colour: str) -> float:
    h = colour.strip().lstrip("#")

    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


SURFACES = ("--adopy-bg", "--adopy-surface", "--adopy-surface-subtle", "--adopy-surface-raised", "--adopy-surface-hover")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_contrast_pairs_follow_the_contrast_check(theme):
    """Те же нормы, что в adopy-design-contrast-check.md: 4.5:1 для текста, 3:1 для рамок и фокуса."""
    t = tokens(theme)
    for surface in SURFACES:
        assert _ratio(t["--adopy-text"], t[surface]) >= 4.5, f"text на {surface}"
        assert _ratio(t["--adopy-text-muted"], t[surface]) >= 4.5, f"text-muted на {surface}"
        assert _ratio(t["--adopy-link"], t[surface]) >= 4.5, f"link на {surface}"
        assert _ratio(t["--adopy-control-border"], t[surface]) >= 3.0, f"control-border на {surface}"
        assert _ratio(t["--adopy-focus"], t[surface]) >= 3.0, f"focus на {surface}"
    assert _ratio(t["--adopy-on-primary"], t["--adopy-primary"]) >= 4.5
    assert _ratio(t["--adopy-selected-text"], t["--adopy-selected-bg"]) >= 4.5
    for fg, bg in (("success", "success-bg"), ("warning", "warning-bg"), ("danger", "danger-bg")):
        assert _ratio(t[f"--adopy-{fg}"], t[f"--adopy-{bg}"]) >= 4.5, fg
        assert _ratio(t[f"--adopy-{fg}"], t["--adopy-surface"]) >= 4.5, fg


def test_no_text_is_smaller_than_the_token_minimum():
    """Мельче 11 px (--adopy-text-xs) в интерфейсе текста нет.

    Исключение — макет объявления Facebook (.fb-feed-*, .fb-reels-*): это
    картинка чужой ленты в её масштабе, а не наш интерфейс.
    """
    css = PAGE[PAGE.index("<style>"): PAGE.index("</style>")]
    # Макет ленты занимает целый участок CSS — от .fb-feed-preview до .cb-status-panel.
    css = css[: css.index(".fb-feed-preview {")] + css[css.index(".cb-status-panel {"):]
    small = []
    for line in css.split("\n"):
        for m in re.finditer(r"font-size:\s*(\.?\d*\.?\d+)rem", line):
            if float(m.group(1)) < 0.6875:
                small.append(line.strip()[:90])
    assert not small, "мелкий текст вернулся: " + " | ".join(small[:5])


# ───────────────────────────── Chromium ─────────────────────────────

def _browser_or_skip():
    pw_mod = pytest.importorskip("playwright.sync_api")
    return pw_mod


@pytest.fixture(scope="module")
def page_html():
    import ds_routes_harness as H
    return H.render()


@pytest.fixture(scope="module")
def browser():
    pw_mod = _browser_or_skip()
    import ds_routes_harness as H
    with pw_mod.sync_playwright() as pw:
        b = H.browser_launch(pw)
        yield b
        b.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_no_route_scrolls_the_page_sideways_at_1280(browser, page_html, theme):
    """Главная и каждая вкладка страны: страница не шире окна. Широкое листается внутри секции."""
    import ds_routes_harness as H
    ctx, page, errors = H.open_page(browser, page_html, theme=theme, width=1280)
    try:
        page.wait_for_timeout(300)
        views = H.view_ids(page)
        assert "home" in views and any(v.startswith("country-") and v.endswith("-manage") for v in views)
        for view in views:
            H.show(page, view)
            got = page.evaluate(H.OVERFLOW_JS)
            assert got["scrollWidth"] <= got["inner"], f"{theme} {view}: страница шире окна {got}"
            assert got["bodyScrollWidth"] <= got["inner"], f"{theme} {view}: body шире окна {got}"
        assert not errors, errors
    finally:
        ctx.close()


def test_a_wide_table_scrolls_inside_its_own_section(browser, page_html):
    """Широкая таблица вкладки страны не растягивает страницу, а листается сама."""
    import ds_routes_harness as H
    ctx, page, errors = H.open_page(browser, page_html, width=1280)
    try:
        H.show(page, "country-mexico-targeting")
        page.evaluate("""() => {
          const td = document.querySelector('#view-country-mexico-targeting td.interest-cell');
          td.style.maxWidth = 'none'; td.style.whiteSpace = 'nowrap';
          td.textContent = 'x'.repeat(400);
        }""")
        got = page.evaluate(H.OVERFLOW_JS)
        assert got["scrollWidth"] <= got["inner"], f"таблица растянула страницу {got}"
        section = page.evaluate("""() => { const p = document.getElementById('view-country-mexico-targeting');
          return {scroll: p.scrollWidth, client: p.clientWidth, ox: getComputedStyle(p).overflowX}; }""")
        assert section["scroll"] > section["client"] and section["ox"] == "auto", section
    finally:
        ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_shell_type_and_surfaces_follow_the_tokens(browser, page_html, theme):
    import ds_routes_harness as H
    ctx, page, errors = H.open_page(browser, page_html, theme=theme, width=1280)
    try:
        H.show(page, "country-mexico-assets")
        t = tokens(theme)
        got = page.evaluate("""() => {
          const cs = (sel, p) => getComputedStyle(document.querySelector(sel))[p];
          return {
            theme: document.documentElement.getAttribute('data-theme'),
            rail: document.querySelector('.sidebar').getBoundingClientRect().width,
            mainMin: cs('.main-content', 'minWidth'),
            body: cs('body', 'backgroundColor'), bodyColor: cs('body', 'color'),
            bodySize: cs('body', 'fontSize'), bodyFont: cs('body', 'fontFamily'),
            title: cs('#view-country-mexico-assets h2.view-title', 'fontSize'),
            td: cs('#view-country-mexico-assets td', 'fontSize'),
            th: cs('#view-country-mexico-assets th', 'fontSize'),
            tableBg: cs('#view-country-mexico-assets table', 'backgroundColor'),
            primary: getComputedStyle(document.documentElement).getPropertyValue('--adopy-primary').trim(),
          };
        }""")
        assert got["theme"] == theme
        assert got["rail"] == 232, f"колонка не по токену: {got['rail']}"
        assert got["mainMin"] in ("0px", "0"), got["mainMin"]
        assert got["bodySize"] == "14px" and "system-ui" in got["bodyFont"]
        assert got["title"] == "20px" and got["td"] == "13px" and got["th"] == "12px", got

        def rgb(hexs):
            h = hexs.lstrip("#")
            return "rgb(%d, %d, %d)" % tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        assert got["body"] == rgb(t["--adopy-bg"]), got["body"]
        assert got["bodyColor"] == rgb(t["--adopy-text"]), got["bodyColor"]
        assert got["tableBg"] == rgb(t["--adopy-surface"]), got["tableBg"]
        assert got["primary"] == t["--adopy-primary"]
    finally:
        ctx.close()


def test_the_theme_switch_still_flips_the_theme_with_the_same_key(browser, page_html):
    import ds_routes_harness as H
    ctx, page, errors = H.open_page(browser, page_html, theme="light")
    try:
        before = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.click("#theme-toggle")
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
        assert page.evaluate("localStorage.getItem('acl.theme')") == "dark", "ключ хранилища сменился"
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") != before
        assert page.evaluate("Object.keys(localStorage).filter(k => /theme/i.test(k))") == ["acl.theme"]
    finally:
        ctx.close()
