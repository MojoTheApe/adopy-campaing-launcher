# -*- coding: utf-8 -*-
"""Закрытая вкладка посреди объявления кончает работу сразу (ACL-1673).

7641d7b3d86a (B96, 07.10): `ad3_destination` ответил `page_closed`
(TargetClosedError), а поток ещё ~1.5 мин шёл по мёртвой странице —
`window_bounds` с пустыми границами, `ad_copy_fields not_revealed` ×2,
`ad3_copy`, — и только `ad4_create` назвал `browser_closed`. Теперь на
границе каждого шага объявления стоит один страж: вкладки нет — остановка
`browser_closed` с причиной `page_closed:<шаг>`, ни одного вызова страницы
после неё. До этого 8 минут молчал сам `ad3_destination`: его под-шаги
(`count()`, `keyboard`, `is_visible`) таймаута не соблюдают, и теперь перед
каждым стоит ограниченная проба `_tab_frozen`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from src.fb_ads_rpa import page_liveness  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.web import robot_health  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401
from tests.test_the_single_screen_editor_flow import _THREE, _creates, sim  # noqa: E402,F401


class TargetClosedError(Exception):
    """Как у Playwright: класс по имени (page_liveness.is_gone_error)."""


def _quiet(sim, monkeypatch):
    monkeypatch.setattr(dom, "_browser_addon_none_checked", lambda p: True)
    monkeypatch.setattr(dom, "_review_errors_visible", lambda p: False)
    monkeypatch.setattr(dom, "upload_ad_creative_with_retry", lambda p, ctype, path, **kw: True)


def test_a_tab_closed_at_the_first_ad_destination_stops_before_the_copy(sim, monkeypatch):
    _quiet(sim, monkeypatch)
    page = sim["page"]
    copies: list = []

    def destination(p, url):
        page.is_closed.return_value = True
        return False

    monkeypatch.setattr(dom, "set_ad_destination_website", destination)
    monkeypatch.setattr(dom, "write_ad_copy", lambda *a, **k: copies.append(a) or True)
    out: list[str] = []
    assert sim["run"](soft_fails_out=out) == "browser_closed"
    assert copies == [], "текст писали на закрытой вкладке"
    assert ("browser_closed", False, "page_closed") in sim["rows"]
    assert "page_closed:ad_destination" in out


def test_a_tab_closed_at_ad3_destination_does_no_more_page_work(sim, monkeypatch):
    """Точный случай 7641d7b3d86a: ad3_destination → page_closed."""
    _quiet(sim, monkeypatch)
    page = sim["page"]
    _creates(sim)
    windows: list = []
    copies: list = []
    dests: list = []

    def destination(p, url):
        dests.append(url)
        if len(dests) == 3:
            page.is_closed.return_value = True
            return False
        return True

    monkeypatch.setattr(dom, "set_ad_destination_website", destination)
    monkeypatch.setattr(dom, "write_ad_copy", lambda *a, **k: copies.append(k.get("ad_name")) or True)
    monkeypatch.setattr(br, "ensure_editor_window", lambda p, s: windows.append(s) or True)
    out: list[str] = []
    assert sim["run"](additional_ads=_THREE, soft_fails_out=out) == "browser_closed"
    assert copies == ["Example Ad", "Two"], "текст объявления 3 писали на закрытой вкладке"
    assert ("browser_closed", False, "page_closed") in sim["rows"]
    assert "page_closed:ad3_destination" in out
    assert not [w for w in windows if w == "ad_copy"], windows


def test_after_the_close_the_page_object_is_not_called_again(sim, monkeypatch):
    """Страж — локальные поля (`is_closed`): после закрытия ни `evaluate`, ни
    `wait_for_function` у страницы не зовут."""
    _quiet(sim, monkeypatch)
    page = sim["page"]
    seen = {}

    def destination(p, url):
        page.is_closed.return_value = True
        page.evaluate.reset_mock()
        page.wait_for_function.reset_mock()
        seen["closed"] = True
        return False

    monkeypatch.setattr(dom, "set_ad_destination_website", destination)
    monkeypatch.setattr(dom, "write_ad_copy", lambda *a, **k: pytest.fail("текст на мёртвой странице"))
    assert sim["run"](soft_fails_out=[]) == "browser_closed"
    assert seen["closed"]
    assert page.evaluate.call_count == 0
    assert page.wait_for_function.call_count == 0


def test_the_editor_window_check_on_a_gone_tab_makes_no_page_call(monkeypatch):
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw.get("note", ""))))
    page = MagicMock()
    page.is_closed.return_value = True
    monkeypatch.setattr(rt, "window_state", lambda p: pytest.fail("спросили окно мёртвой вкладки"))
    assert br.ensure_editor_window(page, "ad_copy") is False
    assert rows == [("window_bounds", False, "page_closed")]
    assert not page.method_calls[:0] and page.evaluate.call_count == 0


def test_a_page_marked_gone_by_a_failed_call_is_gone_for_the_next_step():
    page = MagicMock()
    page_liveness.mark_gone(page)
    assert page_liveness.tab_gone(page) is True


def test_the_destination_step_on_a_frozen_tab_stops_before_typing(monkeypatch):
    """ACL-1673: на зависшей вкладке под-шаги без таймаута не зовутся."""
    page = MagicMock()
    monkeypatch.setattr(dom, "_tab_frozen", lambda p: True)
    monkeypatch.setattr(dom, "destination_state", lambda *a, **k: {"state": "unreadable"})
    monkeypatch.setattr(dom, "_close_creative_setup_modal",
                        lambda p: pytest.fail("тронули зависшую вкладку"))
    assert dom.set_ad_destination_website(page, "https://example.com") is False
    assert dom.last_destination_why() == "tab_frozen"
    assert page.keyboard.press.call_count == 0 and page.keyboard.insert_text.call_count == 0


def test_the_destination_step_on_a_gone_tab_says_page_closed(monkeypatch):
    page = MagicMock()
    page.is_closed.return_value = True
    monkeypatch.setattr(dom, "_close_creative_setup_modal",
                        lambda p: pytest.fail("тронули ушедшую вкладку"))
    assert dom.set_ad_destination_website(page, "https://example.com") is False
    assert dom.last_destination_why() == "page_closed"


def test_the_destination_step_respects_the_step_budget(monkeypatch):
    """Зависшая вкладка не тянет шаг: срок шага выходит из `_tab_frozen`."""
    page = MagicMock()
    page.wait_for_function.side_effect = TimeoutError("Timeout 5000ms exceeded")
    monkeypatch.setattr(dom, "_window_wake", lambda *a, **k: {}, raising=False)
    dom._flow_budget_arm()
    try:
        dom._flow_budget.step_start -= dom.STEP_BUDGET_S + 1
        with pytest.raises(dom.FlowBudgetExceeded) as err:
            dom.set_ad_destination_website(page, "https://example.com")
        assert err.value.reason == "step_budget_exceeded"
    finally:
        dom._flow_budget_disarm()
    assert page.keyboard.press.call_count == 0


def test_wait_editor_saved_does_not_poll_a_gone_tab():
    page = MagicMock()
    page.is_closed.return_value = True
    dom.wait_editor_saved(page)
    assert page.get_by_text.call_count == 0


def test_robot_health_names_a_closed_tab_and_the_step():
    rows = [
        {"run_id": "r1", "ts": "2026-10-07T07:34:50+00:00", "step": "ad3_creative_upload", "ok": True},
        {"run_id": "r1", "ts": "2026-10-07T07:43:07+00:00", "step": "browser_closed", "ok": False,
         "note": "page_closed", "diag": {"where": "ad3_destination"}},
    ]
    run = robot_health.build_runs(rows)[0]
    assert run.abort_label == "tab closed at ad3_destination"
    summary = robot_health.run_summary([run], "r1")
    assert summary["abort_label"] == "tab closed at ad3_destination"
