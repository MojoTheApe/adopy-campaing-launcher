# -*- coding: utf-8 -*-
"""Закрытая вкладка посреди объявления кончает работу сразу (ACL-1673).

7641d7b3d86a (B96, 07.10): `ad3_destination` ответил `page_closed`
(TargetClosedError), а поток ещё ~1.5 мин шёл по мёртвой странице —
`window_bounds` с пустыми границами, `ad_copy_fields not_revealed` ×2,
`ad3_copy`, — и только `ad4_create` назвал `browser_closed`. Теперь на
границе каждого шага объявления стоит один страж: вкладки нет — остановка
`browser_closed` (шаг — в `diag.where`), ни одного вызова страницы
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
    assert not [f for f in out if f.startswith("page_closed")], out


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
    assert not [f for f in out if f.startswith("page_closed")], out
    assert windows[-3:] == ["ad", "ad2", "ad3"] and "ad_copy" not in windows, windows


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


# ══ B1: одна закрытая вкладка — одна блокирующая проблема ═════════════════

def test_a_tab_close_is_one_blocking_problem_with_the_tab_title(sim, monkeypatch):
    """Ревью PR #683, B1: ни `page_closed:<шаг>`, ни «Unclassified step»."""
    from src.fb_ads_rpa import failure_policy

    _quiet(sim, monkeypatch)
    page = sim["page"]

    def copy(p, *a, **k):
        page.is_closed.return_value = True
        return True

    monkeypatch.setattr(dom, "write_ad_copy", copy)
    out: list[str] = []
    assert sim["run"](soft_fails_out=out) == "browser_closed"
    found = failure_policy.problems([*out, "browser_closed"])
    blocking = [x for x in found if x["severity"] == failure_policy.BLOCKING]
    assert [x["title"] for x in blocking] == ["Browser tab closed during the build"], found
    assert not [x for x in found if x["title"].startswith("Unclassified")]


# ══ B2: вкладка закрыта на КАЖДОЙ границе — остановка на ней самой ═════════

BOUNDARIES = [
    # (имя, где закрыли, ожидаемое `where`)
    ("ad_window", "window:ad", "ad_window"),
    ("ad_name_fill", "overlays:ad", "ad_name"),
    ("ad_name_done", "row:ad_name", "ad_name"),
    ("ad_creative_upload", "upload:1", "ad_creative_upload"),
    ("ad_destination", "dest:1", "ad_destination"),
    ("ad_copy", "copy:1", "ad_copy"),
    ("ad2_create", "enter_extras", "ad2_create"),
    ("ad2_window", "window:ad2", "ad2_window"),
    ("ad2_name_fill", "overlays:ad2", "ad2_name"),
    ("ad2_name_done", "row:ad2_name", "ad2_name"),
    ("ad2_creative_upload", "upload:2", "ad2_creative_upload"),
    ("ad2_destination", "dest:2", "ad2_destination"),
    ("ad2_copy", "copy:2", "ad2_copy"),
    ("ad3_create", "listed:1", "ad3_create"),
    ("ad3_window", "window:ad3", "ad3_window"),
    ("ad3_creative_upload", "upload:3", "ad3_creative_upload"),
    ("ad3_destination", "dest:3", "ad3_destination"),
    ("ad3_copy", "copy:3", "ad3_copy"),
    ("ads_done", "listed:2", "ads_done"),
]


