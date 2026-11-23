# -*- coding: utf-8 -*-
"""ACL-2113: окно профиля остаётся после чистой публикации; простаивающие уступают место.

Владелец: байер работает в окне дальше (проверяет кампании, включает), поэтому
робот его не закрывает. Но каждое оставленное окно держит место в очереди, и два
опубликованных аккаунта запирали всю очередь (22.10, TEST9095: B91 и D65 ждали
Doc122/9403A). Здесь: окна «для удобства» (чистая публикация) места не занимают
и при нехватке памяти уступают сразу (ACL-2668, решение владельца 16.11 — без
10 минут простоя); окна «на проверку» не закрываются никогда; открытое окно
берёт следующее задание того же аккаунта. Очередь, часы и остановка профиля —
подставные: ни Multilogin, ни Meta.
"""
from __future__ import annotations

import contextlib
import dataclasses
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import profile_slots as ps  # noqa: E402
from src.web import publish_api  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402

GB = 1024


def _key(label):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, label))


IDLE_S = 600.0
#: Свободно столько — меньше запаса (`keep_free_mb` ~10 ГБ из 128): новый профиль ждёт памяти.
SHORT_MB = 8 * GB
#: Закрытое окно возвращает столько.
FREED_MB = 4 * GB


def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class Rig:
    """Очередь с потолком 2, ручными часами и подставным закрытием окон."""

    def __init__(self, ceiling=2, evict_ok=True, idle_close_s=0.0):
        self.now = 1000.0
        self.avail_mb = 120 * GB
        self.closed: list[tuple[str, str, str]] = []
        self.evict_ok = evict_ok
        self.lock_free = True
        self.idle_seen: list[float] = []
        self.settings = dataclasses.replace(ps.load_settings({}), warmup_s=0, ceiling=ceiling,
                                            kept_idle_close_s=idle_close_s)
        self.probe = ps.ProfileUsage(lambda profiles: {p: int(3.5 * GB) for p in profiles},
                                     clock=lambda: self.now, background=False)
        self.slots = self.make_slots(self._evict)

    def make_slots(self, evictor):
        return ps.ProfileSlots(
            memory=lambda: ps.MemoryReading(total_mb=128 * GB, available_mb=self.avail_mb),
            settings=lambda: self.settings, clock=lambda: self.now, tick_s=0.02, usage=self.probe,
            # Подставная «открыт ли профиль»: настоящая сканирует процессы Mimic и может
            # отвечать секунды — очередь ждёт её до 5 с перед закрытием окна ради памяти.
            open_probe=ps.OpenProfileProbe(lambda profiles: set(), clock=lambda: self.now,
                                           background=False),
            evictor=evictor)

    def _evict(self, job_id, label, profile, idle_s=0.0):
        # ACL-2113 (ревью): очередь вызывает остановку БЕЗ своего замка — проверяем из чужого потока.
        seen: dict = {}
        t = threading.Thread(target=lambda: seen.setdefault("snap", self.slots.snapshot()), daemon=True)
        t.start()
        t.join(2)
        self.lock_free = self.lock_free and "snap" in seen
        self.idle_seen.append(idle_s)
        self.closed.append((job_id, label, profile))
        if self.evict_ok:
            self.avail_mb += FREED_MB
        return self.evict_ok

    def run_and_keep(self, job_id, profile, label, kind):
        grant = self.slots.acquire(job_id, profile)
        assert grant is not None
        self.slots.keep(job_id, label, kind)
        return grant

    def start(self, job_id, profile):
        box: dict = {"waits": []}

        def go():
            box["grant"] = self.slots.acquire(job_id, profile, on_wait=box["waits"].append)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        box["thread"] = t
        return box


def _running(slots):
    return sorted((r["profile"], r["kept_kind"]) for r in slots.snapshot()["running"])


# ── очередь: окна «для удобства» места не занимают и уступают сразу ───────

def _drop_waiter(rig, job_id):
    with rig.slots._cond:
        rig.slots._waiting.pop(job_id, None)
        rig.slots._cond.notify_all()


