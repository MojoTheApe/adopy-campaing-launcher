"""ACL-1361: поля ввода видны в обеих темах, серые ступени тёмной темы различимы.

Владелец прислал снимок карты аккаунтов и ящика билдера: поля Type, File,
Ad Title, CTA и кнопки строк сливались с почти чёрным фоном, а в светлой
теме поля были слишком бледными. Контраст считается, а не оценивается на
глаз: рамка поля — не меньше 3:1 ко всему, на чём поле стоит, и к самому
полю (WCAG 1.4.11), текст и подсказка в поле — не меньше 4.5:1 (AA).

Две половины. Первая — по токенам шаблона: быстро и без браузера. Вторая —
в Chromium на настоящей странице: вычисленные цвета полей ящика билдера,
чтобы правило компонента, вернувшееся к зашитому цвету или к --border,
не прошло мимо проверки токенов.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _design_tokens import tokens  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "src" / "web" / "templates" / "report.html"

LIGHT = ":root {"
DARK = ':root[data-theme="dark"] {'


def _css() -> str:
    t = TPL.read_text(encoding="utf-8")
    return t[t.index("<style>"): t.index("</style>")]


def _block(selector: str) -> str:
    css = _css()
    at = css.index(selector)
    return css[at: css.index("}", at)]


def _tokens(theme: str) -> dict[str, str]:
    """Токены темы с раскрытыми var() — как их считает браузер (ACL-1797)."""
    return tokens(theme)


def _rgb(colour: str) -> tuple[float, float, float]:
    colour = colour.strip()
    if colour.startswith("#"):
        h = colour.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.match(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", colour)
    assert m, f"не цвет: {colour!r}"
    return tuple(float(x) for x in m.groups())  # type: ignore[return-value]


def _lum(colour: str) -> float:
    def lin(c: float) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(colour)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# Поверхности, на которых стоят поля: фон страницы, карточка и ящик, подложка
# группы (строка страны в карте), наведённая строка — и само поле.
SURFACES = ("--bg", "--bg-elev", "--bg-subtle", "--bg-hover", "--field-bg")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_field_border_stands_out_from_every_surface(theme):
    t = _tokens(theme)
    for surface in SURFACES:
        got = _ratio(t["--field-border"], t[surface])
        assert got >= 3.0, f"{theme}: рамка поля к {surface} — {got:.2f}:1, нужно 3:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_and_placeholder_in_a_field_are_readable(theme):
    t = _tokens(theme)
    for name in ("--text", "--field-placeholder"):
        got = _ratio(t[name], t["--field-bg"])
        assert got >= 4.5, f"{theme}: {name} в поле — {got:.2f}:1, нужно 4.5:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_focus_ring_is_visible(theme):
    t = _tokens(theme)
    for surface in ("--field-bg", "--bg-elev"):
        got = _ratio(t["--focus-ring"], t[surface])
        assert got >= 3.0, f"{theme}: кольцо фокуса к {surface} — {got:.2f}:1"


def test_the_dark_greys_climb_in_steps():
    """Фон < карточка < подложка группы < наведённая строка; поле светлее карточки.

    Прежде фон, карточка и поле были одним почти чёрным. Каждая ступень
    светлее предыдущей, а соседние различимы — не одно и то же число.
    ACL-1797: лестница теперь из токенов Adopy; поле — «приподнятая»
    поверхность (surface-raised), а не самая светлая ступень, потому что у
    наведённой строки (surface-hover) светлота выше. Рамку поля к любой
    поверхности держит соседний тест — 3:1.
    """
    t = _tokens("dark")
    steps = ["--bg", "--bg-elev", "--bg-subtle", "--bg-hover"]
    for lower, upper in zip(steps, steps[1:]):
        got = _ratio(t[upper], t[lower])
        assert _lum(t[upper]) > _lum(t[lower]), f"{upper} не светлее {lower}"
        assert got >= 1.05, f"{lower} → {upper}: {got:.3f}:1 — ступень не видна"
    assert _ratio(t["--bg-elev"], t["--bg"]) >= 1.1, "ящик и карточки сливаются со страницей"
    assert _lum(t["--field-bg"]) > _lum(t["--bg-elev"]), "поле не светлее карточки"
    assert _ratio(t["--field-bg"], t["--bg-elev"]) >= 1.15, "поле сливается с карточкой"


# Правила полей, которые раньше брали фон страницы или карточки и тихую
# рамку разделителей. Каждое обязано брать токены поля.
FIELD_RULES = (
    ".cb-select, .cb-input {",
    ".cb-sel-text {",
    ".gform input {",
    ".report-box textarea {",
    ".amap-ml-signin input {",
    ".amap-add select, .amap-add input {",
    '.acc-field input[type="text"] {',
    '.acc-table input[type="text"], .acc-table select {',
    ".cbd-pick select {",
    ".cbd-pick.cbd-newc .cbd-new-country {",
)


@pytest.mark.parametrize("rule", FIELD_RULES)
def test_field_rules_use_the_field_tokens(rule):
    body = _block(rule)
    assert "var(--field-bg)" in body, f"{rule} красит фон мимо --field-bg"
    assert "var(--field-border)" in body, f"{rule} рисует рамку мимо --field-border"
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", body), f"{rule}: зашитый цвет"


# ── В браузере: вычисленные цвета полей ящика билдера ─────────────────────

def _page_html() -> str:
    from test_builder_per_account import _render
    return _render({"Demo": [], "Other": []})


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


# Для каждого видимого поля ящика: его фон, рамка, текст и первая непрозрачная
# поверхность под ним.
_MEASURE = """() => {
  const opaque = el => {
    for (let n = el.parentElement; n; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      if (c && c !== 'transparent' && !/rgba\\([^)]*,\\s*0\\)$/.test(c)) return c;
    }
    return getComputedStyle(document.body).backgroundColor;
  };
  const sel = '#cbd-panel .cb-input, #cbd-panel .cb-select, #cbd-panel .cb-sel-text, '
            + '#cbd-panel .cbd-pick select, #cbd-panel .cbd-new-country';
  return Array.from(document.querySelectorAll(sel))
    .filter(el => el.offsetParent !== null && !el.classList.contains('cb-language-empty'))
    .map(el => {
      const s = getComputedStyle(el);
      return {what: el.className || el.tagName, bg: s.backgroundColor,
              border: s.borderTopColor, text: s.color, under: opaque(el)};
    });
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_builder_fields_are_visible_in_the_real_page(browser, scheme):
    html = _page_html()
    context = browser.new_context(color_scheme=scheme)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        r.fulfill(json={"files": [], "texts": [], "profiles": [], "waiting": False})

    page.route("**/*", route)
    page.goto("http://acl.test/")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == scheme
    page.evaluate("cbDrawer.open()")
    page.evaluate("""() => {const f=document.querySelectorAll('form.open-account-form')[0];
        cbAddSlot(f); f.dispatchEvent(new Event('input',{bubbles:true}));}""")
    page.wait_for_function("document.querySelectorAll('.cbd-camp').length > 0")
    page.locator(".cbd-camp-row").first.click()
    page.wait_for_function("document.querySelectorAll('#cbd-panel .cb-sel-text').length > 0")

    fields = page.evaluate(_MEASURE)
    context.close()
    assert not errors, errors
    assert len(fields) >= 5, f"в ящике не нашлось полей: {fields}"
    for f in fields:
        who = f"{scheme} {f['what']}"
        assert _ratio(f["border"], f["bg"]) >= 3.0, f"{who}: рамка к полю {f}"
        assert _ratio(f["border"], f["under"]) >= 3.0, f"{who}: рамка к поверхности под полем {f}"
        assert _ratio(f["text"], f["bg"]) >= 4.5, f"{who}: текст в поле {f}"
