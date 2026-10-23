"""Render the real connection partial with production CSS and synthetic metadata."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch(
            executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None
        )
        yield instance
        instance.close()


def _render(theme, host, width):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    form = env.get_template("_account_map_add.html").render(
        # The builder borrows a country's actual form, including its Add controls.
        amap_value="Test country" if host in {"country", "builder"} else "",
        amap={"all_accounts": ["Synthetic account " + "x" * 120]},
        mlx={"A": "Test workspace", "B": "Other workspace"},
    )
    # This is the text inserted by the shared connection-state painter.
    form = form.replace("Multilogin: <i>checking…</i>",
                        "Multilogin: connected · " + "x" * 160 + "@example.invalid")
    # Cached results must stay hidden until the search handler reveals them.
    form = form.replace('<div class="amap-hits" hidden></div>',
                        '<div class="amap-hits" hidden><div class="amap-hit">'
                        'Synthetic cached profile</div></div>')
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>",
                                 (TEMPLATES / "report.html").read_text(encoding="utf-8"), re.S))
    wrapper = "cbd-connect-host" if host == "builder" else ""
    return (f'<html data-theme="{theme}"><head><style>{css}</style></head><body>'
            f'<div id="test-host" class="{wrapper}" style="width:{width}px">'
            f'{form}</div></body></html>')


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("host", ["country", "accounts", "builder"])
@pytest.mark.parametrize("width", [280, 720, 1040])
def test_connection_panel_contains_long_values_and_hidden_results(browser, theme, host, width):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.set_content(_render(theme, host, width))
        geometry = page.locator("#test-host").evaluate("""host => {
          const box = host.getBoundingClientRect();
          const results = host.querySelector('.amap-hits');
          return {width: host.clientWidth, scroll: host.scrollWidth,
            hiddenResultsHeight: results.getBoundingClientRect().height,
            hiddenResultsDisplay: getComputedStyle(results).display,
            overflow: Array.from(host.querySelectorAll('input, select, button')).filter(el => {
              const r = el.getBoundingClientRect();
              return r.width && (r.left < box.left - 1 || r.right > box.right + 1);
            }).map(el => el.className || el.textContent)};
        }""")
        assert geometry["hiddenResultsDisplay"] == "none", geometry
        assert geometry["hiddenResultsHeight"] == 0, geometry
        assert geometry["scroll"] <= geometry["width"] + 1, geometry
        assert not geometry["overflow"], geometry
        search = page.get_by_role("textbox", name="Find Multilogin profile")
        assert search.count() == 1
        search.focus()
        assert search.evaluate("el => el === document.activeElement")
        if host in {"country", "builder"}:
            known = page.get_by_role("combobox", name="Connected account")
            assert known.count() == 1
            # Add still obtains the selected account from its preceding sibling.
            assert known.evaluate("el => el.nextElementSibling.textContent.trim()") == "Add"
        results = page.locator(".amap-hits")
        results.evaluate("el => { el.hidden = false; }")
        assert results.is_visible()
        assert results.locator(".amap-hit").is_visible()
        results.evaluate("el => { el.hidden = true; }")
        assert not results.is_visible()
    finally:
        page.close()
