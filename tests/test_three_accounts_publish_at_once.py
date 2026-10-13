# -*- coding: utf-8 -*-
"""Несколько аккаунтов публикуются разом, и каждый прогон остаётся своим (acl-1210).

Очередь «одна публикация на процесс» была нашим решением 31.07; физическая
причина (фоновое окно Chrome душилось) ушла 30.07, и 16.09 владелец сделал
«отложенный осознанный шаг». Что на самом деле мешало — состояние на процесс,
и здесь проверяется, что его больше нет:

* задания на разные профили идут одновременно, на один профиль — по очереди
  (замок — в `test_browser_boundary_contract`, здесь — сам `_run_job`);
* телеметрия из двух потоков подписана своим `run_id`, а не чужим;
* диагностика загрузки одного потока не затирает другую;
* скриншоты прогона ложатся в свою папку.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
from tests.profile_lease_fakes import fake_profile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom, run_telemetry, screenshots  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402


# ── два задания на два профиля идут разом ─────────────────────────────────

@pytest.fixture
def quiet_runner(monkeypatch):
    """`_run_job` без RPA: подставной `_run_rpa` сигналит, что вошёл, и ждёт."""
    monkeypatch.setattr(pj, "use_rpa_publish", lambda: True)
    monkeypatch.setattr(pj, "keep_display_awake", lambda label: _Noop())
    monkeypatch.setattr(pj, "_watch_screen_lock", lambda job_id: _Noop())
    monkeypatch.setattr(pj, "profile_key_for", lambda label: fake_profile(label))  # ярлык = профиль
    # ACL-1321: сколько профилей разом, решает память машины. Здесь проверяется
    # параллельность, а не память ноутбука, на котором идут тесты.
    from src.web.profile_slots import ProfileSlots, SlotSettings

    monkeypatch.setattr(pj, "_slots", ProfileSlots(
        memory=lambda: None, settings=lambda: SlotSettings(override=4), tick_s=0.05))

    entered: dict[str, threading.Event] = {}
    release: dict[str, threading.Event] = {}

    def fake_rpa(job_id, slots, *, account_label, **kw):
        entered[account_label].set()
        release[account_label].wait(timeout=5)
        pj._finish(job_id)

    monkeypatch.setattr(pj, "_run_rpa", fake_rpa)

    def job(label: str) -> str:
        entered[label] = threading.Event()
        release[label] = threading.Event()
        job_id = f"job-{label}"
        with pj._jobs_lock:
            pj._jobs[job_id] = {
                "job_id": job_id, "account_label": label, "status": "queued",
                "results": [], "errors": [], "done_count": 0, "total": 1,
                "cancel_requested": False, "creative_notices": [],
            }
        t = threading.Thread(target=pj._run_job, args=(job_id, []), kwargs={
            "account_label": label, "country": "CL", "via_multilogin": True,
            "activate": False, "dry_run": False, "hand_off": True,
        }, daemon=True)
        t.start()
        return job_id

    return {"job": job, "entered": entered, "release": release}


class _Noop:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_two_accounts_on_two_profiles_run_at_the_same_time(quiet_runner):
    q = quiet_runner
    q["job"]("DQ501")
    assert q["entered"]["DQ501"].wait(timeout=5)
    q["job"]("Doc19")
    assert q["entered"]["Doc19"].wait(timeout=2), "второй аккаунт ждёт первого без причины"
    q["release"]["DQ501"].set()
    q["release"]["Doc19"].set()


def test_two_jobs_on_one_profile_still_queue(quiet_runner, monkeypatch):
    q = quiet_runner
    monkeypatch.setattr(pj, "profile_key_for", lambda label: fake_profile("one-profile"))
    q["job"]("DQ501")
    assert q["entered"]["DQ501"].wait(timeout=5)
    q["job"]("DQ501-again")
    assert not q["entered"]["DQ501-again"].wait(timeout=0.5), "два задания вошли в один профиль разом"
    assert pj.get_job("job-DQ501-again")["status"] == "queued"
    q["release"]["DQ501"].set()
    assert q["entered"]["DQ501-again"].wait(timeout=5)
    q["release"]["DQ501-again"].set()


# ── телеметрия: каждый поток — своим номером ──────────────────────────────

def test_telemetry_rows_from_two_threads_carry_their_own_run_ids(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", path)
    go = threading.Barrier(2)

    def worker(run_id: str):
        run_telemetry.set_current_run(run_id, f"camp-{run_id}")
        go.wait(timeout=5)
        for i in range(20):
            run_telemetry.record(f"step{i}", True)

    a = threading.Thread(target=worker, args=("run-A",))
    b = threading.Thread(target=worker, args=("run-B",))
    a.start(); b.start(); a.join(5); b.join(5)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by = {}
    for r in rows:
        by.setdefault(r["run_id"], set()).add(r["campaign"])
    assert by == {"run-A": {"camp-run-A"}, "run-B": {"camp-run-B"}}, by


def test_a_thread_without_a_run_writes_an_empty_run_id_not_a_neighbours(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", path)
    started = threading.Event()

    def busy():
        run_telemetry.set_current_run("run-busy", "camp")
        started.set()

    t = threading.Thread(target=busy); t.start(); t.join(5)
    assert started.is_set()
    seen = []

    def other():
        seen.append(run_telemetry.current_run_id())

    o = threading.Thread(target=other); o.start(); o.join(5)
    assert seen == [""]


def test_the_old_call_sites_still_get_the_run_id_for_free(tmp_path, monkeypatch):
    """`set_current_run` один раз — остальные записи получают номер даром, как прежде."""
    path = tmp_path / "t.jsonl"
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", path)
    run_telemetry.set_current_run("run-Z", "camp-Z")
    run_telemetry.record("x", True)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["run_id"] == "run-Z" and row["campaign"] == "camp-Z"


def test_the_old_dict_shaped_current_still_answers_for_this_thread():
    """Открытые ветки читают `_telemetry._current.get("run_id")` (мемо
    загрузки acl-1171); первое живое дерево с acl-1210 рядом с ней уронило
    все загрузки на «no attribute '_current'» (прогон 86b7cd7edda9)."""
    run_telemetry.set_current_run("run-compat", "camp")
    assert run_telemetry._current.get("run_id") == "run-compat"
    assert run_telemetry._current["campaign"] == "camp"
    seen = {}

    def other():
        seen["id"] = run_telemetry._current.get("run_id")

    t = threading.Thread(target=other); t.start(); t.join(5)
    assert seen["id"] == "", "чужой поток увидел наш номер прогона через _current"
    run_telemetry._current["run_id"] = "run-set"
    assert run_telemetry.current_run_id() == "run-set"


# ── диагностика загрузки — своя у каждого потока ──────────────────────────

def test_upload_diagnostics_do_not_overwrite_each_other_across_threads():
    go = threading.Barrier(2)
    seen = {}

    def worker(name: str):
        meta_dom._set_last_upload_diag({"file": name, "attempts": []})
        go.wait(timeout=5)
        seen[name] = meta_dom.last_upload_diag()["file"]

    a = threading.Thread(target=worker, args=("a.mp4",))
    b = threading.Thread(target=worker, args=("b.mp4",))
    a.start(); b.start(); a.join(5); b.join(5)
    assert seen == {"a.mp4": "a.mp4", "b.mp4": "b.mp4"}


def test_the_module_level_record_is_still_there_for_the_outside(monkeypatch):
    meta_dom._set_last_upload_diag({"file": "z.mp4", "attempts": []})
    assert meta_dom._LAST_UPLOAD_DIAG["file"] == "z.mp4"


# ── скриншоты — по папке на прогон ────────────────────────────────────────

def test_screenshots_of_a_run_land_in_its_own_folder(tmp_path):
    run_telemetry.set_current_run("run-shots", "c")
    got = screenshots.screenshot_dir(tmp_path / "shots")
    assert got == tmp_path / "shots" / "run-shots" and got.is_dir()


def test_outside_a_run_the_folder_is_the_base(tmp_path):
    seen = {}

    def other():
        seen["dir"] = screenshots.screenshot_dir(tmp_path / "shots")

    t = threading.Thread(target=other); t.start(); t.join(5)
    assert seen["dir"] == tmp_path / "shots"
