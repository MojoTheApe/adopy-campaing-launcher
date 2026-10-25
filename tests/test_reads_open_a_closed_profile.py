# -*- coding: utf-8 -*-
"""Чтение само открывает закрытый профиль и закрывает то, что открыло (ACL-1580).

До ACL-1580 «Read spend», «Read all» и чтение стран на закрытом профиле
отказывали: «the profile is not open. Open it through automation and leave the
window open». Причина отказа была в перезапуске ОТКРЫТОГО окна (разлогинил
x01); запуск ЗАКРЫТОГО — обычное открытие, ровно то, что делает кнопка
«open profile». Решение владельца 05.10: чтения открывают нужное сами.

Здесь:

* закрытый профиль запускается тем же безопасным путём, что «open profile»
  (только старт, без перезапуска, без простого браузера), затем читается;
* открытый — к нему цепляемся, не перезапускаем и оставляем открытым;
* профиль, запущенный чтением, после чтения мягко закрывается через MLX API
  (`allow_kill=False`), никакого kill, другие профили не трогаются — и при
  упавшем чтении тоже;
* разлогиненный профиль — понятная строка и никаких повторов;
* Read all с N закрытыми профилями идёт через очередь мест Publish
  (ACL-1424): разом не больше разрешённого, строка показывает ожидание.

Процессы и MLX подменены (фикстуры ACL-1562); настоящий Multilogin не
трогается.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.profile_lease_fakes import fake_profile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import country_split as cs  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.web import profile_slots as ps  # noqa: E402

from tests.test_a_hand_opened_window_is_reopened_at_once import (  # noqa: E402,F401
    ACCOUNT,
    OTHER,
    OTHER_PROFILE,
    PROFILE,
    machine,
    world,
)

PAGE = ROOT / "src" / "web" / "templates" / "report.html"
DAY = "2026-10-01"
LOGIN = "https://www.facebook.com/login/?next=https%3A%2F%2Fadsmanager.facebook.com%2F"


class _Page:
    """Вкладка чтения: переходы пишутся в мир, адрес — как скажет мир."""

    def __init__(self, world, pid, *, fail=None, url=""):
        self.world, self.pid, self.fail, self._url = world, pid, fail, url

    @property
    def url(self):
        return self._url

    def goto(self, url, **kw):
        self.world.ev.append(("goto", self.pid))
        if self.fail:
            raise self.fail
        if not self._url:
            self._url = url

    def close(self):
        self.world.ev.append(("tab_closed", self.pid))


class _Direct:
    """«Прицепиться к открытому»: порт есть только у профилей робота."""

    def __init__(self, world):
        self.world = world

    def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
        if self.world.automation.get(profile_id):
            self.world.ev.append(("attach", profile_id))
            return SimpleNamespace(page=_Page(self.world, profile_id, **self.world.page_kw),
                                   close=lambda **kw: self.world.closes.append(kw))
        return None

    def profile_is_running(self, user_data_dir, profile_id=""):
        return bool(self.world.running.get(profile_id))


def _quiet_queue(limit=1):
    return ps.ProfileSlots(
        memory=lambda: ps.MemoryReading(total_mb=16 * 1024, available_mb=12 * 1024),
        settings=lambda: ps.SlotSettings(override=limit),
        tick_s=0.01,
        usage=ps.ProfileUsage(measure=lambda profiles: {}, background=False),
    )


@pytest.fixture
def reads(world, monkeypatch, tmp_path):
    """Оба профиля закрыты; чтение видит мир ACL-1562 через настоящий провайдер."""
    world.running = {}
    world.automation = {}
    world.page_kw = {}
    world.queue = _quiet_queue()
    monkeypatch.setattr(sp, "_slot_queue", lambda: world.queue)
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 0.0)
    monkeypatch.setattr(sp, "_PENDING_CLOSE", {})
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(sp, "_dir_spellings", lambda p: [str(p)])
    monkeypatch.setattr(br, "MultiloginDirectCDPProvider", lambda: _Direct(world))
    monkeypatch.setattr(br, "get_browser_provider", lambda name=None: world.prov)

    def started(started, pid):
        # MLX поднял профиль с автоматизацией: у него теперь есть порт робота.
        world.ev.append(("connect", pid))
        world.running[pid] = frozenset({1})
        world.automation[pid] = True
        return SimpleNamespace(page=_Page(world, pid, **world.page_kw),
                               close=lambda **kw: world.closes.append(kw))

    monkeypatch.setattr(world.prov, "_connect_started", started)

    # Остановка через MLX API закрывает профиль и снимает его порт.
    import src.meta_campaign_agent.multilogin_launcher as launcher_mod
    real_launcher = launcher_mod.MultiloginLauncher

    def launcher(cfg):
        inner = real_launcher(cfg)
        real = inner.stop_profile

        def stop_profile(profile_id, folder_id=None):
            real(profile_id, folder_id=folder_id)
            world.automation.pop(profile_id, None)

        inner.stop_profile = stop_profile
        return inner

    monkeypatch.setattr(launcher_mod, "MultiloginLauncher", launcher)
    # Остановка «по просьбе» тоже идёт через подменённый MLX, а не настоящий.
    entries = {
        name: SimpleNamespace(profile_id=pid, folder_id="f-1", multilogin_instance="A",
                              ad_account_id="act_123")
        for name, pid in ((ACCOUNT, PROFILE), (OTHER, OTHER_PROFILE))
    }
    world.entries = entries
    monkeypatch.setattr(sp, "_entry_for", entries.get)
    monkeypatch.setattr(cs, "_entry_for", entries.get)
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    return world


@pytest.fixture
def page_reads(monkeypatch):
    """Ads Manager на вкладке отвечает как надо — чтение доходит до суммы."""
    for name, value in (
        ("_wait_for_grid", lambda page: True),
        ("_act_from_url", lambda page, tries=20: "act_123"),
        ("_read_status", lambda page, tries=0: ("clear", "")),
        ("_shoot_status", lambda page, out: None),
        ("_shown_period", lambda page, tries=0: "Oct 1, 2026"),
        ("_period_matches", lambda shown, a, b="": True),
        ("_read_total", lambda page: (12.5, "fake")),
        ("_currency", lambda page: "USD"),
        ("_read_tree", lambda page, act, expected, a, b, reached=None: ([], "fake", "")),
        ("_shoot", lambda page, account, day: ""),
    ):
        monkeypatch.setattr(sp, name, value)


def _steps(world, name):
    return [r for r in world.rows if r[0] == name]


# ── закрытый профиль → запущен, прочитан, закрыт ──────────────────────────

def test_a_closed_profile_is_started_then_read(reads, page_reads):
    out = sp.read_via_rpa(ACCOUNT, DAY, DAY)
    assert out.state == "ok", out.error
    assert out.spend == 12.5
    ev = [e for e in reads.ev if e[0] in {"start", "connect", "goto", "stop"}]
    assert ev == [("start", PROFILE), ("connect", PROFILE), ("goto", PROFILE), ("stop", PROFILE)]
    # Тот же безопасный путь, что у «open profile»: только старт — ни
    # остановки перед ним, ни права убить.
    assert reads.kill_flags[0] == ("start", False)
    assert "kill" not in reads.ev
    started = _steps(reads, "spend_profile_started")
    assert len(started) == 1 and started[0][1] is True
    assert isinstance(started[0][2].get("duration_s"), float)
    # Ревью PR #583 (F3): вкладка поднятого чтением окна не закрывается —
    # она может быть в окне единственной; окно закрывает мягкая остановка.
    assert ("tab_closed", PROFILE) not in reads.ev


def test_a_profile_the_read_started_is_closed_gracefully_and_only_it(reads, page_reads):
    reads.running[OTHER_PROFILE] = frozenset({4343})          # второй открыт руками
    sp.read_via_rpa(ACCOUNT, DAY, DAY)
    stops = [e for e in reads.ev if isinstance(e, tuple) and e[0] == "stop"]
    assert stops == [("stop", PROFILE)], "закрыли не тот профиль или не один"
    assert "kill" not in reads.ev
    assert reads.running.get(OTHER_PROFILE), "чужой профиль тронут"
    closed = _steps(reads, "spend_profile_closed_after_read")
    assert len(closed) == 1 and closed[0][1] is True
    assert isinstance(closed[0][2].get("duration_s"), float)
    # Место в очереди профилей освобождено.
    snap = reads.queue.snapshot()
    assert snap["running"] == [] and snap["waiting"] == []


def test_the_graceful_stop_never_kills_even_when_mlx_refuses(reads, page_reads):
    reads.stop_fails = True
    out = sp.read_via_rpa(ACCOUNT, DAY, DAY)
    assert out.state == "ok"                    # прочитанное не теряется
    assert "kill" not in reads.ev
    closed = _steps(reads, "spend_profile_closed_after_read")
    assert len(closed) == 1 and closed[0][1] is False
    assert reads.queue.snapshot()["running"] == []


# ── уже открытый → прицепились, не перезапустили, оставили ────────────────

def test_an_open_profile_is_attached_not_restarted_and_left_open(reads, page_reads):
    reads.running[PROFILE] = frozenset({1})
    reads.automation[PROFILE] = True
    out = sp.read_via_rpa(ACCOUNT, DAY, DAY)
    assert out.state == "ok"
    names = [e[0] for e in reads.ev if isinstance(e, tuple)]
    assert "start" not in names and "stop" not in names
    assert ("attach", PROFILE) in reads.ev
    assert reads.automation.get(PROFILE), "открытый профиль закрыт"
    assert not _steps(reads, "spend_profile_started")
    # Своя вкладка чтения в чужом окне закрывается — окно остаётся.
    assert ("tab_closed", PROFILE) in reads.ev
    assert not _steps(reads, "spend_profile_closed_after_read")
    assert reads.queue.snapshot()["running"] == [], "открытому профилю место не нужно"


def test_a_failed_read_still_closes_the_profile_it_started(reads):
    reads.page_kw = {"fail": RuntimeError("navigation exploded")}
    with pytest.raises(RuntimeError, match="navigation exploded"):
        sp.read_via_rpa(ACCOUNT, DAY, DAY)
    ev = [e for e in reads.ev if isinstance(e, tuple) and e[0] in {"start", "stop"}]
    assert ev == [("start", PROFILE), ("stop", PROFILE)]
    assert "kill" not in reads.ev
    assert reads.queue.snapshot()["running"] == []


def test_the_country_read_starts_and_closes_too(reads):
    reads.page_kw = {"fail": RuntimeError("report exploded")}
    out = cs.read_country_split(ACCOUNT, DAY)
    assert out.state == "failed"
    ev = [e for e in reads.ev if isinstance(e, tuple) and e[0] in {"start", "goto", "stop"}]
    assert ev == [("start", PROFILE), ("goto", PROFILE), ("stop", PROFILE)]
    assert len(_steps(reads, "spend_profile_started")) == 1
    assert len(_steps(reads, "spend_profile_closed_after_read")) == 1


def test_a_failed_start_releases_the_slot_and_says_why(reads, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("MLX said no")

    monkeypatch.setattr(reads.prov, "_start_via_mlx_api", boom)
    out = sp.read_via_rpa(ACCOUNT, DAY, DAY)
    assert out.state == "failed" and "could not be started" in out.error
    assert "stop" not in [e[0] for e in reads.ev if isinstance(e, tuple)]
    assert reads.queue.snapshot()["running"] == []
    started = _steps(reads, "spend_profile_started")
    assert len(started) == 1 and started[0][1] is False


# ── разлогинен → понятная строка, без повторов ────────────────────────────

def test_a_logged_out_profile_gets_a_plain_line_and_no_retry(reads):
    reads.page_kw = {"url": LOGIN}
    out = sp.read_via_rpa(ACCOUNT, DAY, DAY)
    assert out.state == "failed"
    assert out.error == (f"The {ACCOUNT} profile is logged out of Facebook — "
                         "log in inside Multilogin, then Read again")
    assert out.error_code == sp.LOGGED_OUT_CODE == "profile_logged_out"
    names = [e[0] for e in reads.ev if isinstance(e, tuple)]
    assert names.count("start") == 1 and names.count("goto") == 1, "чтение повторилось"
    assert names.count("stop") == 1, "запущенный чтением профиль не закрыт"


def test_a_logged_out_open_profile_says_so_too(reads):
    reads.running[PROFILE] = frozenset({1})
    reads.automation[PROFILE] = True
    reads.page_kw = {"url": LOGIN}
    out = sp.read_via_rpa(ACCOUNT, DAY, DAY)
    assert out.error_code == sp.LOGGED_OUT_CODE
    assert "stop" not in [e[0] for e in reads.ev if isinstance(e, tuple)]


def test_the_country_read_names_a_logged_out_profile(reads):
    reads.page_kw = {"url": LOGIN}
    out = cs.read_country_split(ACCOUNT, DAY)
    assert out.state == "failed" and out.error_code == sp.LOGGED_OUT_CODE
    assert "logged out of Facebook" in out.error
    assert [e[0] for e in reads.ev if isinstance(e, tuple)].count("goto") == 1


def test_the_page_shows_the_logged_out_line_without_retry_or_open_button():
    js = PAGE.read_text(encoding="utf-8")
    start = js.index("function amapSpendFailed")
    body = js[start:js.index("function amapOpenProfile", start)]
    assert "var out = code === 'profile_logged_out';" in body
    # Простой текст вместо «failed · retry» и без кнопки «open profile»:
    # открывать нечего, войти может только человек.
    assert "hand || out ? 'Read spend' : 'failed · retry'" in body
    assert "if (!out && /профил|profile/i.test(why || '')" in body


# ── спенд, потом страны — один заход ─────────────────────────────────────

def test_the_country_read_right_after_the_spend_takes_the_profile_over(reads, page_reads, monkeypatch):
    """Read → страны (ACL-1282): одно открытие, одно закрытие — после стран."""
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 30.0)
    assert sp.read_via_rpa(ACCOUNT, DAY, DAY).state == "ok"
    assert "stop" not in [e[0] for e in reads.ev if isinstance(e, tuple)], "закрыли до стран"
    assert reads.queue.snapshot()["running"], "место отпущено, пока профиль открыт"
    reads.page_kw = {"fail": RuntimeError("report exploded")}
    cs.read_country_split(ACCOUNT, DAY)
    names = [e[0] for e in reads.ev if isinstance(e, tuple)]
    assert names.count("start") == 1 and names.count("stop") == 1
    assert reads.queue.snapshot()["running"] == []


def test_a_spend_read_alone_closes_after_the_grace(reads, page_reads, monkeypatch):
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 0.05)
    assert sp.read_via_rpa(ACCOUNT, DAY, DAY).state == "ok"
    assert _wait_for(lambda: ("stop", PROFILE) in reads.ev)
    assert _wait_for(lambda: reads.queue.snapshot()["running"] == [])


# ── Read all: очередь мест Publish ───────────────────────────────────────

def _wait_for(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_read_all_with_closed_profiles_respects_the_slots(monkeypatch, tmp_path, page_reads):
    accounts = [f"Facebook_{n}" for n in (1001, 1002, 1003, 1004)]
    entries = {a: SimpleNamespace(profile_id=fake_profile(a), folder_id="f", multilogin_instance="A",
                                  ad_account_id="act_123") for a in accounts}
    state = SimpleNamespace(open=set(), peak=0, notes=[], starts=[], stops=[], lock=threading.Lock(),
                            queued=threading.Event())

    class Direct:
        def attach_existing_only(self, profile_id, udd, *, new_page=False):
            return None

        def profile_is_running(self, udd, profile_id=""):
            return False

    class Provider:
        def open(self, entry, label, **kw):
            assert kw.get("allow_restart") is False, "закрытый профиль только запускается"
            with state.lock:
                state.open.add(entry.profile_id)
                state.peak = max(state.peak, len(state.open))
                state.starts.append(entry.profile_id)
            # Профиль занят, пока кто-то из остальных не встал в очередь:
            # иначе на медленной машине чтения могли бы и не пересечься.
            state.queued.wait(5)
            return SimpleNamespace(page=SimpleNamespace(goto=lambda url, **k: None, close=lambda: None,
                                                        url=""),
                                   close=lambda **k: None)

    def stop(entry, label):
        with state.lock:
            state.open.discard(entry.profile_id)
            state.stops.append(entry.profile_id)
        return "closed"

    queue = _quiet_queue(limit=2)
    monkeypatch.setattr(sp, "_slot_queue", lambda: queue)
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 0.0)
    monkeypatch.setattr(sp, "_PENDING_CLOSE", {})
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(sp, "_dir_spellings", lambda p: [str(p)])
    monkeypatch.setattr(br, "MultiloginDirectCDPProvider", Direct)
    monkeypatch.setattr(br, "get_browser_provider", lambda name=None: Provider())
    monkeypatch.setattr(br, "stop_profile_on_request", stop)
    monkeypatch.setattr(sp, "_entry_for", entries.get)
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    monkeypatch.setattr(sp, "_UNSAVED", {})
    from src.fb_ads_rpa import run_telemetry as rt
    monkeypatch.setattr(rt, "record", lambda *a, **kw: None)
    # Каждая строка, которую чтение кладёт на карту (её отдаёт `/account-spend/status`).
    real_note = sp._set_wait_note

    def note(account, text):
        with state.lock:
            state.notes.append(text)
        if text.startswith(sp.SLOT_WAIT_NOTE):
            state.queued.set()
        real_note(account, text)

    monkeypatch.setattr(sp, "_set_wait_note", note)

    for a in accounts:
        sp.start_read(a, DAY, DAY)
    assert _wait_for(lambda: all(sp.get_reading(a, DAY, DAY).state != "running" for a in accounts))
    assert all(sp.get_reading(a, DAY, DAY).state == "ok" for a in accounts)
    assert state.peak <= 2, f"{state.peak} профилей разом при двух местах"
    assert sorted(state.starts) == sorted(state.stops) == sorted(e.profile_id for e in entries.values())
    assert any(n.startswith(sp.SLOT_WAIT_NOTE) for n in state.notes), "строка ожидания места не показана"
    assert all(sp.hand_wait_note(a) == "" for a in accounts)
    assert queue.snapshot()["running"] == [] and queue.snapshot()["waiting"] == []


# ── очередь мест: профиль, уже занятый Publish ────────────────────────────

def test_a_read_shares_the_place_of_a_publish_on_its_own_profile():
    """Publish держит место профиля P и ждёт его замок, который держит чтение:
    чтение не встаёт в очередь за ним (иначе оба ждали бы друг друга)."""
    q = _quiet_queue(limit=1)
    assert q.acquire("publish-1", "P") is not None
    grant = q.acquire("read-1", "P", share_profile=True)
    assert grant is not None and grant["shared"] is True
    assert [r["job_id"] for r in q.snapshot()["running"]] == ["publish-1"]
    q.release("read-1")                              # общий — отпускать нечего
    assert [r["job_id"] for r in q.snapshot()["running"]] == ["publish-1"]
    calls = []
    assert q.acquire("read-2", "Q", share_profile=True,
                     cancelled=lambda: calls.append(1) or len(calls) > 3) is None, \
        "чужой профиль место не делит"


# ── телеметрия в таблице процента сборки ──────────────────────────────────

def test_the_new_steps_have_a_build_model_rule():
    from scripts import acl_build_progress_model as m

    rules = m.read_model()["rules"]
    for step in ("spend_profile_started", "spend_profile_closed_after_read", "spend_profile_restarted"):
        assert m.stage_of(step, rules) is not None, step


# ── ревью PR #583: Publish на том же профиле, чужое окно, старый таймер ────

def _run(fn, *a):
    box = {}

    def go():
        try:
            box["out"] = fn(*a)
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    th = threading.Thread(target=go, daemon=True)
    th.start()
    return th, box


def _stops(world):
    return [e for e in world.ev if isinstance(e, tuple) and e[0] == "stop"]


def test_a_read_sharing_a_publish_slot_never_closes_the_profile(reads, page_reads, monkeypatch):
    """F1: Publish держит место профиля P и ждёт его замок, который держит
    чтение. Чтение не ждёт за ним (иначе оба ждали бы друг друга), поднимает
    P — и оставляет его Publish: ни сразу, ни по таймеру не закрывает."""
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 0.05)
    assert reads.queue.acquire("publish-1", PROFILE) is not None
    th, box = _run(sp.read_via_rpa, ACCOUNT, DAY, DAY)
    th.join(5)
    assert not th.is_alive(), "чтение встало в очередь за Publish своего же профиля"
    assert box["out"].state == "ok"
    assert ("start", PROFILE) in reads.ev
    assert ("tab_closed", PROFILE) not in reads.ev
    time.sleep(0.3)                                 # дольше паузы закрытия
    assert _stops(reads) == [], "чтение закрыло профиль, который ждёт Publish"
    reads.queue.release("publish-1")                # Publish кончился, окно оставил
    time.sleep(0.3)
    assert _stops(reads) == [], "таймер чтения закрыл окно, оставленное Publish"
    assert reads.automation.get(PROFILE)


def test_a_publish_waiting_for_the_profile_during_the_grace_gets_it_open(reads, page_reads, monkeypatch):
    """F1: пока удачное чтение держит запущенный им профиль, за его местом
    встал Publish того же профиля. Таймер профиль не закрывает — отдаёт."""
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 0.2)
    assert sp.read_via_rpa(ACCOUNT, DAY, DAY).state == "ok"
    grant = {}
    th = threading.Thread(target=lambda: grant.update(g=reads.queue.acquire("publish-2", PROFILE)),
                          daemon=True)
    th.start()
    th.join(5)
    assert not th.is_alive() and grant["g"] is not None, "Publish не получил место"
    assert _stops(reads) == [], "таймер закрыл профиль, которого ждал Publish"
    skipped = _steps(reads, "spend_profile_closed_after_read")
    assert len(skipped) == 1 and skipped[0][2]["diag"]["skipped"] == "another_job"
    reads.queue.release("publish-2")


def test_a_stale_timer_after_the_country_takeover_stops_nothing(reads, page_reads, monkeypatch):
    """F3: страны забрали профиль и закрыли его сами; старый таймер спенда,
    сработав позже, не останавливает профиль второй раз."""
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 0.15)
    fired = threading.Event()
    real = sp._close_if_still_pending

    def watched(opened):
        try:
            real(opened)
        finally:
            fired.set()

    monkeypatch.setattr(sp, "_close_if_still_pending", watched)
    assert sp.read_via_rpa(ACCOUNT, DAY, DAY).state == "ok"
    reads.page_kw = {"fail": RuntimeError("report exploded")}
    cs.read_country_split(ACCOUNT, DAY)
    assert _stops(reads) == [("stop", PROFILE)]
    assert fired.wait(5), "таймер спенда не сработал"
    assert _stops(reads) == [("stop", PROFILE)], "старый таймер остановил профиль второй раз"


def test_a_profile_opened_by_someone_else_during_the_slot_wait_is_not_ours(reads, page_reads):
    """F2: пока чтение ждало место, профиль открыли автоматизацией мимо замка
    (`/mlx-profile-open`). Чтение цепляется к нему, не запускает и не закрывает."""
    assert reads.queue.acquire("other", "X") is not None    # единственное место занято
    th, box = _run(sp.read_via_rpa, ACCOUNT, DAY, DAY)
    assert _wait_for(lambda: sp.hand_wait_note(ACCOUNT).startswith(sp.SLOT_WAIT_NOTE))
    reads.running[PROFILE] = frozenset({1})
    reads.automation[PROFILE] = True                         # кто-то открыл профиль
    reads.queue.release("other")
    th.join(5)
    assert not th.is_alive() and box["out"].state == "ok"
    names = [e[0] for e in reads.ev if isinstance(e, tuple)]
    assert "start" not in names and "stop" not in names
    assert reads.automation.get(PROFILE), "чужое окно закрыто"
    assert reads.queue.snapshot()["running"] == [], "место не отпущено"
    assert not _steps(reads, "spend_profile_started")


def test_a_publish_that_finished_before_the_timer_keeps_its_window(reads, page_reads, monkeypatch):
    """F1, сценарий ревью: Publish держал место P, чтение его разделило и
    подняло P; Publish кончился и оставил окно (ACL-1444: hand-off,
    FB_RPA_KEEP_PROFILES_OPEN) раньше, чем сработал бы таймер чтения.
    Профиль запустило чтение, но он — Publish: таймера нет вовсе."""
    monkeypatch.setattr(sp, "CLOSE_GRACE_S", 0.2)
    assert reads.queue.acquire("publish-1", PROFILE) is not None
    th, box = _run(sp.read_via_rpa, ACCOUNT, DAY, DAY)
    th.join(5)
    assert box["out"].state == "ok"
    reads.queue.release("publish-1")
    time.sleep(0.5)
    assert _stops(reads) == [], "таймер чтения закрыл окно, оставленное Publish"
    assert not sp._PENDING_CLOSE, "чтение считает профиль Publish своим"