@pytest.mark.parametrize("name,trigger,where", BOUNDARIES, ids=[b[0] for b in BOUNDARIES])
def test_a_tab_closed_at_each_boundary_stops_right_there(sim, monkeypatch, name, trigger, where):
    """Ревью PR #683, B2: каждый страж держится отдельным тестом. Вкладка
    закрывается в шаге, который «удался» (или в самой границе), и поток обязан
    встать на ближайшем стороже: `browser_closed`, своё `where`, и ни одного
    вызова шагов, контекста страницы, снимка или имени после закрытия."""
    import contextlib

    from src.fb_ads_rpa import page_capture, screenshots
    from src.fb_ads_rpa import simple_create

    _quiet(sim, monkeypatch)
    page = sim["page"]
    _creates(sim)
    state = {"closed": False}
    after: list[str] = []
    counts: dict[str, int] = {}
    rows: list = []

    def close():
        page.is_closed.return_value = True
        state["closed"] = True

    def hit(kind: str) -> int:
        counts[kind] = counts.get(kind, 0) + 1
        if state["closed"]:
            after.append(kind)
        return counts[kind]

    def upload(p, ctype, path, **kw):
        if f"upload:{hit('upload')}" == trigger:
            close()
        return True

    def dest(p, url):
        if f"dest:{hit('dest')}" == trigger:
            close()
        return True

    def copy(p, *a, **k):
        if f"copy:{hit('copy')}" == trigger:
            close()
        return True

    def window(p, section):
        hit("window")
        if trigger == f"window:{section}":
            close()
            return False
        return True

    def overlays(p, where_, **kw):
        hit("overlays")
        if trigger == f"overlays:{where_}":
            close()
        return []

    def listed(p, nm, **kw):
        if f"listed:{hit('listed')}" == trigger:
            close()
        return {"listed": True, "ads": 2, "error": None}

    def record(step, ok=None, **kw):
        rows.append((step, ok, kw.get("note", ""), kw.get("diag")))
        if trigger == f"row:{step}" and ok is True and not state["closed"]:
            close()

    @contextlib.contextmanager
    def reloads_blocked(why=""):
        if trigger == "enter_extras":
            close()
        yield

    def watched(label, result):
        def fn(*a, **k):
            if state["closed"]:
                after.append(label)
            return result
        return fn

    monkeypatch.setattr(dom, "upload_ad_creative_with_retry", upload)
    monkeypatch.setattr(dom, "set_ad_destination_website", dest)
    monkeypatch.setattr(dom, "write_ad_copy", copy)
    monkeypatch.setattr(br, "ensure_editor_window", window)
    monkeypatch.setattr(dom, "_clear_blocking_overlays", overlays)
    monkeypatch.setattr(simple_create, "ad_listed", listed)
    monkeypatch.setattr(rt, "record", record)
    monkeypatch.setattr(dom, "_reloads_blocked", reloads_blocked)
    monkeypatch.setattr(dom, "ad_name_failure", watched("ad_name_failure", {"why": "x"}))
    monkeypatch.setattr(rt, "page_context", watched("page_context", {}))
    monkeypatch.setattr(page_capture, "capture", watched("page_capture", None))
    monkeypatch.setattr(screenshots, "capture", watched("screenshot", None))
    out: list[str] = []
    got = sim["run"](additional_ads=_THREE, soft_fails_out=out)
    assert state["closed"], f"граница {trigger} не была достигнута"
    assert got == "browser_closed", got
    closed_rows = [r for r in rows if r[0] == "browser_closed"]
    assert len(closed_rows) == 1 and closed_rows[0][2] == "page_closed", closed_rows
    assert (closed_rows[0][3] or {}).get("where") == where, closed_rows
    assert after == [], f"после закрытия звали: {after}"
    assert not [f for f in out if f.startswith("page_closed")], out


def test_the_destination_probe_comes_before_the_focus_click(monkeypatch):
    """Ревью PR #683, N1: проба — до фокуса и ввода; закрытая в этот момент
    вкладка не получает ни нажатия, ни Ctrl+A."""
    page = MagicMock()
    monkeypatch.setattr(dom, "_close_creative_setup_modal", lambda p: True)
    monkeypatch.setattr(dom, "_show_destination_section", lambda p: True)
    monkeypatch.setattr(dom, "_ensure_destination_website", lambda p: ("", {}))
    monkeypatch.setattr(dom, "_switch_off_instant_form", lambda p: {})

    def mark(p, *a, **k):
        page.is_closed.return_value = True
        return True

    monkeypatch.setattr(dom, "mark_ad_copy_field", mark)
    monkeypatch.setattr(dom, "_focus_via_cdp_click",
                        lambda *a, **k: pytest.fail("нажимали в ушедшую вкладку"))
    assert dom.set_ad_destination_website(page, "https://example.com") is False
    assert dom.last_destination_why() == "page_closed"
    assert page.keyboard.press.call_count == 0


def test_a_failed_step_on_a_gone_tab_takes_no_context_markup_or_picture(sim, monkeypatch):
    """Ревью PR #683, N4: `_shot("fail_*")` на ушедшей вкладке пишет только строку."""
    from src.fb_ads_rpa import page_capture, screenshots

    _quiet(sim, monkeypatch)
    page = sim["page"]
    taken: list[str] = []

    def destination(p, url):
        page.is_closed.return_value = True
        return False

    monkeypatch.setattr(dom, "set_ad_destination_website", destination)
    monkeypatch.setattr(rt, "page_context", lambda p: taken.append("page_context") or {})
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: taken.append("page_capture"))
    monkeypatch.setattr(screenshots, "capture", lambda *a, **k: taken.append("screenshot"))
    assert sim["run"](soft_fails_out=[]) == "browser_closed"
    assert taken == [], taken
