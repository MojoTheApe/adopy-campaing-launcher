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

    def __init__(self, log, *, visibility="visible", answers=True, fail=(), frames=True):
        self.context = FakeContext(log, fail)
        self.visibility, self.answers, self.frames = visibility, answers, frames
        self.log = log

    def is_closed(self):
        return False

    def wait_for_function(self, js, timeout=None, arg=None):
        if not self.answers:
            raise TimeoutError("Timeout 3000ms exceeded")
        if "requestAnimationFrame" in js and not self.frames:
            raise TimeoutError("Timeout 2000ms exceeded")
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

def test_a_window_under_another_window_is_covered():
    got = wv.screen_share((0, 0, 1000, 800), [(-10, -10, 1200, 900)], (0, 0, 1920, 1080))
    assert got == {"onscreen": 1.0, "uncovered": 0.0, "offscreen": False, "covered": True}


def test_a_half_covered_window_is_not_covered():
    got = wv.screen_share((0, 0, 1000, 800), [(0, 0, 500, 800)], (0, 0, 1920, 1080))
    assert got["uncovered"] == 0.5 and got["covered"] is False


def test_a_window_parked_off_screen_is_parked_not_covered():
    """Ревью PR #420, F1: за краем экрана — дом свёрнутого профиля у робота."""
    got = wv.screen_share((-1800, 0, -200, 900), [], (0, 0, 1920, 1080))
    assert got["onscreen"] == 0.0 and got["offscreen"] is True and got["covered"] is False


def test_the_on_screen_part_covered_counts_as_covered():
    got = wv.screen_share((-500, 0, 500, 800), [(0, 0, 600, 900)], (0, 0, 1920, 1080))
    assert got["onscreen"] == 0.5 and got["covered"] is True


def test_the_throttling_helper_records_whether_it_worked():
    log: list[str] = []
    page = FakePage(log)
    br._disable_background_throttling(page.context, page)
    assert br.THROTTLE_STATUS.get(page)["focus"] is True
    assert br.THROTTLE_STATUS.get(page)["lifecycle"] is True
    failing = FakePage(log, fail={"Page.setWebLifecycleState"})
    br._disable_background_throttling(failing.context, failing)
    assert br.THROTTLE_STATUS.get(failing)["lifecycle"] is False


def test_the_diag_has_page_window_and_helper(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    br._disable_background_throttling(page.context, page)
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "minimized": False,
                                                      "covered": True, "uncovered": 0.0})
    got = wv.visibility_diag(page)
    assert got["page"] == {"answered": True, "visibility": "hidden", "focus": True}
    assert got["win32"]["covered"] is True and got["reason"] == "covered"
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
    assert row[2]["diag"]["frames"] is True and row[2]["diag"]["reason"] == "page_hidden"
    assert env["notices"] == [wv.TEXTS_WOKEN["page_hidden"]]


def test_a_visible_tab_is_left_alone(env):
    log: list[str] = []
    page = FakePage(log, visibility="visible")
    got = wv.wake_if_hidden(page, where="x")
    assert got["hidden"] is False and not log
    assert not env["rows"] and not env["notices"]


def test_a_covered_window_counts_even_when_the_page_says_visible(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, visibility="visible")
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "covered": True})
    assert wv.wake_if_hidden(page, where="x")["woke"] is True
    assert env["notices"] == [wv.TEXT_WOKEN]


def test_the_wake_is_not_repeated_every_poll(env):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    wv.wake_if_hidden(page, where="x")
    assert wv.wake_if_hidden(page, where="x")["skipped"] == "recent"
    assert len([r for r in env["rows"] if r[0] == wv.WAKE_STEP]) == 1


def test_an_unanswering_hidden_tab_asks_the_person(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, answers=False)
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "covered": True})
    got = wv.wake_if_hidden(page, where="tab_frozen", answered=False)
    assert got["hidden"] and got["woke"] is False
    assert not log, "no CDP session on a tab that does not answer (ACL-1320)"
    assert env["notices"] == [wv.TEXT_FRONT]


def test_the_notice_is_shown_once_per_tab(env, monkeypatch):
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    wv.wake_if_hidden(page, where="x")
    wv.wake_if_hidden(page, where="x", force=True)
    assert env["notices"] == [wv.TEXTS_WOKEN["page_hidden"]]


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
        wv._WOKEN.set(p, wv._run_id())
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


# ── Ревью PR #420 ─────────────────────────────────────────────────────────

def test_a_parked_window_is_not_hidden_and_gets_no_notice(env, monkeypatch):
    """F1: окно за краем экрана (парковка робота) — ни строки, ни слов в панели;
    скринкаст свежей вкладке — молча."""
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "minimized": False,
                                                      "onscreen": 0.0, "offscreen": True,
                                                      "covered": False})
    got = wv.wake_if_hidden(page, where="create_next_ad", waited_s=40)
    assert got["hidden"] is False and got["before"]["parked"] is True
    assert got["actions"] == ["screencast_started"]
    assert not env["rows"] and not env["notices"]


def test_a_background_tab_in_a_visible_window_is_not_hidden(env, monkeypatch):
    """F2: страница «hidden», а Windows видит окно на экране — это фоновая
    вкладка (вперёд вернули вкладку человека), а не закрытое окно."""
    log: list[str] = []
    page = FakePage(log, visibility="hidden")
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "minimized": False,
                                                      "onscreen": 1.0, "uncovered": 1.0,
                                                      "offscreen": False, "covered": False})
    got = wv.wake_if_hidden(page, where="x")
    assert got["hidden"] is False and not env["rows"] and not env["notices"]


