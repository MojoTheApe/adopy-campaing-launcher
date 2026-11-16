"""ACL-2260 / ACL-2668 (#24): the report defers a rebuild of its own only while memory is short.

A running publish or a kept profile window no longer defers it: the rebuild reads BI / the
workbook, not Meta. The scheduled account-read sweep still waits for a publish (not changed here).
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from src.analysis_agent import account_refresh, bi_stats
from src.web import app, cache, profile_slots, publish_jobs, report_store

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


TOTAL_MB = 16384
SETTINGS = profile_slots.SlotSettings(max_used_pct=92.0)


def _memory(monkeypatch, available_mb):
    monkeypatch.setattr(profile_slots, "load_settings", lambda *a: SETTINGS)
    monkeypatch.setattr(profile_slots, "read_memory",
                        lambda: profile_slots.MemoryReading(total_mb=TOTAL_MB, available_mb=available_mb))


@pytest.fixture
def short(monkeypatch):
    """More than 92 % of memory is in use."""
    _memory(monkeypatch, 500)


@pytest.fixture
def roomy(monkeypatch):
    _memory(monkeypatch, 8000)


@pytest.fixture
def busy(monkeypatch):
    """A publish job runs and a kept window holds a slot."""
    monkeypatch.setattr(publish_jobs, "_jobs", {"j1": {"status": "running", "account_label": "A"}})
    monkeypatch.setattr(publish_jobs._slots, "occupied", lambda: True)


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(publish_jobs, "_jobs", {})
    monkeypatch.setattr(publish_jobs._slots, "occupied", lambda: False)


def test_rebuild_is_deferred_while_memory_is_short(warm_cache, idle, short):
    report, _df, _w = cache.get_cached_report()
    assert report["marker"] == "old"
    assert warm_cache == []


def test_rebuild_runs_during_a_publish_and_a_kept_window(warm_cache, busy, roomy):
    assert publish_jobs.publishing_busy() is True
    assert cache.rebuild_deferred() is False
    assert cache.get_cached_report()[0]["marker"] == "new"
    assert warm_cache == [NEW]


def test_rebuild_runs_when_idle(warm_cache, idle, roomy):
    assert cache.get_cached_report()[0]["marker"] == "new"
    assert warm_cache == [NEW]


def test_threshold_is_the_profile_memory_gate(monkeypatch):
    # The same keep-free amount that holds a new profile back: at it - not short, one MB less - short.
    keep = profile_slots.keep_free_mb(TOTAL_MB, SETTINGS)
    _memory(monkeypatch, keep)
    assert cache.rebuild_deferred() is False
    _memory(monkeypatch, keep - 1)
    assert cache.rebuild_deferred() is True


def test_unreadable_memory_does_not_defer(monkeypatch):
    monkeypatch.setattr(profile_slots, "read_memory", lambda: None)
    assert cache.rebuild_deferred() is False

    def boom():
        raise RuntimeError("no psutil")

    monkeypatch.setattr(profile_slots, "read_memory", boom)
    assert cache.rebuild_deferred() is False


def test_refresh_is_never_deferred(warm_cache, short):
    assert cache.get_cached_report(force_rebuild=True)[0]["marker"] == "new"


def test_nothing_in_memory_still_builds(warm_cache, short, monkeypatch):
    monkeypatch.setitem(cache._CACHE, "entry", None)
    assert cache.get_cached_report()[0]["marker"] == "new"


def test_finished_jobs_are_not_busy(monkeypatch):
    monkeypatch.setattr(publish_jobs, "_jobs", {"j": {"status": "done"}, "k": {"status": "failed"}})
    monkeypatch.setattr(publish_jobs._slots, "occupied", lambda: False)
    assert publish_jobs.publishing_busy() is False


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


def test_status_does_not_start_a_rebuild_while_memory_is_short(cold_page, short):
    body = _get("/report/status").json()
    assert body["deferred"] is True and body["ready"] is False
    assert cold_page == []


def test_status_starts_the_rebuild_during_a_publish(cold_page, busy, roomy):
    body = _get("/report/status").json()
    assert body["deferred"] is False
    assert len(cold_page) == 1


def test_page_shows_the_saved_report_and_waits_while_memory_is_short(cold_page, short, monkeypatch):
    stored = report_store.StoredPage("<html><body><h1>Saved</h1></body></html>", time.time() - 7200, ["source"])
    monkeypatch.setattr(app.report_store, "exists", lambda inputs: True)
    monkeypatch.setattr(app, "_stored_page", lambda inputs: stored)
    response = _get("/")
    assert response.status_code == 200
    assert "Saved" in response.text and ">" + report_store.DEFERRED_TEXT + "<" in response.text
    assert cold_page == []


def test_page_starts_the_rebuild_during_a_publish(cold_page, busy, roomy, monkeypatch):
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


# ── ACL-2508: the notice says what and why; Refresh ends in the fresh page ──

def test_notice_states_saved_version_reason_and_action():
    text = report_store.DEFERRED_TEXT
    assert "saved version" in text
    assert "short of memory" in text
    assert "profile window" not in text and "publish" not in text
    assert "Refresh" in text


def test_notice_is_hidden_when_not_deferred(cold_page, busy, roomy, monkeypatch):
    stored = report_store.StoredPage("<html><body><h1>Saved</h1></body></html>", time.time() - 60, [])
    monkeypatch.setattr(app.report_store, "exists", lambda inputs: True)
    monkeypatch.setattr(app, "_stored_page", lambda inputs: stored)
    assert ">" + report_store.DEFERRED_TEXT + "<" not in _get("/").text


def test_refresh_warms_the_period_of_the_page(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "get_cached_report", lambda force_rebuild=False: ({"r": 1}, "df", []))
    monkeypatch.setattr(app, "resolve_period", lambda df, f, t, s: (f or "2026-01-01", t))
    monkeypatch.setattr(app, "report_for_period", lambda r, df, f, t: calls.append((f, t)))
    app._refresh_and_warm_period("2026-02-01", "2026-02-09", "")
    assert calls == [("2026-02-01", "2026-02-09")]


def test_refresh_endpoint_reads_period_from_query_and_referer(monkeypatch):
    seen = []
    monkeypatch.setattr(app, "cache_clear", lambda **k: None)
    monkeypatch.setattr(app, "_refresh_and_warm_period", lambda f, t, s: seen.append((f, t, s)))
    monkeypatch.setattr(app, "cache_built_at", lambda: "now")

    async def post(url, headers=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                     base_url="http://127.0.0.1") as client:
            return await client.post(url, headers={"Accept": "application/json", **(headers or {})})

    assert asyncio.run(post("/refresh?date_from=2026-02-01&date_to=2026-02-09&span=")).json()["ok"] is True
    r2 = asyncio.run(post("/refresh", {"Referer": "http://127.0.0.1/?date_from=2026-03-01&date_to=2026-03-09"}))
    assert r2.status_code == 200, r2.text
    r5 = asyncio.run(post("/refresh", {"Referer": "http://127.0.0.1/?span=all"}))
    assert r5.status_code == 200, r5.text
    assert seen == [("2026-02-01", "2026-02-09", ""), ("2026-03-01", "2026-03-09", ""), ("", "", "all")]


@pytest.mark.parametrize("referer,expected", [
    ("http://evil.example/?span=all", {}),
    ("http://127.0.0.1:9999/?span=all", {}),
    ("https://127.0.0.1/?span=all", {}),
    ("not a url", {}),
    ("http://127.0.0.1/?span=all&x=1", {"span": ["all"], "x": ["1"]}),
])
def test_referer_is_used_only_when_same_origin(referer, expected):
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "scheme": "http", "path": "/refresh",
                       "query_string": b"", "server": ("127.0.0.1", 80),
                       "headers": [(b"host", b"127.0.0.1"), (b"referer", referer.encode())]})
    assert app._same_origin_referer_query(request) == expected


@pytest.mark.parametrize("bad", [("a", "b", "c"), ("2026-13-01", "", ""), ("2026-02-01", "x", ""),
                                 ("", "", "7"), ("2026-02-01", "2026-02-09", "junk")])
def test_refresh_ignores_malformed_period_and_never_caches_it(monkeypatch, bad):
    app._PERIOD_CACHE.clear()
    monkeypatch.setattr(app, "get_cached_report", lambda force_rebuild=False: ({"r": 1}, "df", []))
    resolved = []
    monkeypatch.setattr(app, "resolve_period", lambda df, f, t, s: resolved.append((f, t, s)) or ("2026-01-01", "2026-01-31"))
    monkeypatch.setattr(app, "report_for_period", lambda r, df, f, t: None)
    app._refresh_and_warm_period(*bad)
    assert resolved == [("", "", "")]
    assert not app._PERIOD_CACHE


def test_refresh_while_short_makes_the_page_ready_and_serves_fresh(warm_cache, short, monkeypatch):
    """After Refresh the page is ready, so / renders the live page, not the saved one."""
    report, _df, _w = cache.get_cached_report(force_rebuild=True)
    assert report["marker"] == "new"
    monkeypatch.setattr(app, "peek_cached_report", lambda: (report, None, []))
    assert app._page_ready("", "", "") is True