def test_kept_convenience_windows_do_not_take_a_slot_the_third_job_starts_at_once():
    # ACL-2668: потолок 2, два окна оставлены после чистых публикаций — третье задание
    # стартует сразу, ничего не закрывая: память есть, а окна места не держат.
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert rig.closed == [] and c["grant"]["closed_for_room"] == []
    assert c["waits"] == []
    assert [p for p, _ in _running(rig.slots)] == ["P1", "P2", "P3"]


def test_the_ceiling_counts_only_running_jobs_not_kept_convenience_windows():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    assert rig.slots.acquire("c", "P3") is not None
    assert rig.slots.acquire("d", "P4") is not None        # два идут — потолок взят
    e = rig.start("e", "P5")
    assert _until(lambda: e["waits"])
    time.sleep(0.15)
    assert "grant" not in e and rig.closed == []          # закрытие окна потолка не освободит
    assert e["waits"][0]["reason"] == "ceiling"
    rig.slots.release("c")
    assert _until(lambda: "grant" in e)


def test_when_memory_is_short_the_oldest_convenience_window_gives_way_at_once():
    # ACL-2668: ни минуты простоя — окно только что оставлено, а уступает сразу.
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.now += 1
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += 1
    rig.avail_mb = SHORT_MB
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert rig.closed == [("a", "KLD_A", "P1")]            # закрыто ровно самое давнее
    assert [x["label"] for x in c["grant"]["closed_for_room"]] == ["KLD_A"]
    assert [p for p, _ in _running(rig.slots)] == ["P2", "P3"]


def test_the_machine_setting_can_still_bring_back_the_idle_wait():
    # Аварийный выключатель: `ACL_KEPT_IDLE_CLOSE_MIN` на машине — окно ждёт простоя, как до ACL-2668.
    rig = Rig(idle_close_s=IDLE_S)
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.avail_mb = SHORT_MB
    c = rig.start("c", "P3")
    assert _until(lambda: c["waits"])
    time.sleep(0.15)
    assert "grant" not in c and rig.closed == []
    assert c["waits"][0]["reason"] == "below_reserve"
    assert c["waits"][0]["convenience_accounts"] == ["KLD_A"]
    rig.now += IDLE_S + 1
    assert _until(lambda: "grant" in c)
    assert [x[1] for x in rig.closed] == ["KLD_A"]


def test_review_windows_are_never_auto_closed_however_long_they_stay():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_REVIEW)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_REVIEW)
    rig.now += 24 * 3600
    c = rig.start("c", "P3")
    assert _until(lambda: c["waits"])
    time.sleep(0.15)
    assert "grant" not in c and rig.closed == []
    assert c["waits"][0]["reason"] == "review_windows"
    assert sorted(c["waits"][0]["kept_accounts"]) == ["KLD_A", "KLD_B"]
    assert pj._queue_view(c["waits"][0])["state"] == "waiting_review"
    _drop_waiter(rig, "c")


def test_review_windows_are_not_closed_for_memory_either():
    rig = Rig(ceiling=4)
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_REVIEW)
    rig.avail_mb = SHORT_MB
    c = rig.start("c", "P3")
    assert _until(lambda: c["waits"])
    time.sleep(0.15)
    assert "grant" not in c and rig.closed == []
    _drop_waiter(rig, "c")


def test_only_the_convenience_window_is_closed_when_a_review_window_is_open_too():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_REVIEW", ps.KEPT_REVIEW)       # самое давнее, но на проверке
    rig.now += 10
    rig.run_and_keep("b", "P2", "KLD_CONV", ps.KEPT_CONVENIENCE)
    rig.avail_mb = SHORT_MB
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert rig.closed == [("b", "KLD_CONV", "P2")]
    assert ("P1", ps.KEPT_REVIEW) in _running(rig.slots)


def test_a_failed_close_keeps_the_window_and_does_not_retry_in_a_loop():
    rig = Rig(evict_ok=False)
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.avail_mb = SHORT_MB
    c = rig.start("c", "P3")
    assert _until(lambda: rig.closed)
    time.sleep(0.2)
    assert "grant" not in c and len(rig.closed) == 1
    assert len(_running(rig.slots)) == 2
    _drop_waiter(rig, "c")