def test_a_failed_win32_read_falls_back_to_the_page(env, monkeypatch):
    page = FakePage([], visibility="hidden")
    monkeypatch.setattr(wv, "win32_state", lambda p: {"error": "OSError: x"})
    assert wv.visibility_diag(page)["reason"] == "page_hidden"


def test_a_minimised_window_says_minimised(env, monkeypatch):
    page = FakePage([], visibility="hidden")
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "minimized": True})
    assert wv.wake_if_hidden(page, where="x")["reason"] == "minimized"
    assert env["notices"] == [wv.TEXTS_WOKEN["minimized"]]


def test_no_frames_after_the_wake_is_not_called_woken(env, monkeypatch):
    """F4: страница отвечает, троттлинг снят, а кадров нет — не «разбудили»."""
    log: list[str] = []
    page = FakePage(log, visibility="visible", frames=False)
    monkeypatch.setattr(wv, "win32_state", lambda p: {"found": True, "covered": True})
    got = wv.wake_if_hidden(page, where="x")
    assert got["hidden"] and got["frames"] is False and got["woke"] is False
    row = [r for r in env["rows"] if r[0] == wv.WAKE_STEP][-1]
    assert row[1] is False
    assert env["notices"] == [wv.TEXT_FRONT]


def test_per_page_state_ignores_a_recycled_id():
    """F5: id умершей вкладки достался новой — старую запись не читаем."""
    class P:
        pass

    m = wv.PerPage()
    old, new = P(), P()
    m.set(old, 123)
    m._d[id(new)] = m._d.pop(id(old))     # как будто id(old) теперь у new
    assert m.get(new) is None and new not in m
    m.set(new, 7)
    assert m.get(new) == 7


def test_woken_is_per_run(env, monkeypatch):
    """F5: «будили» прошлого прогона не делает «не помогло» в новом."""
    page = FakePage([])
    run = {"id": "run-a"}
    monkeypatch.setattr(rt, "current_run_id", lambda: run["id"])
    wv._WOKEN.set(page, wv._run_id())
    assert wv.woken_recently(page) is True
    run["id"] = "run-b"
    assert wv.woken_recently(page) is False


def test_a_missing_browser_pid_is_looked_up_again(monkeypatch):
    """F6: порт ещё не слушал — через PID_RETRY_S ищем снова."""
    wv._reset_for_tests()
    page = FakePage([])
    wv.remember_cdp_url(page.context, "http://127.0.0.1:41235")
    clock = {"t": 1000.0}
    monkeypatch.setattr(wv.time, "time", lambda: clock["t"])
    found = iter([None, 5151])
    monkeypatch.setattr(wv, "_pid_listening", lambda port: next(found))
    assert wv.browser_pid(page) is None
    assert wv.browser_pid(page) is None, "within the retry window the miss is cached"
    clock["t"] += wv.PID_RETRY_S + 1
    assert wv.browser_pid(page) == 5151
    wv._reset_for_tests()


def test_a_long_creating_dialog_on_a_live_page_does_not_wake(env, monkeypatch):
    """F3: окно «Creating...» на отвечающей странице — Meta занята, не повод."""
    page = FakePage([])
    script = [{"ads": 1, "dialog": CREATING}] * 200
    _clock(monkeypatch, script)
    wakes = []
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: wakes.append(k) or {})
    sc._wait_new_ad(page, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=60)
    assert not wakes and not env["notices"]


def test_a_long_wait_without_a_dialog_asks_windows_only(env, monkeypatch):
    """F3: страница ответила — о видимости её не спрашиваем (без 3 с в опросе)."""
    page = FakePage([])
    script = [{"ads": 1}] * 200
    _clock(monkeypatch, script)
    wakes = []
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: wakes.append(k) or {})
    sc._wait_new_ad(page, 1, ["Sample Ad 01"], wait_s=30, busy_wait_s=60)
    assert wakes and all(k["probe"] is False and k["answered"] is True for k in wakes)


def test_unstick_still_reloads_when_the_tab_cannot_be_asked_after_the_wake(env, monkeypatch):
    """F7: после побудки `_page_responsive` вернул None — неответившую вкладку
    всё равно перезагружаем, а не пишем «woke»."""
    answers = iter([False, None])
    monkeypatch.setattr(md, "_page_responsive", lambda p, t=5.0: next(answers))
    monkeypatch.setattr(md, "_tab_gone", lambda p: False)
    monkeypatch.setattr(md, "_stall_evidence", lambda *a, **k: {})
    monkeypatch.setattr(md, "_UNSTUCK_RELOADED", set())
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "current_run_id", lambda: "run1")
    monkeypatch.setattr(wv, "wake_if_hidden", lambda p, **k: {"hidden": True, "woke": True})
    monkeypatch.setattr(wv, "visibility_diag", lambda p, **k: {})
    reloaded = []
    monkeypatch.setattr(md, "_reload_awake", lambda p, where: reloaded.append(where) or True)
    assert md._unstick(object(), "create", editor=False) == "reloaded"
    assert reloaded == ["create"]
