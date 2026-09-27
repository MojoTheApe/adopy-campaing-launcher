# -*- coding: utf-8 -*-
"""Перезагрузка от сторожа снимает троттлинг Chrome заново (ACL-1320).

`_unstick` перезагружал не отвечающий список кампаний голым `page.reload`, и
страница после перезагрузки жила без `_disable_background_throttling` —
фоновой вкладке Chrome снова придерживал таймеры, и «Loading creation»
тянулась минутами (тот же случай, что 19.09 вечером у `recover_stuck_page`).
Теперь — `_reload_awake`: перезагрузка, ограниченная проверка живости и
троттлинг только живой странице.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    ev: list = []
    rows: list = []
    state = {"alive_after": True}
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_UNSTUCK_RELOADED", set())
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(md._telemetry, "current_run_id", lambda: "run1")
    monkeypatch.setattr(br, "wake_page", lambda p: ev.append("wake") or {})
    monkeypatch.setattr(br, "_disable_background_throttling", lambda ctx, p: ev.append("unthrottle"))

    class _Page:
        url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
        context = object()

        def __init__(self):
            self.reloaded = False

        def reload(self, **kw):
            ev.append("reload")
            self.reloaded = True

        def wait_for_function(self, js, arg=None, timeout=None):
            if self.reloaded and state["alive_after"]:
                return True
            raise TimeoutError(f"Timeout {timeout}ms exceeded.")

        def evaluate(self, *a, **k):
            raise AssertionError("страницу, которая не отвечает, не читаем")

    return SimpleNamespace(ev=ev, rows=rows, state=state, page=_Page())


def test_the_unresponsive_list_is_reloaded_and_unthrottled(env):
    assert md._unstick(env.page, "create", editor=False) == "reloaded"
    assert env.ev.index("reload") < env.ev.index("unthrottle"), env.ev
    row = [r for r in env.rows if r[0] == "loading_modal_reload"][-1]
    assert row[1] is True and row[2]["note"] == "unresponsive"


def test_a_tab_still_frozen_after_the_reload_gets_no_cdp_calls(env):
    env.state["alive_after"] = False
    assert md._unstick(env.page, "create", editor=False) == "reload_failed"
    assert "unthrottle" not in env.ev


def test_a_frozen_tab_gets_no_cdp_session_from_the_guard(env):
    """На вкладке, которая не отвечает, `wake_page` (CDP-сессия на ней самой)
    не зовётся: `new_cdp_session` на зависшем рендерере висел 197 с."""
    env.state["alive_after"] = False
    md._unstick(env.page, "create", editor=False)
    assert "wake" not in env.ev
    assert md._unstick(env.page, "ad_name", editor=True) == "unsaved_or_unknown"
    assert "wake" not in env.ev


def test_a_live_tab_is_still_woken(env):
    env.page.reloaded = True   # отвечает
    assert md._unstick(env.page, "create", editor=False) == "woke"
    assert env.ev == ["wake"]
