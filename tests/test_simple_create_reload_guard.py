# -*- coding: utf-8 -*-
"""Охрана объявлений 2+ нового редактора (ACL-1353, ревью PR #365: F2, F4, F5).

Пока заполняется объявление N в `simple_create`, адрес страницы держит
объявление 1. Под `_reloads_blocked` (публичное имя `hold_open_ad`) никто не
перезагружает страницу и не открывает другое объявление через дерево —
перезагрузка и щелчок по строке дерева открыли бы редактор Leads-объявления
заново (это его портит). Здесь каждая такая ветка — по отдельности; вне
охраны поведение прежнее.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

HELD = "simple_create_new_ad"


@pytest.fixture
def rows(monkeypatch):
    got: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: got.append((step, ok, kw.get("note", ""))))
    monkeypatch.setattr(dom, "_sleep_h", lambda *a, **k: None)
    return got


def test_the_guard_is_reusable_and_nests(rows):
    """ACL-1390 (второй проход Locations) входит в ту же охрану по имени
    `hold_open_ad`; вложенный вход без причины внешнюю не снимает."""
    assert dom.hold_open_ad is dom._reloads_blocked
    assert dom.reloads_held() == ""
    with dom.hold_open_ad(HELD):
        assert dom.reloads_held() == HELD
        with dom.hold_open_ad(""):
            assert dom.reloads_held() == HELD
        with dom.hold_open_ad("simple_create_open_ad_n"):
            assert dom.reloads_held() == "simple_create_open_ad_n"
        assert dom.reloads_held() == HELD
    assert dom.reloads_held() == ""


# ── F5: три отказа от перезагрузки ─────────────────────────────────────────

def test_a_stalled_upload_of_ad_n_is_not_reloaded(monkeypatch, rows):
    page = MagicMock()
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1"
    reloads: list = []
    monkeypatch.setattr(dom, "upload_already_failed", lambda p: False)
    monkeypatch.setattr(dom, "upload_already_succeeded", lambda p: False)
    monkeypatch.setattr(dom, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(dom, "_page_read_trouble", lambda *a, **k: "")
    monkeypatch.setattr(dom, "media_refused_on_ad", lambda: None)
    monkeypatch.setattr(dom, "_close_creative_setup_modal", lambda p: True)
    monkeypatch.setattr(dom, "_verify_ad_media_present", lambda p: False)
    monkeypatch.setattr(dom, "_creative_left", lambda: 1000.0)
    monkeypatch.setattr(dom, "_upload_stalled", lambda p, **k: True)
    monkeypatch.setattr(dom, "_file_was_offered", lambda: False)
    monkeypatch.setattr(dom, "_reload_editor", lambda p, url="": reloads.append(url))
    monkeypatch.setattr(dom, "_upload_retry_can_reload", lambda p, url="": True)
    assert dom._upload_ad_creative_with_retry(page, "video", "b.mp4", edit_url="", reload=False) is False
    assert reloads == []
    assert dom.last_upload_why() == "upload_stalled"
    assert ("creative_upload_stalled", False, "reload_refused_new_ad_single_screen") in rows
    # Прежний путь (ad 1, прежний мастер): застрявшую загрузку перезагружают.
    dom._upload_ad_creative_with_retry(page, "video", "c.mp4", edit_url="")
    assert len(reloads) == 1


def _overlay_stubs(monkeypatch) -> list:
    reloads: list = []
    monkeypatch.setattr(dom, "_tab_frozen", lambda p: False)
    monkeypatch.setattr(dom, "_network_trouble", lambda p: False)
    monkeypatch.setattr(dom, "_close_ad_blocker_modal", lambda p: "ok")
    monkeypatch.setattr(dom, "_close_onboarding_tour", lambda p, where="": "")
    monkeypatch.setattr(dom, "_close_banners", lambda p, titles=(), **k: [])
    monkeypatch.setattr(dom, "_cancel_naming_template_dialog", lambda p: False)
    monkeypatch.setattr(dom, "_all_edits_saved", lambda p: True)
    monkeypatch.setattr(dom, "_reload_editor", lambda p, url="": reloads.append(url))
    return reloads


def test_the_ad_blocker_window_on_ad_n_is_closed_without_a_reload(monkeypatch, rows):
    reloads = _overlay_stubs(monkeypatch)
    page = MagicMock()
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1"
    closed = dom._clear_blocking_overlays(page, "ad2", may_reload=False)
    assert "ad_blocker_modal:ok" in closed
    assert "network_trouble:not_reloaded_new_ad" in closed
    assert reloads == []
    # Прежний путь: окно ad blocker — закрыть и перезагрузить.
    closed = dom._clear_blocking_overlays(page, "ad2")
    assert "network_trouble:reloaded" in closed and len(reloads) == 1


def test_the_copy_fields_are_not_revealed_by_a_reload_on_ad_n(monkeypatch, rows):
    page = MagicMock()
    monkeypatch.setattr(dom, "mark_ad_copy_field", lambda *a, **k: False)
    monkeypatch.setattr(dom, "_review_errors_visible", lambda p: False)
    monkeypatch.setattr(dom, "_scroll_until_mounted", lambda *a, **k: None)
    page.evaluate.return_value = False
    page.get_by_text.return_value.first.count.return_value = 0
    with dom._reloads_blocked(HELD):
        assert dom._reveal_ad_copy_fields(page) is False
    assert page.reload.call_count == 0 and page.goto.call_count == 0
    assert ("loading_modal_reload", False, "refused:" + HELD) in rows
    # Вне охраны — последняя мера прежняя: перезагрузка.
    try:
        dom._reveal_ad_copy_fields(page)
    except Exception:
        pass
    assert page.reload.call_count == 1


# ── F4: `_unstick` → `_reload_awake` ──────────────────────────────────────

def test_an_unresponsive_tab_is_not_reloaded_under_the_guard(monkeypatch, rows):
    page = MagicMock()
    monkeypatch.setattr(dom, "_page_responsive", lambda p, t=0: True)
    with dom._reloads_blocked(HELD):
        assert dom._reload_awake(page, "ad2_copy") is False
    assert page.reload.call_count == 0
    assert ("loading_modal_reload", False, "refused:" + HELD) in rows
    dom._reload_awake(page, "campaigns_list")
    assert page.reload.call_count == 1


# ── F2: ни одного открытия объявления через дерево ────────────────────────

def test_the_tree_is_not_clicked_under_the_guard(monkeypatch, rows):
    page = MagicMock()
    with dom._reloads_blocked(HELD):
        assert dom._open_tree_ad(page, "Example Ad 2") is False
    assert page.evaluate.call_count == 0 and page.mouse.click.call_count == 0
    assert ("ad_tree_open_refused", False, HELD) in rows


def test_a_wrong_open_ad_is_not_reopened_under_the_guard(monkeypatch, rows):
    reopened: list = []
    monkeypatch.setattr(dom, "_open_ad_name", lambda p: "Example Ad")
    monkeypatch.setattr(dom, "_reopen_ad_after_reload", lambda p, name: reopened.append(name) or True)
    with dom._reloads_blocked(HELD):
        assert dom._on_the_named_ad(MagicMock(), "Two") is False
        assert dom._on_the_named_ad(MagicMock(), "example ad") is True
    assert reopened == []
    # Прежний путь: не то объявление — открыть нужное в дереве.
    assert dom._on_the_named_ad(MagicMock(), "Two") is True
    assert reopened == ["Two"]


def _blocked_copy_stubs(monkeypatch) -> dict:
    calls: dict = {"sweep": [], "dest": [], "recover": []}
    monkeypatch.setattr(dom, "_lead_terms_not_accepted", lambda p: False)
    monkeypatch.setattr(dom, "_select_browser_addon_none", lambda p: True)
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: True)
    monkeypatch.setattr(dom, "verify_and_fix_ad_destinations",
                        lambda p, names, url: calls["sweep"].append(list(names)) or [])
    monkeypatch.setattr(dom, "set_ad_destination_website", lambda p, url: calls["dest"].append(url) or True)
    monkeypatch.setattr(dom, "recover_stuck_page", lambda p, where, **k: calls["recover"].append(where) or False)
    monkeypatch.setattr(dom, "_review_errors_visible", lambda p: False)
    return calls


def test_a_blocked_copy_editor_on_ad_n_is_repaired_on_the_open_ad(monkeypatch, rows):
    calls = _blocked_copy_stubs(monkeypatch)
    with dom._reloads_blocked(HELD):
        assert dom._repair_blocked_copy_editor(MagicMock(), "Two", "https://example.com") is True
    assert calls["sweep"] == [], "обход по дереву открыл бы объявление заново"
    assert calls["dest"] == ["https://example.com"]
    assert ("browser_addon_repair", True, "open_ad_only") in rows
    # Объявление 1 / прежний мастер — как раньше: обход по дереву.
    calls["dest"].clear()
    assert dom._repair_blocked_copy_editor(MagicMock(), "Example Ad", "https://example.com") is True
    assert calls["sweep"] == [["Example Ad"]] and calls["dest"] == []