def test_a_job_for_an_account_whose_window_is_open_reuses_it_without_closing_anything():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    again = rig.start("a2", "P1")
    assert _until(lambda: "grant" in again)
    assert rig.closed == []
    assert again["grant"]["reused_kept"] == ps.KEPT_CONVENIENCE
    assert [p for p, _ in _running(rig.slots)].count("P1") == 1


def test_a_review_window_stays_review_when_the_next_job_reuses_it():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_REVIEW)
    again = rig.start("a2", "P1")
    assert _until(lambda: "grant" in again)
    assert again["grant"]["reused_kept"] == ps.KEPT_REVIEW


def test_by_default_there_is_no_idle_wait_and_the_machine_env_is_in_minutes():
    assert ps.load_settings({}).kept_idle_close_s == 0.0
    assert ps.load_settings({"ACL_KEPT_IDLE_CLOSE_MIN": "3"}).kept_idle_close_s == 180.0


def test_a_touched_window_is_closed_last():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += 5
    rig.slots.touch("kld_a")
    rig.avail_mb = SHORT_MB
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert [x[1] for x in rig.closed] == ["KLD_B"]


def test_a_kept_window_does_not_make_the_queue_working_but_a_running_job_does():
    # ACL-2668: оставленное окно не откладывает утреннее чтение и пересборку отчёта.
    rig = Rig()
    assert rig.slots.working() is False
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_REVIEW)
    assert rig.slots.working() is False
    assert rig.slots.occupied() is True                    # подключение MLX окна всё же держат
    rig.slots.acquire("c", "P3")
    assert rig.slots.working() is True
    rig.slots.release("c")
    assert rig.slots.working() is False


# ── задания: вид оставленного окна и строка в панели ────────────────────────

def _ok_result(name="C1"):
    return {"campaign_name": name, "backend": "api", "success": True, "publishable": True,
            "blocking_failures": [], "readback": {"ok": True}, "error": None}


def _mismatch_result(name="C1"):
    return {"campaign_name": name, "backend": "api", "success": True, "publishable": False,
            "blocking_failures": ["readback mismatch"], "readback": {"ok": False}, "error": None}


class FakePublisher:
    scenario: dict = {}

    def __init__(self, *, progress=None, dry_run=False):
        self.cancel_event = threading.Event()
        self.writes = 0
        self.inherited_profile = False
        self.stop = self.scenario.get("stop")

    def cancel(self):
        self.cancel_event.set()

    def cancelled(self):
        return self.cancel_event.is_set()

    def build_drafts(self, slots, *, account_label, country):
        return [SimpleNamespace(name="C1", errors=[])]

    def open(self, account_label):
        if self.scenario.get("open_raises"):
            raise publish_api.ApiPublishError(self.scenario["open_raises"])

    def publish_draft(self, draft):
        self.writes += self.scenario.get("writes", 3)
        return self.scenario["result"]("C1")

    def finish_job(self, account_label, *, clean, other_job_waits=False):
        return "kept" if clean else ""

    @property
    def started_profile(self):
        return True

    def close(self, *, force=False):
        pass


@pytest.fixture
def jobs(monkeypatch):
    with pj._jobs_lock:
        before = dict(pj._jobs)
        pj._jobs.clear()
    rig = Rig()
    monkeypatch.setattr(pj, "_slots", rig.make_slots(lambda j, l, p, i=0.0: pj._evict_kept_window(j, l, p, i)))
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "_fb_backend", lambda: "api")
    monkeypatch.setattr(pj, "_clear_inherited_asyncio_loop", lambda: None)
    monkeypatch.setattr(pj, "keep_display_awake", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pj, "_start_stuck_watch", lambda *a, **k: None)
    monkeypatch.setattr(pj, "profile_key_for", _key)
    monkeypatch.setattr(publish_api, "ApiPublisher", FakePublisher)
    stops: list[str] = []

    def stop(label, profile, busy_msg="", **kw):
        stops.append(label)
        rig.avail_mb += FREED_MB
        return {"ok": True}

    monkeypatch.setattr(pj, "_stop_idle_profile", stop)

    def run(label, result=_ok_result, **scenario):
        FakePublisher.scenario = {"result": result, **scenario}
        job_id = pj.create_job(label, "CL", [{}])
        pj._run_job(job_id, [], account_label=label, country="CL", via_multilogin=True,
                    activate=False, dry_run=False, hand_off=True)
        with pj._jobs_lock:
            return job_id, dict(pj._jobs[job_id])

    run.rig, run.stops = rig, stops
    yield run
    with pj._jobs_lock:
        pj._jobs.clear()
        pj._jobs.update(before)


