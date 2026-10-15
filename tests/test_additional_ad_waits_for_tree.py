# -*- coding: utf-8 -*-
"""Второе объявление создаётся, только когда левое дерево смонтировано (ACL-1313).

Прогон 24.09: `create_additional_ad` стартовал на странице, где дерева ещё
нет. Снимки:

* 7364047ef03c/05 — вместо редактора спиннер маршрута
  `AdsPERouteLoadPlaceholder` (fixtures/ads_manager/route_placeholder_…);
* 7d2ce3ab1161/05 — после перезагрузки только шапка `nav-header-*` и меню
  действий шапки, строк дерева нет (breadcrumb_only_…);
* d3e690189a6e/04 — нормальное дерево: кампания, ад-сет с кнопкой «...»,
  объявление (editor_tree_adset_…).

`_wait_tree_ready` отвечает «готово» только на третьем, и шаг, не дождавшись
дерева, называет причину `tree_not_ready`, а не `no_click_point`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "ads_manager"
TREE = (FIX / "editor_tree_adset_2026_09_24.html").read_text(encoding="utf-8")
PLACEHOLDER = (FIX / "route_placeholder_2026_09_24.html").read_text(encoding="utf-8")
BREADCRUMB = (FIX / "breadcrumb_only_2026_09_24.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    from src.fb_ads_rpa import browser as br
    from src.fb_ads_rpa import page_capture

    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(br, "wake_page", lambda p: {})
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    # Телеметрия — во временный файл, не в живой output/.
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def _tree_page(html: str) -> str:
    # Дерево живёт в левой панели редактора.
    return f"<div style='width:360px'>{html}</div>"


# ── готовность дерева ──────────────────────────────────────────────────────

def test_a_mounted_tree_with_the_adset_menu_is_ready(page):
    page.set_content(_tree_page(TREE))
    assert page.evaluate(md._TREE_READY_JS) is True
    assert md._wait_tree_ready(page, timeout_s=0.5) is True


def test_a_route_placeholder_spinner_is_not_ready(page):
    page.set_content(PLACEHOLDER)
    assert page.evaluate(md._TREE_READY_JS) is False
    assert md._wait_tree_ready(page, timeout_s=0.3) is False


def test_a_breadcrumb_only_page_is_not_ready(page):
    """Шапка с именем кампании и её собственным меню действий — ещё не дерево."""
    page.set_content(BREADCRUMB)
    assert page.locator("[data-surface*='campaign-structure']").count() == 1
    assert md._wait_tree_ready(page, timeout_s=0.3) is False


def test_the_spinner_wins_over_a_stale_tree(page):
    """Смена маршрута: старое дерево ещё в DOM, но маршрут грузится."""
    page.set_content(_tree_page(TREE) + PLACEHOLDER)
    assert md._wait_tree_ready(page, timeout_s=0.3) is False


def test_a_tree_that_mounts_while_waiting_is_accepted(page):
    page.set_content(f"<div id='host'>{PLACEHOLDER}</div>")
    page.evaluate("html => setTimeout(() => { document.getElementById('host').innerHTML = html; }, 300)",
                  _tree_page(TREE))
    assert md._wait_tree_ready(page, timeout_s=5) is True


# ── сам шаг ────────────────────────────────────────────────────────────────

def test_the_step_does_not_click_blindly_before_the_tree_is_there(page, tmp_path, monkeypatch):
    real = md._wait_tree_ready
    monkeypatch.setattr(md, "_wait_tree_ready", lambda p, timeout_s=30.0: real(p, timeout_s=0.3))
    page.set_content(PLACEHOLDER)
    page.evaluate("window.__clicks = 0; document.addEventListener('click', () => window.__clicks++, true)")
    assert md.create_additional_ad(page, "Campaign_A") is False
    assert page.evaluate("window.__clicks") == 0
    row = [r for r in _rows(tmp_path) if r["step"] == "create_additional_ad"][-1]
    assert row["ok"] is False and row["note"] == "tree_not_ready"


_MENU_JS = """() => {
  window.__adset_menu = 0;
  const btn = document.querySelector(
    '[data-surface*="editor_tree:adset"][data-surface*="campaign-structure-action-menu-item"]');
  btn.addEventListener('click', () => {
    window.__adset_menu++;
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
}"""


def test_on_the_real_tree_the_adset_menu_opens_and_the_ad_is_created(page):
    page.set_content(_tree_page(TREE))
    page.evaluate(_MENU_JS)
    assert md.create_additional_ad(page, "Campaign_A") is True
    assert page.evaluate("window.__adset_menu") == 1
    assert page.get_by_placeholder("Enter your ad name here...").is_visible()
