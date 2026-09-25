# -*- coding: utf-8 -*-
"""Свёрнутое окно профиля не вешает задание (ACL-1313, прогон 25.09).

07:52–08:01 UTC оба задания стояли: после неудачной попытки робот свернул
окно и снимал список кампаний — CDP-снимок ждал кадра, которого свёрнутое
окно не рисует; подключение к свёрнутому профилю Doc21 стояло так же. Пошли
дальше, только когда владелец развернул окна руками.

Проверяется: снимок со скрытой страницы не делается вовсе (ни Playwright,
ни CDP); при подключении свёрнутые окна профиля выводятся в обычное
состояние за край экрана командами домена Browser, а развёрнутые не
трогаются; ошибка протокола не роняет подключение.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import screenshots as shots  # noqa: E402


class _ShotSession:
    """CDP-сессия страницы: окно отвечает на Browser.*, снимок — считается."""

    def __init__(self, page):
        self.page = page

    def send(self, method, params=None):
        self.page.cdp_calls.append(method)
        if method == "Browser.getWindowForTarget":
            if self.page.window is None:
                raise RuntimeError("Browser domain unavailable")
            return {"windowId": 1, "bounds": {"windowState": self.page.window}}
        if method == "Page.captureScreenshot":
            return {"data": "cG5n"}
        raise AssertionError(method)


class _Page:
    """`visibility` — что говорит документ; `window` — состояние окна
    (None — Browser.getWindowForTarget не отвечает)."""

    def __init__(self, visibility: str, window: str | None = "normal", playwright_ok: bool = True):
        self.visibility = visibility
        self.window = window
        self.playwright_ok = playwright_ok
        self.screenshot_calls = 0
        self.cdp_calls: list[str] = []

    def evaluate(self, js, *a):
        assert "visibilityState" in js
        return self.visibility

    def screenshot(self, **kw):
        self.screenshot_calls += 1
        if not self.playwright_ok:
            # Пока Playwright ждал, человек свернул окно.
            self.window = "minimized"
            raise TimeoutError("Timeout 5000ms exceeded")
        Path(kw["path"]).write_bytes(b"png")

    @property
    def context(self):
        page = self

        class _Ctx:
            def new_cdp_session(self, _p):
                return _ShotSession(page)
        return _Ctx()


def test_a_hidden_page_is_not_photographed_at_all(tmp_path):
    page = _Page("hidden", window=None)
    assert shots.capture(page, "01_campaigns", base_dir=tmp_path) == ""
    assert page.screenshot_calls == 0
    assert "Page.captureScreenshot" not in page.cdp_calls


def test_a_minimized_window_is_skipped_although_the_document_says_visible(tmp_path):
    """PR #314, ревью: эмуляция фокуса и скринкаст держат документ «visible»
    и в свёрнутом окне — решает состояние окна."""
    page = _Page("visible", window="minimized")
    assert shots.capture(page, "01_campaigns", base_dir=tmp_path) == ""
    assert page.screenshot_calls == 0
    assert "Page.captureScreenshot" not in page.cdp_calls


def test_no_cdp_snapshot_when_the_window_was_minimized_during_the_playwright_try(tmp_path):
    page = _Page("visible", window="normal", playwright_ok=False)
    shots.capture(page, "01_campaigns", base_dir=tmp_path)
    assert page.screenshot_calls == 1
    assert "Page.captureScreenshot" not in page.cdp_calls


def test_no_cdp_snapshot_when_the_window_state_is_unknown(tmp_path):
    page = _Page("visible", window=None)
    page.screenshot = lambda **kw: (_ for _ in ()).throw(TimeoutError("fonts"))
    shots.capture(page, "01_campaigns", base_dir=tmp_path)
    assert "Browser.getWindowForTarget" in page.cdp_calls
    assert "Page.captureScreenshot" not in page.cdp_calls


def test_cdp_fallback_still_works_on_a_normal_window(tmp_path):
    page = _Page("visible", window="normal")
    page.playwright_ok = False
    page.screenshot = lambda **kw: (_ for _ in ()).throw(TimeoutError("fonts"))
    path = shots.capture(page, "01_campaigns", base_dir=tmp_path)
    assert "Page.captureScreenshot" in page.cdp_calls
    assert Path(path).read_bytes() == b"png"


def test_a_visible_page_is_photographed_as_before(tmp_path):
    page = _Page("visible")
    path = shots.capture(page, "01_campaigns", base_dir=tmp_path)
    assert path and Path(path).exists() and page.screenshot_calls == 1


class _Session:
    def __init__(self, windows):
        # windows: targetId -> {"windowId": .., "state": ..}
        self.windows = windows
        self.sent: list[tuple[str, dict]] = []
        self.detached = False

    def send(self, method, params=None):
        params = params or {}
        self.sent.append((method, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": t} for t in self.windows]
                    + [{"type": "service_worker", "targetId": "sw"}]}
        if method == "Browser.getWindowForTarget":
            w = self.windows[params["targetId"]]
            return {"windowId": w["windowId"],
                    "bounds": {"windowState": w["state"], "left": 10, "top": 20, "width": 1368, "height": 900}}
        if method == "Browser.setWindowBounds":
            for w in self.windows.values():
                if w["windowId"] == params["windowId"] and "windowState" in params["bounds"]:
                    w["state"] = params["bounds"]["windowState"]
            return {}
        raise AssertionError(method)

    def detach(self):
        self.detached = True


class _Browser:
    def __init__(self, session):
        self.session = session

    def new_browser_cdp_session(self):
        return self.session


def _no_sleep(monkeypatch):
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.delenv("FB_RPA_WINDOW_LEFT", raising=False)


def test_a_minimized_profile_window_is_restored_offscreen_before_attach(monkeypatch):
    _no_sleep(monkeypatch)
    s = _Session({"t1": {"windowId": 7, "state": "minimized"}, "t2": {"windowId": 7, "state": "minimized"}})
    done = br._unminimize_before_attach(_Browser(s))
    assert done == [{"window": 7, "was": {"left": 10, "top": 20}}]
    moves = [p for m, p in s.sent if m == "Browser.setWindowBounds"]
    assert moves[0] == {"windowId": 7, "bounds": {"windowState": "normal"}}
    assert moves[1]["bounds"]["left"] < 0, "window must stay off the owner's screen"
    assert len(moves) == 2, "one window, handled once even with two tabs"
    assert s.detached


def test_a_normal_window_is_left_alone(monkeypatch):
    _no_sleep(monkeypatch)
    s = _Session({"t1": {"windowId": 3, "state": "normal"}})
    assert br._unminimize_before_attach(_Browser(s)) == []
    assert not [m for m, _ in s.sent if m == "Browser.setWindowBounds"]


def test_a_protocol_error_does_not_break_the_attach(monkeypatch):
    _no_sleep(monkeypatch)

    class _Broken:
        def new_browser_cdp_session(self):
            raise RuntimeError("no browser session")

    done = br._unminimize_before_attach(_Broken())
    assert done and "error" in done[0]


def test_the_owners_chosen_screen_is_respected(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setenv("FB_RPA_WINDOW_LEFT", "0")
    monkeypatch.setenv("FB_RPA_WINDOW_TOP", "0")
    s = _Session({"t1": {"windowId": 5, "state": "minimized"}})
    br._unminimize_before_attach(_Browser(s))
    moves = [p for m, p in s.sent if m == "Browser.setWindowBounds"]
    assert moves[1]["bounds"]["left"] == 0 and moves[1]["bounds"]["top"] == 0
