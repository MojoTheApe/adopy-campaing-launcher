"""ACL-2260: the report never starts a rebuild of its own while a publish is active."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from src.analysis_agent import account_refresh, bi_stats
from src.web import app, cache, publish_jobs, report_store

OLD, NEW = ("bi", 1, None), ("bi", 2, None)


@pytest.fixture
def warm_cache(monkeypatch):
    """A report of the old source is in memory; the source key has moved on."""
    report = {"countries": [], "marker": "old"}
    monkeypatch.setattr(cache, "_CACHE", {**cache._CACHE, "entry": (OLD, {"": report}, "df", []), "key": OLD})
    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p: "book.xlsx")
    monkeypatch.setattr(cache, "_current_key", lambda _p: NEW)
    monkeypatch.setattr(cache, "_cm_key", lambda cm: "")
    monkeypatch.setattr(bi_stats, "bi_enabled", lambda: False)
    builds = []

    def build(expected_key, cm_key, progress=None):
        builds.append(expected_key)
        return {"marker": "new"}, "df2", []

    monkeypatch.setattr(cache, "_build_locked", build)
    return builds


@pytest.fixture
def busy(monkeypatch):
    monkeypatch.setattr(publish_jobs, "_jobs", {"j1": {"status": "running", "account_label": "A"}})


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(publish_jobs, "_jobs", {})
    monkeypatch.setattr(publish_jobs._slots, "occupied", lambda: False)


def test_rebuild_is_deferred_while_a_publish_job_runs(warm_cache, busy):
    report, _df, _w = cache.get_cached_report()
    assert report["marker"] == "old"
    assert warm_cache == []


def test_rebuild_is_deferred_while_a_kept_window_holds_a_slot(warm_cache, idle, monkeypatch):
    monkeypatch.setattr(publish_jobs._slots, "occupied", lambda: True)
    assert cache.get_cached_report()[0]["marker"] == "old"
    assert warm_cache == []


def test_rebuild_runs_when_idle(warm_cache, idle):
    assert cache.get_cached_report()[0]["marker"] == "new"
    assert warm_cache == [NEW]


def test_refresh_is_never_deferred(warm_cache, busy):
    assert cache.get_cached_report(force_rebuild=True)[0]["marker"] == "new"


def test_nothing_in_memory_still_builds(warm_cache, busy, monkeypatch):
    monkeypatch.setitem(cache._CACHE, "entry", None)
    assert cache.get_cached_report()[0]["marker"] == "new"


def test_finished_jobs_do_not_defer(monkeypatch):
    monkeypatch.setattr(publish_jobs, "_jobs", {"j": {"status": "done"}, "k": {"status": "failed"}})
    monkeypatch.setattr(publish_jobs._slots, "occupied", lambda: False)
    assert publish_jobs.publishing_busy() is False
    assert cache.rebuild_deferred() is False


def test_slots_occupied_covers_running_waiting_and_kept():
    slots = publish_jobs._slots
    assert slots.occupied() in (True, False)  # no exception on the live object
    fresh = type(slots).__new__(type(slots))
    import threading
    fresh._cond = threading.Condition()
    fresh._running, fresh._waiting = {}, {}
    assert fresh.occupied() is False
    fresh._running = {"j": {"kept": True}}
    assert fresh.occupied() is True
    fresh._running, fresh._waiting = {}, {"w": {}}
    assert fresh.occupied() is True


def _get(path, **params):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                     base_url="http://127.0.0.1") as client:
            return await client.get(path, params=params)
    return asyncio.run(request())


@pytest.fixture
def cold_page(monkeypatch, warm_cache):
    started = []
    monkeypatch.setattr(app, "_page_ready", lambda *a: False)
    monkeypatch.setattr(app, "_start_warm", lambda *a: started.append(a))
    monkeypatch.setattr(app, "forget_background_error", lambda: None)
    monkeypatch.setattr(app, "background_build_state",
                        lambda: {"building": False, "error": None, "token": None, "stage": None})
    return started


def test_status_does_not_start_a_rebuild_during_a_publish(cold_page, busy):
    body = _get("/report/status").json()
    assert body["deferred"] is True and body["ready"] is False
    assert cold_page == []


def test_status_starts_the_rebuild_once_idle(cold_page, idle):
    body = _get("/report/status").json()
    assert body["deferred"] is False
    assert len(cold_page) == 1


def test_page_shows_the_saved_report_and_waits_during_a_publish(cold_page, busy, monkeypatch):
    stored = report_store.StoredPage("<html><body><h1>Saved</h1></body></html>", time.time() - 7200, ["source"])
    monkeypatch.setattr(app.report_store, "exists", lambda inputs: True)
    monkeypatch.setattr(app, "_stored_page", lambda inputs: stored)
    response = _get("/")
    assert response.status_code == 200
    assert "Saved" in response.text and ">" + report_store.DEFERRED_TEXT + "<" in response.text
    assert cold_page == []


def test_page_starts_the_rebuild_when_idle(cold_page, idle, monkeypatch):
    stored = report_store.StoredPage("<html><body><h1>Saved</h1></body></html>", time.time() - 7200, ["source"])
    monkeypatch.setattr(app.report_store, "exists", lambda inputs: True)
    monkeypatch.setattr(app, "_stored_page", lambda inputs: stored)
    response = _get("/")
    assert ">" + report_store.DEFERRED_TEXT + "<" not in response.text
    assert len(cold_page) == 1


def test_scheduled_account_reads_wait_for_a_publish(busy, monkeypatch):
    monkeypatch.setenv("ACL_AUTO_REFRESH", "1")
    queue = account_refresh.RefreshQueue.__new__(account_refresh.RefreshQueue)
    now = account_refresh.datetime(2026, 10, 28, 9, 0).astimezone()
    result = queue.page_open(now=now, port=9090)
    assert result == {"sweep": "deferred_publish", "window": "08:00", "queued": 0}
