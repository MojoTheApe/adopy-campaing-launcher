# -*- coding: utf-8 -*-
"""Окно браузера подгоняется под экран профиля, а не просит 1920×1040 вслепую (ACL-1214).

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
    def __init__(self, screen_cap):
        self.cap = screen_cap
        self.bounds = {"left": 0, "top": 0, "width": 1039, "height": 583, "windowState": "minimized"}
        self.sent = []

    def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7, "bounds": dict(self.bounds)}
        if method == "Browser.setWindowBounds":
            b = params["bounds"]
            if "windowState" in b:
                self.bounds["windowState"] = b["windowState"]
            if "width" in b:
                # Chrome молча не принимает окно больше экрана.
                if b["width"] <= self.cap[0] and b["height"] <= self.cap[1]:
                    self.bounds.update(width=b["width"], height=b["height"])
        return {}


class _Page:
    def __init__(self, avail):
        self.avail = avail

    def evaluate(self, js, *a):
        if "availWidth" in js:
            return list(self.avail)
        return None


class _Ctx:
    def __init__(self, session):
        self.session = session

    def new_cdp_session(self, page):
        return self.session


def test_a_small_profile_screen_gets_a_window_that_fits(monkeypatch):
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    s = _Session(screen_cap=(1536, 816))
    br._ensure_wide_window(_Ctx(s), _Page((1536, 816)))
    sizes = [p["bounds"] for m, p in s.sent if m == "Browser.setWindowBounds" and "width" in p["bounds"]]
    assert sizes[0]["width"] == 1536 and sizes[0]["height"] == 816
    assert s.bounds["windowState"] == "minimized", "свёрнутое окно сворачивается обратно"


def test_a_big_screen_still_gets_1920(monkeypatch):
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    s = _Session(screen_cap=(3840, 2112))
    br._ensure_wide_window(_Ctx(s), _Page((3840, 2112)))
    sizes = [p["bounds"] for m, p in s.sent if m == "Browser.setWindowBounds" and "width" in p["bounds"]]
    assert sizes[0]["width"] == 1920 and sizes[0]["height"] == 1040
