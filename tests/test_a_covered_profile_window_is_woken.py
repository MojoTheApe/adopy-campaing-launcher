"""ACL-1402: окно профиля, закрытое другими окнами, не стопорит робота.

Живой прогон 30.09 (Facebook_1443, fba2c3f5): шаг цели ждал 5 минут, пока
владелец не открыл окно аккаунта. Здесь — только заглушки: ни одного
настоящего окна (на ноутбуке владельца идут живые кампании), Win32 и CDP
подменены.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import creative_compress as cc  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402
from src.fb_ads_rpa import window_visibility as wv  # noqa: E402


class FakeSession:
    def __init__(self, log, fail=()):
        self.log, self.fail = log, set(fail)

    def send(self, method, params=None):
        self.log.append(method)
        if method in self.fail:
            raise RuntimeError("cdp refused")
        if method == "Browser.getWindowForTarget":
            return {"windowId": 1, "bounds": {"windowState": "normal"}}
        return {}

    def on(self, *_a):
        pass

    def detach(self):
        pass


class FakeContext:
    def __init__(self, log, fail=()):
        self.log, self.fail = log, fail
        self.browser = None

    def new_cdp_session(self, page):
        return FakeSession(self.log, self.fail)


class FakePage:
    """Вкладка: отвечает ли и что говорит о видимости — по сценарию."""

    def __init__(self, log, *, visibility="visible", answers=True, fail=()):
        self.context = FakeContext(log, fail)
        self.visibility, self.answers = visibility, answers
        self.log = log

    def is_closed(self):
        return False

    def wait_for_function(self, js, timeout=None, arg=None):
        if not self.answers:
            raise TimeoutError("Timeout 3000ms exceeded")
        page = self

        class Handle:
            def json_value(self):
                if "visibilityState" in js:
                    return {"v": {"visibility": page.visibility, "focus": True}}
                return {"v": True}

            def dispose(self):
                pass
        return Handle()

    def bring_to_front(self):  # noqa: D401 - запрет
        raise AssertionError("the robot must never bring the profile window to the front")


@pytest.fixture
def env(monkeypatch):
    wv._reset_for_tests()
    br.THROTTLE_STATUS.clear()
    br._KEEPALIVE_PAGES.clear()
    rows: list[tuple] = []
    notices: list[str] = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(wv, "win32_state", lambda page: {})
    monkeypatch.setattr(br, "_restore_offscreen", lambda c, p: False)
    monkeypatch.setattr(br, "_KEEPALIVE_SESSIONS", [])
    token = cc._notice_sink.set(notices.append)
    try:
        yield {"rows": rows, "notices": notices}
    finally:
        cc._notice_sink.reset(token)
        wv._reset_for_tests()


# ── Что видно в телеметрии ────────────────────────────────────────────────

def test_a_window_under_another_window_is_occluded():
    got = wv.screen_share((0, 0, 1000, 800), [(-10, -10, 1200, 900)], (0, 0, 1920, 1080))
    assert got == {"onscreen": 1.0, "uncovered": 0.0, "occluded": True}


def test_a_half_covered_window_is_not_occluded():
    got = wv.screen_share((0, 0, 1000, 800), [(0, 0, 500, 800)], (0, 0, 1920, 1080))
    assert got["uncovered"] == 0.5 and got["occluded"] is False


def test_a_window_parked_off_screen_counts_as_hidden():
    got = wv.screen_share((-1800, 0, -200, 900), [], (0, 0, 1920, 1080))
    assert got["onscreen"] == 0.0 and got["occluded"] is True


def test_the_throttling_helper_records_whether_it_worked():
    log: list[str] = []
    page = FakePage(log)
    br._disable_background_throttling(page.context, page)
    assert br.THROTTLE_STATUS[id(page)]["focus"] is True
    assert br.THROTTLE_STATUS[id(page)]["lifecycle"] is True
    failing = FakePage(log, fail={"Page.setWebLifecycleState"})
    br._disable_background_throttling(failing.context, failing)
    assert br.THROTTLE_STATUS[id(failing)]["lifecycle"] is False


def test_the_diag_has_page_window_and_helper(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    br._disable_background_throttling(page.context, page)
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "minimized": False,
                                                      "occluded": True, "uncovered": 0.0})
    got = wv.visibility_diag(page)
    assert got["page"] == {"answered": True, "visibility": "hidden", "focus": True}
    assert got["win32"]["occluded"] is True
    assert got["throttle"]["focus"] is True and got["hidden"] is True


def test_an_unanswering_tab_is_not_asked_again_for_visibility(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, answers=False)
    asked = []
    monkeypatch.setattr(wv, "page_visibility", lambda p, *a, **k: asked.append(1) or {})
    got = wv.visibility_diag(page, answered=False)
    assert got["page"] == {"answered": False} and not asked


def test_the_section_row_carries_visibility(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    monkeypatch.setattr(rt, "window_state",
                        lambda p: {"inner": [1700, 900], "minimized": False, "visibility": "hidden"})
    assert br.ensure_editor_window(page, "adset") is True
    row = [r for r in env["rows"] if r[0] == "window_bounds"][-1]
    assert row[2]["diag"]["visibility"]["page"]["visibility"] == "hidden"
    assert row[2]["diag"]["visibility"]["hidden"] is True


def test_win32_reading_is_skipped_without_a_known_browser(monkeypatch):
    wv._reset_for_tests()
    monkeypatch.setattr(wv, "_is_windows", lambda: True)
    assert wv.win32_state(FakePage([])) == {}


def test_the_browser_process_is_found_by_its_debug_port(monkeypatch):
    wv._reset_for_tests()
    page = FakePage([])
    wv.remember_cdp_url(page.context, "http://127.0.0.1:41234")
    monkeypatch.setattr(wv, "_is_windows", lambda: True)
    monkeypatch.setattr(wv, "_pid_listening", lambda port: 4242 if port == 41234 else None)
    seen = []
    monkeypatch.setattr(wv, "_win32_windows_state", lambda pid: seen.append(pid) or {"found": True})
    assert wv.win32_state(page) == {"found": True} and seen == [4242]
    wv._reset_for_tests()


# ── Побудка ───────────────────────────────────────────────────────────────

def test_a_hidden_tab_is_woken_without_the_foreground(env):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    got = wv.wake_if_hidden(page, where="conversion_event", waited_s=95)
    assert got["hidden"] and got["woke"]
    assert "Emulation.setFocusEmulationEnabled" in log and "Page.setWebLifecycleState" in log
    assert "Page.startScreencast" in log, "the tab got no keep-alive screencast"
    row = [r for r in env["rows"] if r[0] == wv.WAKE_STEP][-1]
    assert row[1] is True and row[2]["diag"]["waited_s"] == 95
    assert env["notices"] == [wv.TEXT_WOKEN]


def test_a_visible_tab_is_left_alone(env):
    log: list[str] = []
    page = FakePage(log, visibility="visible")
    got = wv.wake_if_hidden(page, where="x")
    assert got["hidden"] is False and not log
    assert not env["rows"] and not env["notices"]


def test_a_covered_window_counts_even_when_the_page_says_visible(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, visibility="visible")
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "occluded": True})
    assert wv.wake_if_hidden(page, where="x")["woke"] is True


def test_the_wake_is_not_repeated_every_poll(env):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    wv.wake_if_hidden(page, where="x")
    assert wv.wake_if_hidden(page, where="x")["skipped"] == "recent"
    assert len([r for r in env["rows"] if r[0] == wv.WAKE_STEP]) == 1


def test_an_unanswering_hidden_tab_asks_the_person(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, answers=False)
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "occluded": True})
    got = wv.wake_if_hidden(page, where="tab_frozen", answered=False)
    assert got["hidden"] and got["woke"] is False
    assert not log, "no CDP session on a tab that does not answer (ACL-1320)"
    assert env["notices"] == [wv.TEXT_FRONT]


def test_the_notice_is_shown_once_per_tab(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    wv.wake_if_hidden(page, where="x")
    wv.wake_if_hidden(page, where="x", force=True)
    assert env["notices"] == [wv.TEXT_WOKEN]


def test_nothing_here_raises_a_window():
    src = (ROOT / "src" / "fb_ads_rpa" / "window_visibility.py").read_text(encoding="utf-8")
    for call in (".bring_to_front(", "user32.SetForegroundWindow", "user32.BringWindowToTop",
                 "user32.ShowWindow", "user32.SetWindowPos", "user32.CreateWindow", "tkinter"):
        assert call not in src, call


# ── Решения «вкладка зависла» сначала будят ───────────────────────────────

def test_page_answers_wakes_a_hidden_tab_before_saying_frozen(env, monkeypatch):
    calls = iter([False, True])
    from src.fb_ads_rpa import page_liveness

    monkeypatch.setattr(page_liveness, "answers", lambda p, t=5.0: next(calls))
    woke = []
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: woke.append(k) or {"woke": True})
    assert br.page_answers(object(), 5.0) is True
    assert woke and woke[0]["answered"] is False


def test_page_answers_stays_false_when_the_window_is_not_hidden(env, monkeypatch):
    from src.fb_ads_rpa import page_liveness

    monkeypatch.setattr(page_liveness, "answers", lambda p, t=5.0: False)
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: {"hidden": False, "woke": False})
    assert br.page_answers(object(), 5.0) is False


def test_tab_frozen_tries_the_wake_first(env, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(md, "_page_responsive", lambda p, t=5.0: next(answers))
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: {"hidden": True, "woke": True})
    noted = []
    monkeypatch.setattr(md, "_flow_budget_note_frozen", lambda: noted.append(1))
    assert md._tab_frozen(object()) is False
    assert not noted, "a woken tab is not a frozen-tab episode"


def test_tab_frozen_still_frozen_when_the_wake_does_not_help(env, monkeypatch):
    monkeypatch.setattr(md, "_page_responsive", lambda p, t=5.0: False)
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: {"hidden": True, "woke": False})
    monkeypatch.setattr(md, "_flow_budget_note_frozen", lambda: None)
    monkeypatch.setattr(md, "_flow_budget_check", lambda: None)
    assert md._tab_frozen(object()) is True


CREATING = {"text": "Creating...", "surface": "/am/lib:ads_progress_dialog_modal_8e9c"}


def _clock(monkeypatch, script):
    clock = {"t": 1000.0}
    reads = iter(script)
    monkeypatch.setattr(sc, "_read_ads", lambda p: dict(next(reads)))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(sc.time, "time", lambda: clock["t"])
    monkeypatch.setattr(sc.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


def test_a_long_meta_busy_wait_wakes_a_hidden_window_and_keeps_the_limit(env, monkeypatch):
    page = FakePage([])
    script = [{"ads": 1, "dialog": CREATING}] + [{"error": "TimeoutError"}] * 200
    _clock(monkeypatch, script)
    wakes = []

    def wake(p, **k):
        wakes.append(k)
        wv._WOKEN.add(id(p))
        return {"hidden": True, "woke": True}
    monkeypatch.setattr(wv, "wake_if_hidden", wake)
    got = sc._wait_new_ad(page, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=60)
    assert got.get("error") and got["busy_s"] == 60.0, "the 120 s busy logic must stay intact"
    assert wakes and all(k["answered"] is False for k in wakes)
    assert wakes[0]["waited_s"] >= wv.WAKE_AFTER_S
    assert env["notices"] == [wv.TEXT_FRONT], "the wake did not help: ask the person"


def test_a_quick_meta_busy_wait_does_not_wake(env, monkeypatch):
    page = FakePage([])
    script = ([{"ads": 1, "dialog": CREATING}] + [{"error": "TimeoutError"}] * 5
              + [{"ads": 2, "name_value": "New Leads Ad"}] * 2)
    _clock(monkeypatch, script)
    wakes = []
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: wakes.append(k) or {})
    got = sc._wait_new_ad(page, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=120)
    assert got["name_stable"] and not wakes and not env["notices"]


def test_the_progress_model_puts_the_wake_on_hold():
    from scripts import acl_build_progress_model as m

    assert m.stage_of(wv.WAKE_STEP, m.read_model()["rules"]) == "hold"