def _kinds():
    return sorted((r["profile"], r["kept_kind"]) for r in pj._slots.snapshot()["running"])


def test_a_clean_publish_leaves_a_convenience_window_with_an_explaining_line(jobs):
    _, job = jobs("KLD_A")
    assert _kinds() == [(_key("KLD_A"), ps.KEPT_CONVENIENCE)]
    assert publish_api.PROFILE_CLOSED_NOTICE not in job["creative_notices"]
    assert pj.API_KEPT_NOTICE in job["creative_notices"]


def test_a_readback_mismatch_leaves_a_review_window(jobs):
    jobs("KLD_A", result=_mismatch_result)
    assert _kinds() == [(_key("KLD_A"), ps.KEPT_REVIEW)]


def test_three_clean_publishes_with_a_ceiling_of_two_close_nothing_while_memory_is_free(jobs):
    jobs("KLD_A")
    jobs("KLD_B")
    _, job3 = jobs("KLD_C")
    assert jobs.stops == []
    assert not any("make room" in n for n in job3["creative_notices"])
    assert {p for p, _ in _kinds()} == {_key("KLD_A"), _key("KLD_B"), _key("KLD_C")}


def test_short_memory_closes_the_oldest_kept_window_at_once_and_says_so_in_both_panels(jobs):
    rig = jobs.rig
    first, _ = jobs("KLD_A")
    rig.now += 30
    jobs("KLD_B")
    rig.now += 1
    rig.avail_mb = SHORT_MB
    _, job3 = jobs("KLD_C")
    assert jobs.stops == ["KLD_A"]                          # закрыто через остановку профиля
    assert "Closed KLD_A to make room" in job3["creative_notices"]
    with pj._jobs_lock:
        old = dict(pj._jobs[first])
    assert old["profile_closed"] is True
    assert any("make room" in n for n in old["creative_notices"])
    assert {p for p, _ in _kinds()} == {_key("KLD_B"), _key("KLD_C")}


def test_review_windows_block_the_third_job_and_nothing_is_closed(jobs):
    rig = jobs.rig
    jobs("KLD_A", result=_mismatch_result)
    jobs("KLD_B", result=_mismatch_result)
    rig.now += 24 * 3600
    FakePublisher.scenario = {"result": _ok_result}
    job_id = pj.create_job("KLD_C", "CL", [{}])
    t = threading.Thread(target=pj._run_job, args=(job_id, []), kwargs=dict(
        account_label="KLD_C", country="CL", via_multilogin=True, activate=False,
        dry_run=False, hand_off=True), daemon=True)
    t.start()
    assert _until(lambda: (pj._jobs[job_id].get("queue") or {}).get("hold") == "review_windows")
    time.sleep(0.15)
    assert jobs.stops == []
    with pj._jobs_lock:
        pj._jobs[job_id]["cancel_requested"] = True
    pj._slots.wake()
    t.join(3)


def test_the_next_job_for_an_account_with_an_open_window_reuses_it(jobs):
    jobs("KLD_A")
    _, job = jobs("KLD_A")                                  # тот же аккаунт: окно открыто
    assert job["reused_kept"] == ps.KEPT_CONVENIENCE
    assert jobs.stops == []
    assert _kinds() == [(_key("KLD_A"), ps.KEPT_CONVENIENCE)]


# ── выключатель ACL-1444 ────────────────────────────────────────────────────

