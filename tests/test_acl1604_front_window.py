# -*- coding: utf-8 -*-
"""ACL-1604: разлогиненный профиль — окно на экран, вперёд; не закрывать и не убивать.

Всё на подделках: ни браузера, ни Multilogin. Проверяется, что делает
`browser.front_for_login`, когда он вызывается и что остаётся нетронутым.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402


class Session:
    def __init__(self, bounds):
        self.bounds, self.sent = dict(bounds), []

    def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7, "bounds": dict(self.bounds)}
        if method == "Browser.setWindowBounds":
            self.bounds.update((params or {}).get("bounds") or {})
            if (params or {}).get("bounds", {}).get("windowState") == "normal":
                self.bounds.setdefault("left", -1800)
        return {}


class Ctx:
    def __init__(self, bounds):
        self.session = Session(bounds)

    def new_cdp_session(self, page):
        return self.session


class FakePage:
    def __init__(self, bounds):
        self.context = Ctx(bounds)
        self.fronted = 0
        self.closed = 0

    def bring_to_front(self):
        self.fronted += 1

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _telemetry(monkeypatch):
    rows = []
    from src.fb_ads_rpa import run_telemetry as rt
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    return rows


def methods(page):
    return [m for m, _ in page.context.session.sent]


def test_minimized_window_is_restored_moved_on_screen_and_fronted(_telemetry):
    page = FakePage({"windowState": "minimized", "left": -1800, "top": 0, "width": 1000, "height": 700})
    br._OFFSCREEN[id(page.context)] = {"window_id": 7, "left": 5, "top": 5}
    out = br.front_for_login(page)
    assert out == {"restored": True, "moved": True, "front": True}
    final = page.context.session.bounds
    assert final["windowState"] == "normal" and final["left"] == 40 and final["width"] >= 1200
    assert page.fronted == 1 and page.closed == 0
    # put_window_back больше не свернёт окно обратно
    assert id(page.context) not in br._OFFSCREEN
    assert _telemetry[-1][0] == "profile_front_for_login" and _telemetry[-1][1] is True


def test_offscreen_normal_window_is_moved_on_screen():
    page = FakePage({"windowState": "normal", "left": -1800, "top": 0, "width": 1500, "height": 900})
    out = br.front_for_login(page)
    assert out["moved"] and not out["restored"] and out["front"]
    assert page.context.session.bounds["left"] == 40


def test_visible_window_is_only_fronted_not_moved():
    page = FakePage({"windowState": "normal", "left": 300, "top": 100, "width": 1500, "height": 900})
    out = br.front_for_login(page)
    assert out == {"restored": False, "moved": False, "front": True}
    assert "Browser.setWindowBounds" not in methods(page)


def test_never_closes_or_kills_anything():
    page = FakePage({"windowState": "minimized", "left": -1800, "top": 0})
    br.front_for_login(page)
    assert page.closed == 0
    assert not [m for m in methods(page) if "close" in m.lower() or "Target." in m]


def test_a_broken_window_api_does_not_raise():
    class Broken(FakePage):
        def bring_to_front(self):
            raise RuntimeError("tab gone")

    page = Broken({})
    page.context.new_cdp_session = lambda p: (_ for _ in ()).throw(RuntimeError("no cdp"))
    assert br.front_for_login(page) == {"restored": False, "moved": False, "front": False}


def test_only_logged_out_brings_the_window_front(monkeypatch):
    called = []
    monkeypatch.setattr(br, "front_for_login", lambda page: called.append(page))
    page = object()
    br.front_if_logged_out(page, pt.NETWORK)
    br.front_if_logged_out(page, pt.RESTART_FAILED)
    br.front_if_logged_out(None, pt.LOGGED_OUT)
    assert called == []
    br.front_if_logged_out(page, pt.LOGGED_OUT)
    assert called == [page]


def test_flow_logged_out_stop_fronts_its_own_page_only(monkeypatch):
    from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow

    fronted = []
    monkeypatch.setattr(br, "front_for_login", lambda page: fronted.append(page))
    flow = object.__new__(AdsManagerFlow)
    flow.page = SimpleNamespace(name="mine")
    assert flow._record_profile_stop(pt.ProfileStop(pt.LOGGED_OUT, "login_form")) == pt.LOGGED_OUT
    assert [p.name for p in fronted] == ["mine"]
    flow._record_profile_stop(pt.ProfileStop(pt.NETWORK, "ERR_SOCKS_CONNECTION_FAILED"))
    assert len(fronted) == 1


def test_logged_out_message_tells_the_person_what_happens():
    msg = pt.message(pt.LOGGED_OUT)
    assert "brought to the front" in msg and "log in" in msg.lower()
