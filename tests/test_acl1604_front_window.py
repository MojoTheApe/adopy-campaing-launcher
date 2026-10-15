# -*- coding: utf-8 -*-
"""ACL-1604: разлогиненный профиль — окно на экран, вперёд; не закрывать и не убивать.

Всё на подделках: ни браузера, ни Multilogin. Проверяется, что делает
`browser.front_for_login`, когда он вызывается и что остаётся нетронутым.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402
from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow  # noqa: E402

URL = "https://business.facebook.com/business/loginpage/?next=x"
LIST_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
LOGIN_INTERRUPT = ('Page.goto: Navigation to "%s" is interrupted by another navigation to '
                   '"https://business.facebook.com/business/loginpage/?next=x"' % LIST_URL)


class Session:
    """Сессия БРАУЗЕРА: список целей, окно цели, активация цели."""

    def __init__(self, bounds):
        self.bounds, self.sent, self.detached = dict(bounds), [], False

    def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "T-other", "url": "https://x.test/"},
                                    {"type": "page", "targetId": "T-me", "url": URL}]}
        if method == "Browser.getWindowForTarget":
            assert params == {"targetId": "T-me"}, "чужая цель"
            return {"windowId": 7, "bounds": dict(self.bounds)}
        if method == "Browser.setWindowBounds":
            self.bounds.update((params or {}).get("bounds") or {})
            if (params or {}).get("bounds", {}).get("windowState") == "normal":
                self.bounds.setdefault("left", -1800)
        return {}

    def detach(self):
        self.detached = True


class Ctx:
    def __init__(self, bounds):
        self.session = Session(bounds)
        self.browser = SimpleNamespace(new_browser_cdp_session=lambda: self.session)

    def new_cdp_session(self, page):
        # Сессия на самой вкладке запрещена: на зависшей она висела 197 с.
        raise AssertionError("tab-level CDP session opened")


class FakePage:
    url = URL

    def __init__(self, bounds):
        self.context = Ctx(bounds)
        self.closed = 0

    def bring_to_front(self):
        raise AssertionError("page.bring_to_front on a possibly hung tab")

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
    assert ("Target.activateTarget", {"targetId": "T-me"}) in page.context.session.sent
    assert page.context.session.detached and page.closed == 0
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


def test_never_closes_kills_or_touches_another_target():
    page = FakePage({"windowState": "minimized", "left": -1800, "top": 0})
    br.front_for_login(page)
    assert page.closed == 0
    assert not [m for m in methods(page) if "close" in m.lower() or "crash" in m.lower()]
    assert [p for m, p in page.context.session.sent if m == "Target.activateTarget"] == [{"targetId": "T-me"}]


def test_a_broken_window_api_does_not_raise():
    page = FakePage({})
    page.context.browser = SimpleNamespace(
        new_browser_cdp_session=lambda: (_ for _ in ()).throw(RuntimeError("no cdp")))
    assert br.front_for_login(page) == {"restored": False, "moved": False, "front": False}


def test_a_hung_tab_never_gets_a_tab_level_call_and_the_stop_is_not_delayed():
    """Вкладка не отвечает: `new_cdp_session` / `bring_to_front` на ней не зовутся
    (FakePage роняет тест, если позвали) — только сессия браузера."""
    page = FakePage({"windowState": "normal", "left": 100, "top": 0, "width": 1500, "height": 900})
    started = time.monotonic()
    out = br.front_for_login(page)
    assert time.monotonic() - started < 3
    assert out["front"] is True


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


# --- проводка: удалить любой из вызовов — тест упадёт ------------------------

def _attach_env(monkeypatch, page):
    class _Ctx:
        pages = [page]

    browser = SimpleNamespace(contexts=[_Ctx()])
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: SimpleNamespace(start=lambda: pw))
    monkeypatch.setattr(br, "ensure_playwright_browsers_path", lambda: None)
    monkeypatch.setattr(br, "_unminimize_before_attach", lambda b: [])
    monkeypatch.setattr(br, "fresh_tab", lambda p, url, **kw: None)


def test_attach_to_a_silent_profile_on_the_login_page_fronts_the_window_then_stops(monkeypatch):
    class _Page:
        url = URL

        def wait_for_function(self, *a, **k):
            raise TimeoutError("Timeout 5000ms exceeded.")

    page = _Page()
    _attach_env(monkeypatch, page)
    fronted = []
    monkeypatch.setattr(br, "front_for_login", lambda p: fronted.append(p))
    with pytest.raises(pt.ProfileStop) as stop:
        br._connect_playwright(cdp_url="http://127.0.0.1:1", provider="multilogin_direct",
                               debug_port="1", ws_endpoint=None, check_responsive=True)
    assert stop.value.code == pt.LOGGED_OUT and fronted == [page]


def _fresh_env(monkeypatch, new):
    old = SimpleNamespace(url=LIST_URL, close=lambda **kw: None,
                          context=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True)))
    monkeypatch.setattr(br, "_open_tab_via_browser", lambda ctx, **kw: new)
    monkeypatch.setattr(br, "page_answers", lambda p, t=5.0: True)
    monkeypatch.setattr(br, "_disable_background_throttling", lambda c, p: None)
    monkeypatch.setattr(br, "_keep_renderer_active", lambda c, p: None)
    return old


def test_fresh_tab_login_on_goto_error_fronts_the_window(monkeypatch):
    class _New:
        url = "about:blank"
        context = SimpleNamespace()

        def goto(self, url, **kw):
            raise RuntimeError(LOGIN_INTERRUPT)

    new = _New()
    old = _fresh_env(monkeypatch, new)
    fronted = []
    monkeypatch.setattr(br, "front_for_login", lambda p: fronted.append(p))
    with pytest.raises(pt.ProfileStop):
        br.fresh_tab(old, LIST_URL)
    assert fronted == [new]


def test_fresh_tab_redirected_to_login_after_goto_fronts_the_window(monkeypatch):
    class _New:
        url = URL
        context = SimpleNamespace()

        def goto(self, url, **kw):
            return None

    new = _New()
    old = _fresh_env(monkeypatch, new)
    fronted = []
    monkeypatch.setattr(br, "front_for_login", lambda p: fronted.append(p))
    with pytest.raises(pt.ProfileStop):
        br.fresh_tab(old, LIST_URL)
    assert fronted == [new]
