# -*- coding: utf-8 -*-
"""Маршрутный спиннер Meta — это загрузка, и на имени ad set'а есть перезагрузка (ACL-1313).

Снимок 7364047ef03c/05_create_additional_ad (24.09): вместо редактора —
`[data-non-int-surface*="AdsPERouteLoadPlaceholder"]` с `role=progressbar
aria-valuetext="Loading" data-is-placeholder`. Слова «Loading» на экране нет,
поэтому ни `_loading_visible`, ни `recover_stuck_page` зависшую страницу не
видели и не перезагружали. Разметка — fixtures/ads_manager/
route_placeholder_2026_09_24.html (уже в стенде).

Второе: у шага `adset_name` не было пути перезагрузки, как у
`campaign_name` — спиннер после Next сразу давал отказ кампании.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401

FIX = Path(__file__).parent / "fixtures" / "ads_manager"
PLACEHOLDER = (FIX / "route_placeholder_2026_09_24.html").read_text(encoding="utf-8")
TREE = (FIX / "editor_tree_adset_2026_09_24.html").read_text(encoding="utf-8")


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

    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(br, "wake_page", lambda p: {})
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def test_the_route_placeholder_is_a_loading_state(page):
    page.set_content(PLACEHOLDER)
    assert page.get_by_text(md._LOADING_TEXT).count() == 0, "предпосылка: слова Loading нет"
    assert md._loading_visible(page) is True


def test_a_mounted_editor_is_not_loading(page):
    page.set_content(f"<div style='width:360px'>{TREE}</div>")
    assert md._loading_visible(page) is False


def test_a_hidden_placeholder_is_not_loading(page):
    page.set_content(f"<div style='display:none'>{PLACEHOLDER}</div>")
    assert md._loading_visible(page) is False


def test_a_stuck_placeholder_with_saved_edits_is_reloaded(page, monkeypatch):
    monkeypatch.setattr(md, "LOADING_STUCK_S", 0.3)
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: True)
    page.set_content(PLACEHOLDER)
    reloads = []
    monkeypatch.setattr(page, "reload", lambda **kw: reloads.append(kw))
    assert md.recover_stuck_page(page, "adset_name", editor=True) is True
    assert len(reloads) == 1


def test_a_placeholder_that_clears_is_left_alone(page, monkeypatch):
    monkeypatch.setattr(md, "LOADING_STUCK_S", 3.0)
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: True)
    page.set_content(PLACEHOLDER + "<script>setTimeout(() => document.body.innerHTML = '', 300)</script>")
    reloads = []
    monkeypatch.setattr(page, "reload", lambda **kw: reloads.append(kw))
    assert md.recover_stuck_page(page, "adset_name", editor=True) is False
    assert reloads == []


# ── adset_name: путь перезагрузки ─────────────────────────────────────────

def test_the_adset_name_step_reloads_and_retries(flow, monkeypatch):
    state = {"broken": True, "recovered": []}
    page = flow["page"]
    plain = page.get_by_placeholder.return_value
    name_box = MagicMock()
    name_box.first = name_box

    def fill(value, **kw):
        if state["broken"]:
            raise RuntimeError("Timeout 8000ms exceeded")

    name_box.fill.side_effect = fill
    name_box.wait_for.side_effect = lambda **kw: None
    page.get_by_placeholder.side_effect = (
        lambda text, **kw: name_box if text == "Enter your ad set name here..." else plain)

    def recover(p, step, **kw):
        state["recovered"].append((step, kw.get("editor")))
        if step == "adset_name":
            state["broken"] = False
            return True
        return step == "objective"

    monkeypatch.setattr(md, "recover_stuck_page", recover)
    result = flow["run"]()
    assert ("adset_name", True) in state["recovered"], "перезагрузки на имени ad set'а не было"
    assert result != "adset_name"


def test_the_adset_name_step_still_fails_when_the_reload_is_refused(flow, monkeypatch):
    page = flow["page"]
    plain = page.get_by_placeholder.return_value
    name_box = MagicMock()
    name_box.first = name_box
    name_box.fill.side_effect = RuntimeError("Timeout 8000ms exceeded")
    page.get_by_placeholder.side_effect = (
        lambda text, **kw: name_box if text == "Enter your ad set name here..." else plain)
    monkeypatch.setattr(md, "recover_stuck_page", lambda p, step, **kw: step == "objective")
    assert flow["run"]() == "adset_name"


# ── objective_wait: что именно «грузилось» (E, 25.09) ─────────────────────

def test_the_loading_kind_names_the_route_placeholder(page):
    page.set_content(PLACEHOLDER)
    assert md._loading_kind(page) == "route_placeholder"


def test_the_loading_kind_carries_the_overlay_text(page):
    page.set_content("<div role='dialog'><span>Loading creation…</span></div>")
    assert md._loading_kind(page) == "text:Loading creation…"


def test_nothing_loading_has_no_kind(page):
    page.set_content("<div><span>Campaigns</span></div>")
    assert md._loading_kind(page) == ""
