# -*- coding: utf-8 -*-
"""Окно браузера разворачивается под настоящий экран, а не растягивается до 1920×1040 (ACL-1214).

17.09: профиль S15 эмулирует экран 1536×864; запрос 1920×1040 Chrome не
применял, окно оставалось 1039×583, страница — 1136×542, и редактор уезжал в
горизонтальную прокрутку.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402


class _Session:
    """Chrome: maximized = настоящий монитор (`screen`), свёрнутое хранит размер."""

    def __init__(self, screen, width=1039, height=583, state="minimized"):
        self.screen = screen
        self.bounds = {"left": 0, "top": 0, "width": width, "height": height, "windowState": state}
        self.sent = []

    def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7, "bounds": dict(self.bounds)}
        if method == "Browser.setWindowBounds":
            b = params["bounds"]
            if b.get("windowState") == "maximized":
                self.bounds.update(width=self.screen[0], height=self.screen[1], windowState="maximized")
            elif "windowState" in b:
                self.bounds["windowState"] = b["windowState"]
            if "width" in b:
                self.bounds.update(width=b["width"], height=b["height"])
        return {}


class _Page:
    def __init__(self, session, scale=None):
        self.session = session
        self.scale = scale or (lambda b: 1.0)

    def evaluate(self, js, *a):
        if "devicePixelRatio" in js:
            w = self.session.bounds["width"]
            return [self.scale(w), w]
        return None


class _Ctx:
    def __init__(self, session):
        self.session = session

    def new_cdp_session(self, page):
        return self.session


def _no_sleep(monkeypatch):
    monkeypatch.setattr(br.time, "sleep", lambda s: None)


def test_a_minimised_window_is_maximised_to_the_real_screen_and_minimised_back(monkeypatch):
    _no_sleep(monkeypatch)
    s = _Session(screen=(1536, 816))
    br._ensure_wide_window(_Ctx(s), _Page(s))
    assert ("Browser.setWindowBounds", {"windowId": 7, "bounds": {"windowState": "maximized"}}) in s.sent
    assert not [p for m, p in s.sent if m == "Browser.setWindowBounds" and p["bounds"].get("width") == 1920]
    assert s.bounds["windowState"] == "minimized"


def test_the_window_is_never_made_bigger_than_the_screen(monkeypatch):
    """17.09 19:57: 1920×1040 не влезало в монитор — страница уезжала за край."""
    _no_sleep(monkeypatch)
    s = _Session(screen=(1280, 680), width=1280, height=752, state="normal")
    br._ensure_wide_window(_Ctx(s), _Page(s))
    assert s.bounds["width"] == 1280 and s.bounds["windowState"] == "maximized"


def test_a_window_the_user_maximised_is_left_alone(monkeypatch):
    _no_sleep(monkeypatch)
    s = _Session(screen=(1920, 1040), width=1920, height=1040, state="maximized")
    br._ensure_wide_window(_Ctx(s), _Page(s))
    assert not [m for m, p in s.sent if m == "Browser.setWindowBounds"]


def test_a_maximise_that_drops_the_scale_is_reverted(monkeypatch):
    """S15 17.09: окно больше дало масштаб 0.9 → 0.5; окно возвращается."""
    _no_sleep(monkeypatch)
    s = _Session(screen=(1536, 816))
    br._ensure_wide_window(_Ctx(s), _Page(s, scale=lambda w: 0.5 if w > 1100 else 0.9))
    assert s.bounds["width"] == 1039 and s.bounds["windowState"] == "minimized"
