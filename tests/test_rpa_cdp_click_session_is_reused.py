# -*- coding: utf-8 -*-
"""Клик через CDP: одна сессия на вкладку, путь курсора прежний (ACL-1313).

Каждый `_cdp_mouse_click` открывал новую CDP-сессию и отсоединял её — при
трёх профилях 0,1–0,3 с на клик, 4–6 с на кампанию. Теперь сессия кэшируется
на странице; отвалившаяся (закрытая вкладка, переподключение) заменяется
свежей, пока ни одно событие не отправлено.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

PATH = [(300.0, 200.0), (250.0, 150.0), (200.0, 100.0)]


class _Session:
    def __init__(self, log, fail_first=0):
        self.log = log
        self.fail_first = fail_first
        self.detached = False

    def send(self, method, params):
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("Target page, context or browser has been closed")
        assert method == "Input.dispatchMouseEvent"
        self.log.append((params["type"], params["x"], params["y"]))

    def detach(self):
        self.detached = True


class _Context:
    def __init__(self, log, fail_first_session=0):
        self.log = log
        self.sessions: list[_Session] = []
        self.fail_first_session = fail_first_session

    def new_cdp_session(self, page):
        s = _Session(self.log, fail_first=self.fail_first_session if not self.sessions else 0)
        self.sessions.append(s)
        return s


class _Mouse:
    def __init__(self, log):
        self.log = log

    def move(self, x, y):
        self.log.append(("pw_move", x, y))

    def down(self):
        self.log.append(("pw_down",))

    def up(self):
        self.log.append(("pw_up",))


class _Page:
    def __init__(self, fail_first_session=0):
        self.log: list[tuple] = []
        self.context = _Context(self.log, fail_first_session)
        self.mouse = _Mouse(self.log)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(md, "_mouse_path", lambda page, x, y, start=None: list(PATH))
    monkeypatch.setattr(md, "_hit_mark", lambda page, x, y: None)
    monkeypatch.setattr(md, "_hit_changed", lambda page, x, y: False)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)


def _one_click(x=200.0, y=100.0):
    return [("mouseMoved", px, py) for px, py in PATH] + [
        ("mousePressed", x, y), ("mouseReleased", x, y)]


def test_three_clicks_share_one_session():
    page = _Page()
    for _ in range(3):
        md._cdp_mouse_click(page, 200.0, 100.0)
    assert len(page.context.sessions) == 1
    assert not page.context.sessions[0].detached


def test_the_pointer_path_is_unchanged():
    page = _Page()
    md._cdp_mouse_click(page, 200.0, 100.0)
    md._cdp_mouse_click(page, 200.0, 100.0)
    assert page.log == _one_click() * 2


def test_a_dead_cached_session_is_replaced_without_a_fallback_click():
    page = _Page(fail_first_session=1)
    md._cdp_mouse_click(page, 200.0, 100.0)
    assert len(page.context.sessions) == 2
    assert page.context.sessions[0].detached
    assert page.log == _one_click(), "двойной клик или клик мимо CDP"
    md._cdp_mouse_click(page, 200.0, 100.0)
    assert len(page.context.sessions) == 2, "свежая сессия не закэширована"


def test_a_failure_mid_click_falls_back_as_before(monkeypatch):
    """Событие уже ушло — повтора через CDP нет, дальше прежний путь Playwright."""
    page = _Page()
    md._cdp_mouse_click(page, 200.0, 100.0)
    session = page.context.sessions[0]
    calls = {"n": 0}
    real = session.send

    def flaky(method, params):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        real(method, params)

    session.send = flaky
    page.log.clear()
    md._cdp_mouse_click(page, 200.0, 100.0)
    assert page.log[:1] == [("mouseMoved", *PATH[0])]
    assert page.log[1:] == [("pw_move", 200.0, 100.0), ("pw_down",), ("pw_up",)]
    assert len(page.context.sessions) == 1 and session.detached