@pytest.fixture
def real_publisher(monkeypatch):
    stops = []
    monkeypatch.setattr(publish_api, "_stop_profile", lambda e, label: stops.append(label))
    from src.fb_api import stop_log
    monkeypatch.setattr(stop_log, "record_stop", lambda pid: None)

    def make(*, started=True):
        pub = publish_api.ApiPublisher()
        pub._opened = SimpleNamespace(started_now=started)
        pub._entry = SimpleNamespace(profile_id="")
        return pub

    make.stops = stops
    return make


def test_by_default_a_clean_publish_keeps_the_profile_open(real_publisher, monkeypatch):
    monkeypatch.delenv("FB_RPA_KEEP_PROFILES_OPEN", raising=False)
    assert real_publisher().finish_job("A", clean=True) == "kept"
    assert real_publisher.stops == []


def test_with_the_switch_off_a_clean_publish_closes_the_profile_as_before(real_publisher, monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "0")
    assert real_publisher().finish_job("A", clean=True) == "closed"
    assert real_publisher.stops == ["A"]


def test_an_unclean_job_never_reports_a_convenience_keep(real_publisher, monkeypatch):
    monkeypatch.delenv("FB_RPA_KEEP_PROFILES_OPEN", raising=False)
    assert real_publisher().finish_job("A", clean=False) == ""


def test_a_window_inherited_from_a_previous_job_is_ours_to_keep_or_close(real_publisher, monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "0")
    pub = real_publisher(started=False)
    assert pub.started_profile is False
    pub.inherited_profile = True
    assert pub.started_profile is True
    assert pub.finish_job("A", clean=True) == "closed"


# ── ревью ACL-2113: остановленные до записи задания — не «для удобства» ──────

def test_a_checkpoint_before_any_write_leaves_a_review_window_that_is_never_auto_closed(jobs):
    rig = jobs.rig
    _, job = jobs("KLD_A", open_raises="Facebook checkpoint: solve it inside Multilogin", writes=0)
    assert _kinds() == [(_key("KLD_A"), ps.KEPT_REVIEW)]
    assert job["review_window"] is True
    assert pj.API_STILL_OPEN_NOTICE in job["creative_notices"]
    jobs("KLD_B")
    rig.now += 24 * 3600
    kept = {r["profile"]: r["kept_kind"] for r in pj._slots.snapshot()["running"]}
    assert kept == {_key("KLD_A"): ps.KEPT_REVIEW, _key("KLD_B"): ps.KEPT_CONVENIENCE}
    with pj._slots._cond:
        cand = pj._slots._idle_candidate("other", pj._slots._settings())
        assert pj._slots._running[cand]["profile"] == _key("KLD_B")     # A на проверке — не кандидат


def test_a_preflight_error_leaves_a_review_window(jobs):
    jobs("KLD_A", open_raises="preflight: the ad account is disabled; nothing was created", writes=0)
    assert _kinds() == [(_key("KLD_A"), ps.KEPT_REVIEW)]


def test_a_stopped_publisher_is_not_a_convenience_window_even_if_the_results_look_clean(jobs):
    jobs("KLD_A", stop=RuntimeError("stopped by Meta"))
    assert _kinds() == [(_key("KLD_A"), ps.KEPT_REVIEW)]


# ── ревью: RPA — «для удобства» только после проверок закрытия ───────────────

class _RpaProvider:
    def __init__(self, owns=True):
        self.owns = owns

    def close_finished_profile(self, entry, label):
        return "closed"

    def robot_owns_profile(self, entry):
        return self.owns

    def close(self, entry, *, keep_browser_open=False):
        pass


def _rpa(monkeypatch, *, saved=True, owns=True):
    from src.fb_ads_rpa import rpa_publisher as rp

    monkeypatch.delenv(rp.ENV_KEEP_PROFILES_OPEN, raising=False)
    pub = rp.RpaPublisher.__new__(rp.RpaPublisher)
    pub._provider = _RpaProvider(owns)
    pub._conn = SimpleNamespace(page=SimpleNamespace(url="x"), close=lambda keep_browser_open=False: None)
    pub._entry = SimpleNamespace(profile_id="p")
    pub._cancelled = False
    monkeypatch.setattr(rp.RpaPublisher, "_draft_saved", staticmethod(lambda page: saved))
    return pub


