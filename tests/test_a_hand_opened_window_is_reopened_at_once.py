# -*- coding: utf-8 -*-
"""Окно, открытое руками, переоткрывается сразу — без условий (ACL-1562).

04.10 кнопка «open profile» у Facebook_9943 ответила «The profile is open by
hand — close its window in Multilogin» сразу после того, как владелец
развернул окно: условия ACL-1388/1427 (окну не меньше 15 минут, не на
переднем плане, минута без ввода, ожидание тишины до 2 минут) не прошли.

Решение владельца 04.10: условия и ожидание убраны. Окно, открытое руками,
закрывается и профиль поднимается с автоматизацией сразу — на Publish, Read
spend, Read all и «open profile». Неизменно: закрытие — только мягкая
остановка ЭТОГО профиля через MLX API, ничего не убивается, другие профили
не трогаются, простой браузер не открывается. Строка в панели задания и на
строке аккаунта; телеметрия — что переоткрыли и сколько шла мягкая
остановка; отказ остановки — понятная причина и никакого kill.

Машина здесь «самая неудобная»: окну 10 секунд, оно на переднем плане, ввод
был секунду назад. Win32, процессы, часы и MLX подменены; настоящий
Multilogin не трогается.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import country_split as cs  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import hand_window as hw  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

ACCOUNT = "Facebook_9943"
OTHER = "Facebook_6540"
PROFILE = "550e8400-e29b-41d4-a716-446655440000"
OTHER_PROFILE = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
HAND_PID = 4242
OTHER_PID = 4343
NOTICE = "The Facebook_9943 window was opened by hand \u2014 reopening it with automation"


@pytest.fixture
def machine(monkeypatch):
    """Свежее окно на переднем плане, ввод только что. Прежние условия здесь
    провалились бы все; слоёв Win32 после ACL-1562 может и не быть."""
    monkeypatch.setenv(hw.ENV_ENABLED, "1")
    m = SimpleNamespace(sleeps=[], win32_reads=[])
    for name, value in (
        ("_is_windows", lambda: True),
        ("_process_create_times", lambda pids: m.win32_reads.append("age") or [hw.time.time() - 10]),
        ("_foreground_pid", lambda: m.win32_reads.append("fg") or HAND_PID),
        ("_ancestors", lambda pid: []),
        ("_idle_seconds", lambda: m.win32_reads.append("idle") or 1.0),
        ("_sleep", lambda s: m.sleeps.append(s)),
    ):
        monkeypatch.setattr(hw, name, value, raising=False)
    return m


class _Launcher:
    """MLX API: остановка и запуск по id профиля; убить тут нечем."""

    def __init__(self, world):
        self.world = world

    def start_profile(self, folder_id, profile_id, *, headless=False):
        from src.meta_campaign_agent.multilogin_launcher import StartedProfile

        self.world.ev.append(("start", profile_id))
        return StartedProfile(profile_id=profile_id, folder_id=folder_id, port="58906")

    def stop_profile(self, profile_id, folder_id=None):
        from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError

        self.world.ev.append(("stop", profile_id))
        # Что видела строка аккаунта в момент остановки.
        self.world.row_notes.append(sp.hand_wait_note(self.world.account_of[profile_id]))
        if self.world.stop_fails:
            raise MultiloginLauncherError("Stop profile failed HTTP 500")
        self.world.running[profile_id] = frozenset()


@pytest.fixture
def world(machine, monkeypatch):
    """Два профиля открыты руками (порта автоматизации нет ни у одного)."""
    import src.meta_campaign_agent.multilogin_config as cfg_mod
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod
    from src.fb_ads_rpa import run_telemetry as rt

    w = SimpleNamespace(closes=[], br_sleeps=[], ev=[], rows=[], crumbs=[], notices=[], row_notes=[], stop_fails=False,
                        m=machine, account_of={PROFILE: ACCOUNT, OTHER_PROFILE: OTHER},
                        running={PROFILE: frozenset({HAND_PID}), OTHER_PROFILE: frozenset({OTHER_PID})})
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    monkeypatch.setattr(br.time, "sleep", lambda s: w.br_sleeps.append(s))
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: f"C:/mlx/{pid}")
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: w.running.get(pid, frozenset()))
    monkeypatch.setattr(br, "_kill_chrome_for_user_data_dir", lambda *a, **k: w.ev.append("kill"))
    monkeypatch.setattr(cfg_mod, "get_multilogin_instance", lambda inst: SimpleNamespace(instance_id=inst))
    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", lambda cfg: _Launcher(w))
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: w.rows.append((step, ok, kw)))
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "add_breadcrumb", lambda **kw: w.crumbs.append(kw))
    # Ревью PR #563 (F1): тест не читает процессы раннера. Любой обход
    # psutil — промах подмены (на CI он ещё и спал через `br.time.sleep`).
    import psutil
    w.process_reads = []
    monkeypatch.setattr(psutil, "process_iter",
                        lambda *a, **kw: w.process_reads.append(a) or iter(()))

    prov = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(prov._direct, "_try_attach_existing", lambda *a, **kw: None)
    monkeypatch.setattr(prov._direct, "_discover_owned_debug_port", lambda udd: None)
    monkeypatch.setattr(prov._direct, "_profile_process_running",
                        lambda udd, pid: bool(w.running.get(pid)))
    monkeypatch.setattr(prov, "_connect_started",
                        lambda started, pid: w.ev.append(("connect", pid)) or SimpleNamespace(
                            page="reopened-page", close=lambda **kw: w.closes.append(kw)))
    # Ни остановка, ни подъём не просят права убить.
    w.kill_flags = []
    real_stop, real_start = prov._stop_profile, prov._start_via_mlx_api

    def stop(*a, **kw):
        w.kill_flags.append(("stop", kw.get("allow_kill", True)))
        return real_stop(*a, **kw)

    def start(*a, **kw):
        w.kill_flags.append(("start", kw.get("allow_kill", True)))
        return real_start(*a, **kw)

    monkeypatch.setattr(prov, "_stop_profile", stop)
    monkeypatch.setattr(prov, "_start_via_mlx_api", start)
    w.prov = prov
    return w


def _entry(pid=PROFILE):
    return SimpleNamespace(profile_id=pid, folder_id="f-1", multilogin_instance="A",
                           ad_account_id="act_123")


def _reopened_only(world, pid=PROFILE):
    assert world.ev == [("stop", pid), ("start", pid), ("connect", pid)], world.ev
    assert "kill" not in world.ev
    assert world.kill_flags == [("stop", False), ("start", False)], world.kill_flags
    assert world.running[OTHER_PROFILE if pid == PROFILE else PROFILE], "тронули другой профиль"


def _no_wait(world):
    assert world.m.sleeps == [], f"ждали: {world.m.sleeps}"
    # Пауза после мягкой остановки (3 с, `_stop_profile`) — не ожидание.
    stops = sum(1 for e in world.ev if isinstance(e, tuple) and e[0] == "stop")
    assert world.br_sleeps == [3] * stops, world.br_sleeps
    assert world.m.win32_reads == [], "читали окно/ввод машины"
    assert world.process_reads == [], "читали процессы машины"


def _hand_row(world):
    return [r for r in world.rows if r[0] == "profile_hand_window"][-1]


# ── Publish ────────────────────────────────────────────────────────────────

def test_publish_reopens_a_fresh_foreground_typed_in_window_at_once(world):
    from src.fb_ads_rpa.creative_compress import observe_preparation

    with observe_preparation(world.notices.append):
        conn = world.prov.open(_entry(), ACCOUNT)
    assert conn.page == "reopened-page"
    _reopened_only(world)
    _no_wait(world)
    assert world.notices == [NOTICE]


def test_publish_through_the_publisher_reopens_at_once(world, monkeypatch):
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher
    import src.fb_ads_rpa.rpa_publisher as pub_mod
    from src.fb_ads_rpa.creative_compress import observe_preparation

    monkeypatch.setattr(pub_mod, "get_mlx_attach_only", lambda: True)
    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._entry = None
    pub.browser_provider_name = "multilogin_attach"
    pub._cancelled = False
    pub._provider = world.prov
    monkeypatch.setattr(pub, "_resolve_entry", lambda label: _entry())
    with sp.profile_lock(PROFILE), observe_preparation(world.notices.append):
        pub._ensure_browser(ACCOUNT)
    _reopened_only(world)
    _no_wait(world)
    assert world.notices == [NOTICE]


def test_the_reopen_is_recorded_with_the_graceful_stop_duration(world, monkeypatch):
    clock = iter([100.0, 104.5])
    monkeypatch.setattr(br, "_stop_clock", lambda: next(clock), raising=False)
    world.prov.open(_entry(), ACCOUNT)
    step, ok, kw = _hand_row(world)
    assert (step, ok, kw["note"]) == ("profile_hand_window", True, "reopened")
    assert kw["diag"]["where"] == "no_automation_port"
    assert kw["diag"]["stop_s"] == 4.5
    assert "age_s" not in kw["diag"] and "idle_s" not in kw["diag"]
    assert "waited_s" not in kw["diag"]
    assert PROFILE[:8] not in repr(world.rows) + repr(world.crumbs)
    assert world.crumbs[-1]["message"] == "hand_window:reopened"


# ── Отказ мягкой остановки: понятная причина, никакого kill ───────────────

def test_a_failed_graceful_stop_gives_a_clear_reason_and_kills_nothing(world):
    world.stop_fails = True
    with pytest.raises(pt.ProfileStop) as stop:
        world.prov.open(_entry(), ACCOUNT)
    assert stop.value.code == pt.OPEN_BY_HAND
    assert "Multilogin did not close" in stop.value.detail
    assert "HTTP 500" in stop.value.detail
    assert "Multilogin did not close" in str(stop.value)
    assert world.ev == [("stop", PROFILE)], "после отказа API убили процесс или подняли профиль"
    assert world.kill_flags == [("stop", False)]
    assert world.running[PROFILE] and world.running[OTHER_PROFILE]
    step, ok, kw = _hand_row(world)
    assert (ok, kw["note"]) == (False, "mlx_stop_failed")
    assert "stop_s" in kw["diag"] and "HTTP 500" in kw["diag"]["error"]


# ── Read spend / Read all / open profile ───────────────────────────────────

class _Direct:
    """Слой «прицепиться к открытому»: порта нет, процесс профиля есть."""

    def __init__(self, world):
        self.world = world

    def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
        return None

    def profile_is_running(self, user_data_dir, profile_id=""):
        return bool(self.world.running.get(profile_id))


@pytest.fixture
def reads(world, monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(sp, "_dir_spellings", lambda p: [str(p)])
    monkeypatch.setattr(br, "MultiloginDirectCDPProvider", lambda: _Direct(world))
    monkeypatch.setattr(br, "get_browser_provider", lambda name=None: world.prov)
    entries = {ACCOUNT: _entry(PROFILE), OTHER: _entry(OTHER_PROFILE)}
    monkeypatch.setattr(sp, "_entry_for", entries.get)
    monkeypatch.setattr(cs, "_entry_for", entries.get)
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    return world


def _post(path, data):
    import httpx

    from src.web.app import app

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.post(path, data=data)

    return asyncio.run(go())


def test_the_wait_and_the_conditions_are_gone():
    for name in ("wait_until_safe", "watch_cancel", "MIN_AGE_S", "MIN_IDLE_S", "IDLE_WAIT_S"):
        assert not hasattr(hw, name), name
    assert not hasattr(br, "HAND_WINDOW_WAIT_NOTICE")
    page = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "once the computer has been idle" not in page
    assert "idle" not in sp.HAND_OPEN_MESSAGE


# ── Stop задания — не сбой кампании (перенесено из ACL-1427) ──────────────
#
# Ожидания тишины ввода больше нет, а правило осталось общим: остановка
# профиля, когда задание остановил человек, пишется в телеметрию, но не
# уходит в Sentry (ревью PR #428, F3; ACL-1318).

def _sentry_and_rows(monkeypatch):
    import src.fb_ads_rpa.rpa_publisher as pub_mod
    from src.fb_ads_rpa import sentry_signal

    sent, rows = [], []
    monkeypatch.setattr(sentry_signal, "report_campaign_stop", lambda code, **kw: sent.append(code) or "")
    monkeypatch.setattr(pub_mod._telemetry, "record", lambda *a, **kw: rows.append((a, kw)))
    return sent, rows


@pytest.mark.parametrize("cancelled, to_sentry", [(False, True), (True, False)])
def test_a_stop_pressed_by_the_person_is_not_a_sentry_failure(monkeypatch, cancelled, to_sentry):
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    sent, rows = _sentry_and_rows(monkeypatch)
    stop = pt.ProfileStop(pt.OPEN_BY_HAND, "attach")
    RpaPublisher._report_profile_stop(stop, "C1", ACCOUNT, "attach", cancelled=cancelled)
    assert [r[0][0] for r in rows] == [pt.OPEN_BY_HAND]
    assert sent == ([pt.OPEN_BY_HAND] if to_sentry else [])


def test_publish_draft_passes_the_job_stop_to_the_report(monkeypatch):
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    sent, rows = _sentry_and_rows(monkeypatch)
    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub._conn = None
    pub._cancelled = True

    def ensure(label):
        raise pt.ProfileStop(pt.OPEN_BY_HAND, "attach")

    monkeypatch.setattr(pub, "_ensure_browser", ensure)
    draft = SimpleNamespace(status="DRAFT", errors=[], name="C1", account_label=ACCOUNT)
    result = pub.publish_draft(draft)
    assert result.success is False
    assert sent == []


def test_stop_pressed_during_the_reopen_does_not_start_the_campaign(world, monkeypatch):
    """Ревью PR #563 (F2): ожидания больше нет, но само переоткрытие идёт
    секунды. Stop, нажатый в это время, не должен пустить первую кампанию."""
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher
    import src.fb_ads_rpa.rpa_publisher as pub_mod

    monkeypatch.setattr(pub_mod, "get_mlx_attach_only", lambda: True)
    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._entry = None
    pub.browser_provider_name = "multilogin_attach"
    pub._built_at = None
    pub._cancelled = False
    pub._provider = world.prov
    monkeypatch.setattr(pub, "_resolve_entry", lambda label: _entry())
    real_stop = world.prov._stop_profile

    def stop_then_press_stop(*a, **kw):
        real_stop(*a, **kw)
        pub._cancelled = True   # человек нажал Stop, пока окно переоткрывалось

    monkeypatch.setattr(world.prov, "_stop_profile", stop_then_press_stop)
    flows = []
    monkeypatch.setattr(pub, "_flow", lambda conn, label: flows.append(label) or SimpleNamespace(
        publish_campaign=lambda draft, dry_run=False: pytest.fail("кампания пошла после Stop")))
    draft = SimpleNamespace(status="DRAFT", errors=[], name="C1", account_label=ACCOUNT)
    result = pub.publish_draft(draft)
    assert result.success is False
    assert "Stopped by the user" in (result.error or "")
    assert flows == []
    assert "kill" not in world.ev
    # Ревью PR #563 (раунд 3): только отключение от CDP — окно человека
    # остаётся открытым, и второй остановки профиля нет.
    # ACL-1606: Stop слышен и перед запуском — после него профиль не поднимают,
    # подключения нет, закрывать нечего.
    assert world.closes == [], world.closes
    assert world.ev == [("stop", PROFILE)], world.ev


# ── Ревью PR #563 (F3): текст отказа говорит правду о причине ─────────────

def test_the_publish_failure_texts_name_both_causes():
    """Publish кончается «открыт руками» и при отказе мягкой остановки, и при
    выключенном переоткрытии — текст не должен валить всё на Multilogin."""
    src = (ROOT / "src" / "fb_ads_rpa" / "failure_policy.py").read_text(encoding="utf-8")
    assert "Multilogin did not close it when the robot asked" not in src
    assert "Multilogin did not close the hand-opened window, so" not in src
    assert "switched off on this computer" in src


@pytest.fixture(autouse=True)
def publisher_profile_mapping(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp
    monkeypatch.setattr(rp, "load_ad_account_map", lambda *_: {})
    monkeypatch.setattr(rp, "resolve_account_entry", lambda *_: _entry())
