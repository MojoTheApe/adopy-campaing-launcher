# -*- coding: utf-8 -*-
"""Окно, выведенное за край экрана при подключении, не разворачивается на
экране владельца и в конце кампании сворачивается (PR #314, ревью).

Было: `_unminimize_before_attach` ставил свёрнутое окно в обычное состояние
за краем экрана, следом `_ensure_wide_window` видел «normal» и разворачивал
его — на мониторе владельца. `put_window_back` потом ставил left/top
развёрнутому окну (Chrome такое отвергает), и сворачивание в том же `try`
не выполнялось: окно так и оставалось развёрнутым поверх работы человека
(решение 19.09 — так быть не должно).

Проверяется вся цепочка на поддельном Chrome, который, как настоящий,
не принимает координаты у развёрнутого/свёрнутого окна.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

SCREEN = (1920, 1040)


class _Window:
    """Одно окно Chrome: состояние + место. История всех состояний."""

    def __init__(self, state="minimized", left=100, top=50, width=1400, height=900):
        self.state, self.left, self.top, self.width, self.height = state, left, top, width, height
        self.states = [state]

    def bounds(self):
        return {"windowState": self.state, "left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    def set(self, b):
        geometry = {k for k in ("left", "top", "width", "height") if k in b}
        if "windowState" in b and geometry:
            raise RuntimeError("state cannot be combined with bounds")
        if geometry and self.state != "normal":
            raise RuntimeError("To resize minimized/maximized/fullscreen window, restore it to normal state first.")
        if "windowState" in b:
            self.state = b["windowState"]
            self.states.append(self.state)
            if self.state == "maximized":
                self.left, self.top = 0, 0
                self.width, self.height = SCREEN
        for k in geometry:
            setattr(self, k, b[k])


class _Session:
    def __init__(self, win: _Window, wid=7):
        self.win, self.wid = win, wid
        self.sent: list = []

    def send(self, method, params=None):
        params = params or {}
        self.sent.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "t1"}]}
        if method == "Browser.getWindowForTarget":
            return {"windowId": self.wid, "bounds": self.win.bounds()}
        if method == "Browser.setWindowBounds":
            assert params["windowId"] == self.wid
            self.win.set(params["bounds"])
            return {}
        return {}

    def detach(self):
        pass


class _Page:
    def __init__(self, ctx):
        self.context = ctx

    def evaluate(self, js, *a):
        if "devicePixelRatio" in js:
            return [1.0, int((self.context.win.width - 16) * self.context.zoom)]
        return None

    def bring_to_front(self):
        pass


class _Ctx:
    def __init__(self, win, zoom=1.0):
        self.win, self.zoom = win, zoom
        self.session = _Session(win)
        self.pages = [_Page(self)]

    def new_cdp_session(self, _page):
        return self.session


def _attach(monkeypatch, win, zoom=1.0):
    monkeypatch.setattr(br.time, "sleep", lambda *_: None)
    monkeypatch.delenv("FB_RPA_WINDOW_LEFT", raising=False)
    monkeypatch.delenv("FB_RPA_WINDOW_TOP", raising=False)
    ctx = _Ctx(win, zoom)
    browser = MagicMock()
    browser.contexts = [ctx]
    browser.new_browser_cdp_session.return_value = _Session(win)
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser
    launcher = MagicMock()
    launcher.start.return_value = pw
    monkeypatch.setattr(br, "sync_playwright", lambda: launcher)
    for name in ("ensure_playwright_browsers_path", "_stop_thread_playwright",
                 "_reset_asyncio_running_loop", "_disable_background_throttling",
                 "_keep_renderer_active"):
        monkeypatch.setattr(br, name, lambda *a: None)
    monkeypatch.setattr(rt, "window_state", lambda p: {"inner": [p.context.win.width - 16, 800]})
    monkeypatch.setattr(rt, "record", lambda *a, **kw: None)
    try:
        conn = br._connect_playwright(cdp_url="synthetic", provider="synthetic",
                                      debug_port=None, ws_endpoint=None)
    finally:
        br._pw_tls.playwright = None
    return conn


def test_a_window_parked_on_attach_is_never_maximized_and_ends_minimized(monkeypatch):
    win = _Window(state="minimized", left=100, top=50, width=1000)
    # Масштаб Mimic 0.8: окно 1368 даёт страницу ~1080 — уже редактора.
    conn = _attach(monkeypatch, win, zoom=0.8)
    try:
        assert "maximized" not in win.states, win.states
        assert win.state == "normal" and win.left < 0, "window must stay off the owner's screen"
        # Узкое окно добирает ширину размером, не выходя из-за края.
        assert conn.page.evaluate("devicePixelRatio")[1] >= br.MIN_EDITOR_WIDTH
        br.put_window_back(conn.page)
        assert win.state == "minimized"
        assert (win.left, win.top) == (100, 50)
        assert "maximized" not in win.states
    finally:
        br._OFFSCREEN.clear()


def test_a_window_already_on_screen_is_still_maximized_as_before(monkeypatch):
    """Окно, которое человек держит на экране (не свёрнутое), — прежнее поведение."""
    win = _Window(state="normal", left=100, top=50, width=1000)
    _attach(monkeypatch, win)
    try:
        assert win.state == "maximized"
        assert not br._OFFSCREEN
    finally:
        br._OFFSCREEN.clear()


def test_put_window_back_minimizes_even_a_maximized_window(monkeypatch):
    """Человек развернул окно посреди прогона: координаты такому окну Chrome
    не ставит, но свернуть окно всё равно надо."""
    monkeypatch.setattr(br.time, "sleep", lambda *_: None)
    monkeypatch.delenv("FB_RPA_WINDOW_LEFT", raising=False)
    win = _Window(state="maximized", left=0, top=0)
    ctx = _Ctx(win)
    br._OFFSCREEN[id(ctx)] = {"window_id": 7, "left": 100, "top": 50}
    try:
        br.put_window_back(ctx.pages[0])
    finally:
        br._OFFSCREEN.clear()
    assert win.state == "minimized"
    assert (win.left, win.top) == (100, 50)


def test_put_window_back_minimizes_when_the_place_is_refused(monkeypatch):
    monkeypatch.setattr(br.time, "sleep", lambda *_: None)
    monkeypatch.delenv("FB_RPA_WINDOW_LEFT", raising=False)
    win = _Window(state="normal", left=-2000, top=0)
    ctx = _Ctx(win)
    real = win.set

    def refuse_place(b):
        if "left" in b:
            raise RuntimeError("refused")
        real(b)

    win.set = refuse_place
    br._OFFSCREEN[id(ctx)] = {"window_id": 7, "left": 100, "top": 50}
    try:
        br.put_window_back(ctx.pages[0])
    finally:
        br._OFFSCREEN.clear()
    assert win.state == "minimized"