def test_rpa_a_clean_saved_robot_window_is_a_convenience_keep(monkeypatch):
    assert _rpa(monkeypatch).finish_job("A", clean=True) == "kept_switch"


def test_rpa_an_unsaved_draft_is_not_a_convenience_keep_even_with_the_switch_on(monkeypatch):
    assert _rpa(monkeypatch, saved=False).finish_job("A", clean=True) == "kept_unsaved"


def test_rpa_a_hand_opened_profile_is_not_a_convenience_keep(monkeypatch):
    assert _rpa(monkeypatch, owns=False).finish_job("A", clean=True) == "hand_opened"


def test_rpa_an_unclean_job_is_not_a_convenience_keep(monkeypatch):
    assert _rpa(monkeypatch).finish_job("A", clean=False) == "kept_problem"


def test_rpa_the_real_provider_knows_whether_the_robot_owns_the_window(monkeypatch):
    from pathlib import Path

    from src.fb_ads_rpa import browser as br

    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: Path("C:/mlx/" + pid))
    monkeypatch.setattr(br, "_profile_pids", lambda udd, pid: frozenset({101}))
    monkeypatch.setattr(br, "_ROBOT_PROFILES", {})
    provider = br.MultiloginAttachOnlyProvider()
    entry = SimpleNamespace(profile_id="550e8400-e29b-41d4-a716-446655440000")
    assert provider.robot_owns_profile(entry) is False
    br._mark_robot_profile(entry.profile_id, "C:/mlx/" + entry.profile_id)
    assert provider.robot_owns_profile(entry) is True


# ── ревью: мелочи ───────────────────────────────────────────────────────────

def test_the_queue_lock_is_free_while_the_idle_window_is_being_stopped():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    rig.run_and_keep("b", "P2", "KLD_B", ps.KEPT_CONVENIENCE)
    rig.now += 42
    rig.avail_mb = SHORT_MB
    c = rig.start("c", "P3")
    assert _until(lambda: "grant" in c)
    assert rig.lock_free is True
    assert rig.idle_seen == [42.0]


def test_a_window_that_is_being_closed_is_not_reused_and_its_account_waits_for_the_stop():
    rig = Rig()
    rig.run_and_keep("a", "P1", "KLD_A", ps.KEPT_CONVENIENCE)
    with rig.slots._cond:
        rig.slots._running["a"]["closing"] = True
        assert rig.slots._reuse_kept("P1") == ""
        assert "P1" in rig.slots._busy_profiles()


def test_a_stop_that_leaves_the_process_running_is_a_failed_auto_close(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp
    from src.fb_api import stop_log

    monkeypatch.setattr(pj, "_publish_serial", lambda profile: SimpleNamespace(
        acquire=lambda blocking=False: True, release=lambda: None))
    monkeypatch.setattr(rp, "close_account_profile", lambda label: "still_running")
    monkeypatch.setattr(stop_log, "record_stop", lambda pid: None)
    assert pj._stop_idle_profile("A", "p", busy_msg="x", strict=True)["ok"] is False
    assert pj._stop_idle_profile("A", "p", busy_msg="x")["ok"] is True      # кнопка Close profile как раньше


def test_a_read_session_restarts_the_idle_time_of_the_kept_window(monkeypatch):
    from contextlib import contextmanager

    from src.analysis_agent import account_spend_api as api

    touched = []
    monkeypatch.setattr(pj, "touch_kept", touched.append)
    monkeypatch.setattr(api._as, "_entry_for", lambda account: SimpleNamespace(
        profile_id=_key(account)))

    @contextmanager
    def inner(account, *, commands=False):
        yield "sess"

    monkeypatch.setattr(api, "_open_session", inner)
    with api._session("KLD_A") as sess:
        assert sess == "sess"
    assert touched == ["KLD_A"]
