# -*- coding: utf-8 -*-
"""Второстепенные кнопки не похожи на поля ввода (ACL-1455, вариант C).

01.10 владелец: кнопки и поля выглядят одинаково. У `.amap-btn`, `.amap-x` и
`.acc-btn` была рамка полей (`--field-border`), почти тот же фон и серый текст
подсказки. Решение — кнопки-ссылки: синий текст, без рамки; рамка остаётся
только у полей. Проверяется вычисленный стиль в настоящем Chromium, а не текст
CSS: перебить правило мог любой селектор посильнее выше по файлу.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")


def _styles() -> str:
    """Все блоки <style> страницы — без Jinja внутри них."""
    blocks = re.findall(r"<style>(.*?)</style>", PAGE, re.S)
    # ACL-1797: палитра — отдельный файл токенов, подключённый в страницу через include.
    tokens = (ROOT / "src" / "web" / "templates" / "adopy-tokens.css").read_text(encoding="utf-8")
    return tokens + "\n".join(re.sub(r"\{[{%#].*?[}%#]\}", "", b, flags=re.S) for b in blocks)


_HTML = """<!doctype html><html %(theme)s><head><style>%(css)s</style></head><body>
<div class="amap">
  <input id="field" class="cb-input" placeholder="Profile name">
  <span class="amap-add"><select id="sel"><option>already connected…</option></select></span>
  <button id="btn" class="amap-btn">Read spend</button>
  <button id="primary" class="amap-btn primary">Read all</button>
  <button id="danger" class="amap-btn danger">Remove</button>
  <button id="bad" class="amap-btn amap-read amap-live-bad">failed</button>
  <button id="x" class="amap-x">×</button>
  <button id="acc" class="acc-btn">Connect</button>
  <button id="off" class="amap-btn" disabled>Select dates</button>
</div></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


def _look(browser, theme=""):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route("**/*", lambda r: r.abort())
    page.set_content(_HTML % {"css": _styles(), "theme": theme})
    got = page.evaluate("""() => {
      const out = {};
      for (const el of document.querySelectorAll('[id]')) {
        const s = getComputedStyle(el);
        out[el.id] = {border: s.borderTopColor, width: s.borderTopWidth, bg: s.backgroundColor,
                      color: s.color, weight: s.fontWeight};
      }
      const probe = (v) => { const d = document.createElement('i'); d.style.color = 'var(' + v + ')';
        document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; };
      out.tokens = {accent: probe('--accent'), bad: probe('--bad'), field: probe('--field-border'), muted: probe('--muted')};
      return out;
    }""")
    ctx.close()
    return got


TRANSPARENT = "rgba(0, 0, 0, 0)"


@pytest.mark.parametrize("theme", ["", 'data-theme="dark"'])
def test_a_secondary_button_has_no_field_frame_and_reads_as_a_link(browser, theme):
    s = _look(browser, theme)
    t = s["tokens"]
    for bid in ("btn", "acc"):
        assert s[bid]["border"] == TRANSPARENT, f"{bid}: рамка полей вернулась ({s[bid]['border']})"
        assert s[bid]["width"] == "1px", "ширина рамки сохранена — строки не прыгают"
        assert s[bid]["bg"] == TRANSPARENT
        assert s[bid]["color"] == t["accent"], f"{bid}: текст не цвета ссылки"
    assert s["field"]["border"] == t["field"], "у поля рамка должна остаться"
    assert s["field"]["border"] != s["btn"]["border"]


@pytest.mark.parametrize("theme", ["", 'data-theme="dark"'])
def test_red_stays_red_and_primary_stays_filled(browser, theme):
    s = _look(browser, theme)
    t = s["tokens"]
    for bid in ("danger", "bad", "x"):
        assert s[bid]["color"] == t["bad"], f"{bid}: красное состояние потеряло цвет"
    assert s["primary"]["bg"] == t["accent"], "главная кнопка должна остаться залитой"
    assert s["off"]["color"] == t["muted"], "выключенная кнопка должна выглядеть выключенной"
