"""Параллельные запуски профилей (TEST9095y, 22.10 12:08–12:11).

Check plan по трём аккаунтам: B91 — обрыв MLX 10053 (а профиль поднялся),
9403A — `Page.enable` не ответил за 10 с на холодном старте, B97 — после
остановки следующая проверка прицепилась к умирающему браузеру.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import ProtocolError

from src.fb_api import cdp as cdp_mod
from src.fb_api import launch
from src.fb_api import profile as prof
from src.fb_api import stop_log
from src.meta_campaign_agent.multilogin_launcher import StartedProfile


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    # Журнал пилота и остановок — не на диск машины; холодные отметки — свои.
    monkeypatch.setattr(launch, "_record", lambda event: None)
    monkeypatch.setattr(stop_log, "refusal", lambda pid, log_=None: "")
    monkeypatch.setattr(launch, "_PACER", launch.StartPacer(gap=lambda: 0.0))
    from src.fb_api import mlx_session
    monkeypatch.setattr(mlx_session, "note_failure", lambda *a, **k: None)
    launch._COLD.clear()
    launch._COLD_PROFILE.clear()
    launch._READY.clear()
    yield
    launch._COLD.clear()
    launch._COLD_PROFILE.clear()
    launch._READY.clear()


def _abort() -> Exception:
    """Ровно как `requests` отдаёт обрыв MLX launcher (B91, 12:08:40)."""
    inner = ConnectionAbortedError(10053, "An established connection was aborted by the software "
                                          "in your host machine", None, 10053, None)
    return requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", inner))


class _Launcher:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def start_profile(self, folder_id, profile_id, *, headless=False):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _ok(port="7001", newly=True):
    return StartedProfile("P1", "F1", port=port, newly_started=newly)


def _open(launcher, probe=lambda pid: ("none", None), **kw):
    return prof.open_profile("P1", "F1", "A", launcher_factory=lambda i: launcher,
                             local_probe=probe, sleep=lambda s: None, **kw)


@pytest.fixture
def not_running(monkeypatch):
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("not_running", None))


# ───────────────────────── 10053 → один повтор ─────────────────────────

def test_launcher_abort_is_recognised_through_the_requests_wrapping():
    assert launch.is_launcher_connection_error(_abort())
    assert launch.is_launcher_connection_error(ConnectionResetError(10054, "reset"))
    assert not launch.is_launcher_connection_error(requests.exceptions.ReadTimeout("Read timed out"))
    assert not launch.is_launcher_connection_error(RuntimeError("Start profile failed HTTP 500"))


def test_launcher_abort_on_start_gets_one_delayed_retry(not_running):
    launcher = _Launcher(_abort(), _ok())
    opened = _open(launcher)
    assert launcher.calls == 2
    assert opened.port == "7001" and opened.started_now is True


def test_retry_answering_already_running_is_still_our_start(not_running):
    # До первого старта процесса не было: «уже запущен» на повторе — наш старт.
    launcher = _Launcher(_abort(), _ok(newly=False))
    assert _open(launcher).started_now is True


def test_profile_that_came_up_despite_the_abort_is_adopted_without_a_second_start(not_running):
    probes = iter([("none", None), ("port", 7555)])
    launcher = _Launcher(_abort())
    opened = _open(launcher, probe=lambda pid: next(probes))
    assert launcher.calls == 1                   # второго старта нет
    assert opened.port == "7555" and opened.started_now is True   # держит место, закроем мы
    assert launch.cold_since(7555) is not None   # и он холодный


def test_second_abort_is_raised_after_exactly_two_starts(not_running):
    launcher = _Launcher(_abort(), _abort())
    with pytest.raises(requests.exceptions.ConnectionError):
        _open(launcher)
    assert launcher.calls == 2


@pytest.mark.parametrize("error", [
    requests.exceptions.ReadTimeout("Read timed out"),
    RuntimeError("navigation failed: net::ERR_INVALID_AUTH_CREDENTIALS"),
])
def test_timeouts_and_proxy_auth_on_mlx_start_are_never_retried(not_running, error):
    launcher = _Launcher(error, _ok())
    with pytest.raises(Exception):
        _open(launcher)
    assert launcher.calls == 1


# ───────────────────────── темп стартов MLX ─────────────────────────

def test_mlx_starts_go_one_at_a_time_and_a_queued_start_waits_the_gap():
    slept = []
    pacer = launch.StartPacer(limit=lambda: 1, gap=lambda: 3.0, sleep=slept.append)
    inside, peak = [0], [0]
    lock = threading.Lock()

    def start():
        with pacer.slot():
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.05)
            with lock:
                inside[0] -= 1

    threads = [threading.Thread(target=start) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert peak[0] == 1
    assert len(slept) == 2 and all(0 < s <= 3.0 for s in slept)   # ждавшие — с паузой


def test_a_lone_start_never_sleeps():
    slept = []
    pacer = launch.StartPacer(limit=lambda: 1, gap=lambda: 3.0, sleep=slept.append)
    for _ in range(3):
        with pacer.slot():
            pass
    assert slept == []


def test_parallel_starts_setting_is_bounded(monkeypatch):
    monkeypatch.setenv(launch.PARALLEL_STARTS_ENV, "9")
    assert launch.parallel_starts() == 4
    monkeypatch.setenv(launch.PARALLEL_STARTS_ENV, "x")
    assert launch.parallel_starts() == launch.DEFAULT_PARALLEL_STARTS


# ───────────────────────── холодный бюджет CDP ─────────────────────────

class _Cdp:
    def __init__(self):
        self.timeouts: list[tuple[str, float]] = []

    def call(self, method, params=None, *, session_id="", timeout=10.0):
        self.timeouts.append((method, timeout))
        if method == "Target.createTarget":
            return {"targetId": "T"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S"}
        return {}

    def close(self):
        pass


def _open_tab(url="http://127.0.0.1:7001"):
    cdp = _Cdp()
    got = {}

    def connect(u, timeout):
        got["connect"] = timeout
        return cdp

    cdp_mod.PageTab.open(url, connect=connect)
    return got["connect"], cdp


def test_first_calls_after_our_start_get_the_cold_budget(monkeypatch):
    monkeypatch.setenv(launch.COLD_START_ENV, "45")
    launch.mark_started("7001", "P1")
    connect, cdp = _open_tab()
    assert connect == 45
    assert dict(cdp.timeouts)["Page.enable"] == 45
    assert all(t == 45 for _, t in cdp.timeouts)


def test_cold_budget_is_spent_on_the_first_tab_only():
    launch.mark_started("7001", "P1")
    _open_tab()
    connect, cdp = _open_tab()                  # вторая вкладка — обычные сроки
    assert connect == cdp_mod.CALL_S
    assert dict(cdp.timeouts)["Page.enable"] == cdp_mod.CALL_S


def test_a_profile_we_did_not_start_keeps_the_warm_limits():
    connect, cdp = _open_tab()
    assert connect == cdp_mod.CALL_S and dict(cdp.timeouts)["Page.enable"] == cdp_mod.CALL_S


def test_a_failed_cold_open_keeps_the_budget_for_the_next_try():
    launch.mark_started("7001", "P1")

    def connect(u, timeout):
        raise TimeoutError("slow")

    with pytest.raises(cdp_mod.CdpTimeout):
        cdp_mod.PageTab.open("http://127.0.0.1:7001", connect=connect)
    assert launch.cold_since(7001) is not None


def test_mlx_start_marks_the_port_cold(not_running):
    _open(_Launcher(_ok(port="7333")))
    assert launch.cold_since("http://127.0.0.1:7333") is not None


# ───────────────────────── остановка → свежий старт ─────────────────────────

MIMIC = "C:/Users/u/mlx/deps/mimic_154/chrome.exe"
PID = "550e8400-e29b-41d4-a716-446655440000"


def _proc(created):
    return SimpleNamespace(info={
        "name": "chrome.exe", "exe": MIMIC, "create_time": created,
        "cmdline": ["chrome.exe", f"--user-data-dir=C:/Users/u/mlx/profiles/ws/f/{PID}",
                    "--remote-debugging-port=63405"]})


def _probe(procs, stopped_at):
    return prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                 listens=lambda proc, port: True, stopped_at=stopped_at)


def test_browser_that_survived_our_stop_is_closing_not_attachable():
    # B97: остановлен в 12:10:03, порт ещё слушается — к нему не цепляемся.
    assert _probe([_proc(created=100.0)], stopped_at=200.0) == ("closing", 63405)


def test_browser_started_after_the_stop_is_attached():
    assert _probe([_proc(created=300.0)], stopped_at=200.0) == ("port", 63405)
    assert _probe([_proc(created=100.0)], stopped_at=None) == ("port", 63405)


def test_survivor_makes_open_profile_refuse_instead_of_timing_out():
    with pytest.raises(prof.ProfileError) as err:
        prof.open_profile("P1", "F1", "A", launcher_factory=lambda i: pytest.fail("no MLX"),
                          local_probe=lambda pid: ("closing", 63405), sleep=lambda s: None)
    assert err.value.code == "profile_closing"


def test_mlx_listing_a_profile_we_just_stopped_is_not_attached(monkeypatch):
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("running", "61000"))
    monkeypatch.setattr(stop_log, "refusal", lambda pid, log_=None: "closed 60 s ago")
    with pytest.raises(prof.ProfileError) as err:
        _open(_Launcher())
    assert err.value.code == "restart_cooldown"


def test_recent_stop_is_only_reported_during_the_cooldown(tmp_path):
    now = [1000.0]
    log = stop_log.StopLog(tmp_path / "stops.json", wall=lambda: now[0])
    log.record("P1")
    assert log.recent_stop_at("P1") == 1000.0
    now[0] += stop_log.restart_cooldown_s() + 1
    assert log.recent_stop_at("P1") is None


def test_stop_makes_the_warm_registry_forget_the_port(tmp_path):
    from src.fb_api import warm

    stops = stop_log.StopLog(tmp_path / "stops.json")
    reg = warm.WarmRegistry(store=warm.WarmStore(tmp_path / "w.json"), scan=lambda ids: {}, stops=stops)
    stops.on_stop(reg.forget_stopped)
    reg.register("P1", account="KLD_TEST", port=61000, dashboard_started=True)
    stops.record("P1")
    assert not reg.is_warm("P1")


def test_stop_drops_the_cold_mark():
    launch.mark_started("7001", "P1")
    launch.forget_profile("P1")
    assert launch.cold_since(7001) is None


# ───────────────────────── Check plan не превышает потолок ─────────────────────────

def test_check_plan_for_many_accounts_never_starts_more_profiles_than_the_ceiling(monkeypatch):
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as api
    from src.web import profile_slots as ps

    extra = {}
    if hasattr(ps, "OpenProfileProbe"):        # ACL-1642 (есть не во всех сборках)
        extra["open_probe"] = ps.OpenProfileProbe(check=lambda p: set(), background=False)
    queue = ps.ProfileSlots(
        memory=lambda: ps.MemoryReading(total_mb=64_000, available_mb=60_000),
        settings=lambda: ps.SlotSettings(ceiling=2),
        usage=ps.ProfileUsage(measure=lambda p: {}, background=False),
        tick_s=0.02, **extra)
    monkeypatch.setattr(_as, "_slot_queue", lambda: queue)
    monkeypatch.setattr(api, "_probe_local", lambda pid: ("none", None))
    live, peak, lock = [0], [0], threading.Lock()

    def open_profile(profile_id, folder_id, instance):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        return prof.OpenedProfile(profile_id, folder_id, instance, "7001", None, True, None,
                                  started_now=True)

    monkeypatch.setattr(api, "_open_profile", open_profile)

    def check(i):
        held = _as._ReadOpen(f"ACC{i}")
        entry = SimpleNamespace(profile_id=f"P{i}", folder_id="F", multilogin_instance="A")
        api._open_gated(entry, f"ACC{i}", held)
        time.sleep(0.1)                           # чтение плана в поднятом профиле
        with lock:
            live[0] -= 1
        queue.release(held.slot_id)              # как `_close_started`: профиль закрыт, место свободно

    threads = [threading.Thread(target=check, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] == 2


def test_check_plan_shows_accounts_waiting_for_a_free_slot(monkeypatch):
    import asyncio
    import json

    from src.analysis_agent import account_spend as _as
    from src.web import app as web

    class _Req:
        query_params = SimpleNamespace(getlist=lambda key: ["ACC1", "ACC2"])

    _as._set_wait_note("ACC1", _as.SLOT_WAIT_NOTE + " (1 ahead)")
    try:
        body = json.loads(asyncio.run(web.publish_plan_waiting(_Req())).body)
    finally:
        _as._set_wait_note("ACC1", "")
    assert body == {"waiting": {"ACC1": "Waiting for a free profile slot (1 ahead)"}}


def test_check_plan_page_polls_the_waiting_line():
    from pathlib import Path

    html = Path("src/web/templates/report.html").read_text(encoding="utf-8")
    start = html.index("window.cbCheckPlan = function")
    body = html[start:html.index("\n};", start)]
    assert "/publish-plan/waiting?" in body and "clearInterval(waitTimer)" in body


# ───────────────────────── п. 5/7: вход в прокси после холодного старта ─────────────────────────

AUTH = "net::ERR_INVALID_AUTH_CREDENTIALS"
FB = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"


class _NavCdp(_Cdp):
    def __init__(self, errors):
        super().__init__()
        self.errors = list(errors)
        self.urls = []

    def call(self, method, params=None, *, session_id="", timeout=10.0):
        if method == "Page.navigate":
            self.urls.append(params["url"])
            err = self.errors.pop(0) if self.errors else ""
            return {"frameId": "F", **({"errorText": err} if err else {})}
        return super().call(method, params, session_id=session_id, timeout=timeout)


def _nav_tab(errors, *, cold=True):
    if cold:
        launch.mark_started("7001", "P1")
    cdp = _NavCdp(errors)
    tab = cdp_mod.PageTab.open("http://127.0.0.1:7001", connect=lambda u, timeout: cdp)
    slept = []
    tab._sleep = slept.append
    return tab, cdp, slept


def test_first_navigation_after_cold_start_probes_the_proxy_before_facebook():
    tab, cdp, slept = _nav_tab([])
    tab.navigate(FB)
    assert cdp.urls == [launch.DEFAULT_PROXY_PROBE_URL, FB] and slept == []
    tab.navigate(FB)                              # дальше — без пробы
    assert cdp.urls[-1] == FB and len(cdp.urls) == 3


def test_proxy_auth_race_on_the_probe_is_retried_once_and_facebook_is_never_hit_unauthenticated():
    # B97 22.10: первая навигация — ERR_INVALID_AUTH_CREDENTIALS, через 15 с — загрузилась.
    tab, cdp, slept = _nav_tab([AUTH, ""])
    tab.navigate(FB)
    assert cdp.urls == [launch.DEFAULT_PROXY_PROBE_URL, launch.DEFAULT_PROXY_PROBE_URL, FB]
    assert slept == [launch.PROXY_RETRY_S]


def test_proxy_auth_error_on_the_retry_is_final():
    tab, cdp, slept = _nav_tab([AUTH, AUTH])
    with pytest.raises(cdp_mod.PageError, match="ERR_INVALID_AUTH_CREDENTIALS"):
        tab.navigate(FB)
    assert FB not in cdp.urls and len(slept) == 1


def test_proxy_auth_error_on_a_warm_profile_is_final():
    tab, cdp, slept = _nav_tab([AUTH], cold=False)
    with pytest.raises(cdp_mod.PageError):
        tab.navigate(FB)
    assert cdp.urls == [FB] and slept == []


def test_without_a_probe_the_first_cold_navigation_gets_one_retry(monkeypatch):
    monkeypatch.setenv(launch.PROXY_PROBE_ENV, "off")
    tab, cdp, slept = _nav_tab([AUTH, ""])
    tab.navigate(FB)
    assert cdp.urls == [FB, FB] and slept == [launch.PROXY_RETRY_S]


def test_a_probe_failing_for_another_reason_does_not_block_facebook():
    tab, cdp, slept = _nav_tab(["net::ERR_NAME_NOT_RESOLVED", ""])
    tab.navigate(FB)
    assert cdp.urls == [launch.DEFAULT_PROXY_PROBE_URL, FB] and slept == []


# ───────────────────────── п. 8: гонки старта и остановки ─────────────────────────

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_ready_means_json_version_answers_not_just_a_listening_port():
    clock = _Clock()
    answers = iter([False, False, True])
    ok, took = launch.wait_ready(7001, budget_s=40, answers=lambda p: next(answers),
                                 sleep=clock.sleep, clock=clock)
    assert ok and took == 2.0


def test_ready_wait_is_bounded_by_the_cold_budget():
    clock = _Clock()
    ok, took = launch.wait_ready(7001, budget_s=5, answers=lambda p: False, sleep=clock.sleep, clock=clock)
    assert not ok and took >= 5


def test_stop_waits_until_our_start_has_settled():
    clock = _Clock()
    launch.mark_started("7001", "P1", clock=clock)
    answers = iter([False, True])
    assert launch.wait_start_settled("P1", answers=lambda p: next(answers),
                                     sleep=clock.sleep, clock=clock) == "settled"
    assert launch.wait_start_settled("P1", answers=lambda p: pytest.fail("no wait"),
                                     sleep=clock.sleep, clock=clock) == "ready"


def test_stop_of_a_profile_we_did_not_start_never_waits():
    assert launch.wait_start_settled("OTHER", answers=lambda p: pytest.fail("no wait")) == "not_ours"


def test_stop_does_not_wait_past_the_start_budget():
    clock = _Clock()
    launch.mark_started("7001", "P1", clock=clock)
    clock.t += launch.cold_start_s() + 1
    assert launch.wait_start_settled("P1", answers=lambda p: False, sleep=clock.sleep,
                                     clock=clock) == "budget_over"


def test_after_stop_the_profile_is_closed_only_when_its_process_is_gone():
    clock = _Clock()
    seen = iter([("port", 61000), ("closing", 61000), ("none", None)])
    assert prof.wait_gone("P1", probe=lambda pid: next(seen), sleep=clock.sleep, clock=clock)
    assert clock.t == 2.0


def test_wait_for_the_stopped_process_is_bounded():
    clock = _Clock()
    assert not prof.wait_gone("P1", timeout_s=30, probe=lambda pid: ("port", 61000),
                              sleep=clock.sleep, clock=clock)
    assert 30 <= clock.t <= 31
