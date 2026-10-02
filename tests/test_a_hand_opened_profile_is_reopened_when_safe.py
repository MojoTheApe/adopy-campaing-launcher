# -*- coding: utf-8 -*-
"""Окно профиля, открытое руками, робот закрывает сам — когда безопасно (ACL-1388).

30.09 06:49 UTC «Publish all» встал на Facebook_6540: «profile … is open by
hand (no automation port) - not stopping it». С ACL-1320 такое окно робот не
трогает (остановка однажды потеряла несохранённую сессию Facebook), и
человек сам ищет окно и жмёт Publish заново.

Решение владельца: закрывать самому, но только когда ВСЁ сразу:
* браузеру профиля не меньше 15 минут (Multilogin успел сохранить вход);
* окно не на переднем плане и минуту не было ввода на машине;
* закрытие — мягкая остановка ЭТОГО профиля через MLX API, без kill;
* после подъёма вход проверяет обычный путь: разлогинен — `profile_logged_out`.
Любой провал/сбой — прежняя остановка `profile_open_by_hand` с прежним текстом.
Win32, процессы и MLX здесь подменены.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import hand_window as hw  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

PROFILE = "550e8400-e29b-41d4-a716-446655440000"
MESSAGE = ("This Multilogin profile is open by hand. Close its window "
           "(so Multilogin saves the session), then press Publish again.")
NOW = 1_800_000_000.0
HAND_PID = 4242


# ── Проверка безопасности (hand_window.assess) ─────────────────────────────

@pytest.fixture
def machine(monkeypatch):
    """Машина, на которой закрыть окно безопасно: окну 20 минут, на переднем
    плане проводник, ввода не было 5 минут. Тест портит одно условие."""
    monkeypatch.setenv(hw.ENV_ENABLED, "1")
    monkeypatch.delenv(hw.ENV_MIN_AGE, raising=False)
    monkeypatch.delenv(hw.ENV_MIN_IDLE, raising=False)
    state = SimpleNamespace(windows=True, created=[NOW - 20 * 60], fg=9999, parents={9999: [1]},
                            idle=300.0)

    def created(pids):
        if isinstance(state.created, Exception):
            raise state.created
        return list(state.created)

    def fg():
        if isinstance(state.fg, Exception):
            raise state.fg
        return state.fg

    def idle():
        if isinstance(state.idle, Exception):
            raise state.idle
        return state.idle

    def ancestors(pid):
        if isinstance(state.parents, Exception):
            raise state.parents
        return state.parents.get(pid, [])

    monkeypatch.setattr(hw, "_is_windows", lambda: state.windows)
    monkeypatch.setattr(hw, "_process_create_times", created)
    monkeypatch.setattr(hw, "_foreground_pid", fg)
    monkeypatch.setattr(hw, "_idle_seconds", idle)
    monkeypatch.setattr(hw, "_ancestors", ancestors)
    return state


def _assess():
    return hw.assess({HAND_PID}, now=NOW)


def test_an_old_idle_background_window_is_safe(machine):
    got = _assess()
    assert got.safe and got.reason == hw.SAFE
    assert got.age_s == pytest.approx(20 * 60)
    assert got.idle_s == 300.0


def test_the_thresholds_are_fifteen_minutes_and_sixty_seconds():
    assert hw.MIN_AGE_S == 15 * 60
    assert hw.MIN_IDLE_S == 60


def test_a_young_window_is_not_safe(machine):
    machine.created = [NOW - 14 * 60]
    got = _assess()
    assert not got.safe and got.reason == hw.TOO_YOUNG


def test_the_youngest_main_process_decides(machine):
    machine.created = [NOW - 3600, NOW - 60]
    assert _assess().reason == hw.TOO_YOUNG


def test_a_window_in_the_foreground_is_not_safe(machine):
    machine.fg = HAND_PID
    assert _assess().reason == hw.IN_FOREGROUND


def test_a_foreground_child_process_of_the_profile_is_not_safe(machine):
    machine.fg = 777
    machine.parents = {777: [HAND_PID, 1]}
    assert _assess().reason == hw.IN_FOREGROUND


def test_no_foreground_window_at_all_is_not_the_profile(machine):
    """Экран заблокирован — окна переднего плана нет."""
    machine.fg = 0
    assert _assess().safe


def test_recent_input_is_not_safe(machine):
    machine.idle = 59.0
    got = _assess()
    assert got.reason == hw.RECENT_INPUT and got.idle_s == 59.0


@pytest.mark.parametrize("broken,reason", [
    ("created", hw.AGE_UNKNOWN),
    ("fg", hw.FOREGROUND_UNKNOWN),
    ("parents", hw.FOREGROUND_UNKNOWN),
    ("idle", hw.IDLE_UNKNOWN),
])
def test_a_failing_read_is_not_safe(machine, broken, reason):
    if broken == "parents":
        machine.fg = 777
    setattr(machine, broken, OSError("Win32 said no"))
    got = _assess()
    assert not got.safe and got.reason == reason


def test_no_process_or_not_windows_is_not_safe(machine):
    assert hw.assess(set(), now=NOW).reason == hw.NO_PROCESS
    machine.windows = False
    assert _assess().reason == hw.NOT_WINDOWS


def test_the_real_win32_layer_answers_or_fails_closed(monkeypatch):
    """Настоящий ctypes-слой на этой машине отвечает числом. Сбой Win32 здесь —
    падение теста; в работе его ловит `assess` и считает «не безопасно»."""
    if sys.platform != "win32":
        pytest.skip("Windows only")
    assert hw._idle_seconds() >= 0
    assert hw._foreground_pid() >= 0


def test_thresholds_follow_the_machine_env(machine, monkeypatch):
    monkeypatch.setenv(hw.ENV_MIN_AGE, "3600")
    assert _assess().reason == hw.TOO_YOUNG
    monkeypatch.setenv(hw.ENV_MIN_AGE, "garbage")
    assert hw.min_age_s() == hw.MIN_AGE_S
    monkeypatch.setenv(hw.ENV_MIN_IDLE, "600")
    assert _assess().reason == hw.RECENT_INPUT


def test_the_switch_turns_it_off(machine, monkeypatch):
    monkeypatch.setenv(hw.ENV_ENABLED, "0")
    assert _assess().reason == hw.DISABLED


# ── Провайдер: закрыть мягко и поднять, или остановиться как раньше ────────

class _Launcher:
    def __init__(self, world):
        self.world = world

    def start_profile(self, folder_id, profile_id, *, headless=False):
        from src.meta_campaign_agent.multilogin_launcher import StartedProfile

        self.world.ev.append("start")
        if self.world.start_error:
            raise self.world.start_error
        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def stop_profile(self, profile_id, folder_id=None):
        from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError

        self.world.ev.append(("stop", profile_id))
        if self.world.stop_fails:
            raise MultiloginLauncherError("Stop profile failed HTTP 500")
        if not self.world.keeps_running:
            self.world.pids = frozenset()


@pytest.fixture
def world(monkeypatch):
    """Профиль открыт руками (процесс 4242, порта автоматизации нет)."""
    import src.meta_campaign_agent.multilogin_config as cfg_mod
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod
    from src.fb_ads_rpa import run_telemetry as rt

    w = SimpleNamespace(ev=[], pids=frozenset({HAND_PID}), stop_fails=False, keeps_running=False,
                        start_error=None, verdict=hw.Verdict(True, hw.SAFE, age_s=1500.0, idle_s=300.0),
                        rows=[], crumbs=[], notices=[])
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br.time, "sleep", lambda s: None)
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: "C:/mlx/p")
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: w.pids)
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: w.ev.append("kill"))
    monkeypatch.setattr(cfg_mod, "get_multilogin_instance", lambda inst: SimpleNamespace(instance_id=inst))
    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", lambda cfg: _Launcher(w))
    seen_pids: list = []
    monkeypatch.setattr(hw, "assess", lambda pids, **kw: seen_pids.append(frozenset(pids)) or w.verdict)
    w.seen_pids = seen_pids
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: w.rows.append((step, ok, kw)))
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "add_breadcrumb", lambda **kw: w.crumbs.append(kw))

    prov = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(prov._direct, "_try_attach_existing", lambda *a, **kw: None)
    monkeypatch.setattr(prov._direct, "_profile_process_running", lambda *a: bool(w.pids))
    monkeypatch.setattr(prov, "_connect_started",
                        lambda started, pid: w.ev.append("connect") or SimpleNamespace(page="reopened-page"))
    w.prov = prov
    return w


def _entry():
    return SimpleNamespace(profile_id=PROFILE, folder_id="f-1", multilogin_instance="A")


def _open(world, **kw):
    from src.fb_ads_rpa.creative_compress import observe_preparation

    with observe_preparation(world.notices.append):
        return world.prov.open(_entry(), "Facebook_6540", **kw)


def test_a_safe_hand_opened_window_is_closed_through_the_api_and_reopened(world):
    conn = _open(world)
    assert conn.page == "reopened-page"
    assert world.ev == [("stop", PROFILE), "start", "connect"], "не мягкая остановка этого профиля"
    assert "kill" not in world.ev
    assert world.seen_pids == [frozenset({HAND_PID})]
    assert br._robot_profile(PROFILE) is True


def test_the_reopen_is_visible_in_the_job_panel_telemetry_and_sentry(world):
    _open(world)
    assert world.notices == ["Closed the hand-opened window of Facebook_6540 and reopened it for the robot"]
    step, ok, kw = world.rows[-1]
    assert (step, ok, kw["note"]) == ("profile_hand_window", True, "reopened")
    assert kw["diag"]["where"] == "no_automation_port"
    assert kw["diag"]["reason"] == hw.SAFE
    assert kw["diag"]["age_s"] == 1500.0 and kw["diag"]["idle_s"] == 300.0
    assert PROFILE[:8] not in repr(world.rows)
    crumb = world.crumbs[-1]
    assert crumb["category"] == "profile" and crumb["message"] == "hand_window:reopened"
    assert crumb["data"]["age_s"] == 1500.0 and crumb["data"]["idle_s"] == 300.0


# ACL-1427: RECENT_INPUT больше не стоп сразу — робот ждёт тишины ввода
# (tests/test_a_hand_opened_window_waits_for_idle.py).
@pytest.mark.parametrize("reason", [hw.TOO_YOUNG, hw.IN_FOREGROUND,
                                    hw.FOREGROUND_UNKNOWN, hw.IDLE_UNKNOWN, hw.NO_PROCESS])
def test_an_unsafe_window_still_stops_with_the_current_message(world, reason):
    world.verdict = hw.Verdict(False, reason, age_s=120.0)
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert MESSAGE in str(stop.value)
    assert stop.value.detail == "no_automation_port"
    assert world.ev == [], "окно, которое небезопасно закрывать, тронули"
    assert world.notices == []
    step, ok, kw = world.rows[-1]
    assert (step, ok, kw["note"]) == ("profile_hand_window", False, reason)


def test_a_failed_mlx_stop_kills_nothing_and_stops_as_before(world):
    world.stop_fails = True
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert world.ev == [("stop", PROFILE)], "после отказа API убили процесс или подняли профиль"
    assert world.rows[-1][2]["note"] == "mlx_stop_failed"


def test_a_window_that_outlives_the_stop_is_not_reopened(world):
    world.keeps_running = True
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert "start" not in world.ev and "kill" not in world.ev
    assert world.rows[-1][2]["note"] == "still_running"


def test_a_failed_start_after_the_close_is_a_restart_failure(world):
    world.start_error = RuntimeError("BrowserType.connect_over_cdp: connect ECONNREFUSED")
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.RESTART_FAILED
    assert "kill" not in world.ev
    assert world.rows[-1][2]["note"] == "restart_failed"
    assert world.notices == []


def test_an_unowned_silent_port_goes_through_the_same_gate(world, monkeypatch):
    def attach(*a, **kw):
        raise br.ProfileUnresponsive("silent")

    monkeypatch.setattr(world.prov._direct, "_try_attach_existing", attach)
    monkeypatch.setattr(world.prov._direct, "_discover_owned_debug_port", lambda udd: None)
    world.verdict = hw.Verdict(False, hw.TOO_YOUNG, age_s=30.0)
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world)
    assert stop.value.code == pt.OPEN_BY_HAND and stop.value.detail == "unowned_port"
    assert world.ev == []
    world.verdict = hw.Verdict(True, hw.SAFE, age_s=1500.0, idle_s=300.0)
    assert _open(world).page == "reopened-page"
    assert world.ev == [("stop", PROFILE), "start", "connect"]


def test_an_mlx_lock_without_a_visible_process_goes_through_the_gate(world, monkeypatch):
    """Процесса не видно, MLX говорит «browser process is running»."""
    from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError

    monkeypatch.setattr(world.prov._direct, "_profile_process_running", lambda *a: False)
    first = {"done": False}
    real_start = _Launcher.start_profile

    def start(self, folder_id, profile_id, *, headless=False):
        if not first["done"]:
            first["done"] = True
            self.world.ev.append("start-locked")
            raise MultiloginLauncherError("Start profile failed HTTP 400: browser process is running")
        return real_start(self, folder_id, profile_id, headless=headless)

    monkeypatch.setattr(_Launcher, "start_profile", start)
    conn = _open(world)
    assert conn.page == "reopened-page"
    assert world.ev == ["start-locked", ("stop", PROFILE), "start", "connect"]
    assert world.rows[-1][2]["diag"]["where"] == "mlx_lock"


def test_reading_spend_never_closes_a_hand_opened_window(world):
    """Чтение спенда (`allow_kill=False`) — окно человека не закрывает."""
    with pytest.raises(pt.ProfileStop) as stop:
        _open(world, allow_restart=True, allow_kill=False)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert world.ev == [] and world.seen_pids == []


def test_a_profile_the_robot_started_is_restarted_as_before_without_the_gate(world):
    br._mark_robot_profile(PROFILE)
    world.pids = frozenset()   # свой процесс робота не прочитан — решает членство
    world.prov._direct._profile_process_running = lambda *a: True
    _open(world)
    assert world.seen_pids == [], "профиль робота прогнали через проверку окна руками"


# ── После подъёма: разлогинен — profile_logged_out, без повторов ───────────

def test_a_reopened_profile_that_is_logged_out_stops_with_profile_logged_out(world, monkeypatch):
    from src.fb_ads_rpa import ads_manager_flow as amf
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher
    import src.fb_ads_rpa.rpa_publisher as pub_mod

    monkeypatch.setenv("FB_RPA_ENGINE", "recorded_flow")
    monkeypatch.setattr(br, "put_window_back", lambda page: None)
    monkeypatch.setattr(amf.time, "sleep", lambda s: None)
    monkeypatch.setattr(pub_mod, "get_mlx_attach_only", lambda: True)
    gotos: list = []

    def goto(self, draft, result):
        gotos.append(self.page)
        raise pt.ProfileStop(pt.LOGGED_OUT, "business.facebook.com/business/loginpage/")

    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", goto)
    monkeypatch.setattr(amf, "run_recorded_campaign_flow", lambda page, **kw: "")

    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._entry = None
    pub._built_at = None
    pub._provider = world.prov
    monkeypatch.setattr(pub, "_resolve_entry", lambda label: _entry())

    drafts = [CampaignDraft(n, "MX", "Facebook_6540",
                            ad_sets=[AdSetDraft("Set", ads=[AdDraft(ad_title="t", ad_body="b")])])
              for n in ("A", "B")]
    results = [pub.publish_draft(d) for d in drafts]
    assert gotos == ["reopened-page"], "разлогиненный профиль открывали повторно"
    assert world.ev.count(("stop", PROFILE)) == 1 and world.ev.count("start") == 1
    for got in results:
        assert got.success is False
        assert pt.LOGGED_OUT in got.blocking_failures


def test_error_text_is_scrubbed_before_it_is_cut(world):
    """Ревью PR #396 (F1): uuid профиля у места обрезки не оставляет своё начало."""
    from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError

    world.stop_fails = True
    # uuid (с 101-го символа) пересекает обрезку на 120: без вычистки начало uuid осталось бы в тексте.
    padding = "x" * 80
    exc = MultiloginLauncherError(f"Stop profile failed {padding} {PROFILE} busy")
    text = br._exc_text(exc)
    assert PROFILE[:8] not in text and len(text) <= len("MultiloginLauncherError: ") + 120
    with pytest.raises(pt.ProfileStop):
        _open(world)
    assert PROFILE[:8] not in repr(world.rows) + repr(world.crumbs)
