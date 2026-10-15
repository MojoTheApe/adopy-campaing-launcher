"""Real loading boundaries, job isolation and the owner-selected ACM screen."""
from __future__ import annotations

import asyncio
import threading

import httpx
import pandas as pd
import pytest

from src.analysis_agent import morning_service as service
from src.web import app, cache


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    book = tmp_path / "stats.xlsx"
    book.touch()
    monkeypatch.setenv("FB_STATS_EXCEL", str(book))
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Tester")
    monkeypatch.setattr(service, "_fallback_accounts_from_csv", lambda cm: [])
    monkeypatch.setattr(cache, "_CACHE", dict(cache._CACHE))
    monkeypatch.setattr(cache, "_WARM", {"thread": None, "error": None, "token": None, "stage": None})
    monkeypatch.setattr(app, "_PERIOD_CACHE", {})
    cache.cache_clear()
    yield book
    thread = cache._WARM["thread"]
    if thread is not None:
        thread.join(10)
        assert not thread.is_alive()


def get_status(**params):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                     base_url="http://127.0.0.1") as client:
            response = await client.get("/report/status", params=params)
            assert response.status_code == 200
            return response.json()
    return asyncio.run(request())


def test_workbook_progress_follows_real_read_and_processing(monkeypatch, isolated):
    stages = []
    raw = pd.DataFrame(columns=["Country", "Account Alias", "Adset Name", "Ad Title",
                                "# Leads", "# FTDs", "$ Spend", "CM", "Ad Asset", "$ Net"])

    def read(path):
        assert stages == ["reading"]
        return raw

    def metrics(frame):
        assert stages == ["reading", "processing"]
        return frame

    def report(**kwargs):
        assert stages == ["reading", "processing", "preparing"]
        return {"countries": []}

    monkeypatch.setattr(service.pd, "read_excel", read)
    monkeypatch.setattr(service, "compute_metrics", metrics)
    monkeypatch.setattr(service, "enrich_structure", lambda frame: frame)
    monkeypatch.setattr(service, "apply_variant_backfill", lambda frame: frame)
    monkeypatch.setattr(service, "build_morning_report", report)
    service.load_morning_report(skip_gsheet=True, progress=stages.append)
    assert stages == ["reading", "processing", "preparing"]


def test_cached_workbook_skips_reading_and_processing(monkeypatch, isolated):
    stages = []
    monkeypatch.setattr(service, "build_morning_report", lambda **kw: {"countries": []})
    monkeypatch.setattr(service.pd, "read_excel", lambda *a: pytest.fail("cached workbook was reread"))
    service.load_morning_report(enriched_df=pd.DataFrame(), account_rows_cached=[], progress=stages.append)
    assert stages == ["preparing"]


def test_read_failure_does_not_complete_the_workbook_stage(monkeypatch, isolated):
    stages = []

    def fail(path):
        raise ValueError("broken workbook")

    monkeypatch.setattr(service.pd, "read_excel", fail)
    with pytest.raises(ValueError, match="broken workbook"):
        service.load_morning_report(skip_gsheet=True, progress=stages.append)
    assert stages == ["reading"]


def test_callbacks_cannot_update_foreign_or_later_jobs(isolated):
    started, release = threading.Event(), threading.Event()
    callbacks = []

    def job():
        callback = cache.background_progress_callback()
        callbacks.append(callback)
        # The real Excel loader calls from its pool thread, not the job thread.
        worker = threading.Thread(target=lambda: callback("processing"))
        worker.start()
        worker.join(5)
        started.set()
        assert release.wait(10)

    try:
        assert cache.start_background_build(job, token="first")
        thread = cache._WARM["thread"]
        assert started.wait(5)
        assert cache.background_build_state()["stage"] == "processing"
        cache.background_progress_callback()("preparing")
        assert cache.background_build_state()["stage"] == "processing"
        release.set()
        thread.join(5)
        started.clear()
        release.clear()
        assert cache.start_background_build(job, token="second")
        assert started.wait(5)
        callbacks[0]("preparing")
        assert cache.background_build_state()["stage"] == "processing"
    finally:
        release.set()


def test_status_does_not_borrow_another_period_or_manager_progress(monkeypatch, isolated):
    started, release = threading.Event(), threading.Event()

    def build(*, progress, **kwargs):
        progress("reading")
        started.set()
        assert release.wait(10)
        progress("processing")
        return {"countries": [], "my_cm": kwargs["my_cm"]}, None, []

    monkeypatch.setattr(cache, "load_morning_report", build)
    try:
        get_status()
        thread = cache._WARM["thread"]
        assert started.wait(5)
        own = get_status()
        assert own["stage"] == "reading" and not own["waiting"]
        other_period = get_status(span="7")
        assert other_period["stage"] is None and other_period["waiting"]
        monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Other manager")
        other_manager = get_status()
        assert other_manager["stage"] is None and other_manager["waiting"]
        monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Tester")
        release.set()
        thread.join(5)
        assert get_status()["stage"] == "ready"
    finally:
        release.set()


def test_warm_base_with_a_cold_period_shows_only_preparation(monkeypatch, isolated):
    started, release = threading.Event(), threading.Event()
    frame = pd.DataFrame({"Date": ["2026-10-01"]})
    monkeypatch.setattr(cache, "load_morning_report", lambda **kw: ({"countries": []}, frame, []))
    cache.get_cached_report()

    def period(report, df, date_from, date_to):
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(app, "report_for_period", period)
    try:
        get_status(date_from="2026-10-01", date_to="2026-10-01")
        assert started.wait(5)
        status = get_status(date_from="2026-10-01", date_to="2026-10-01")
        assert not status["ready"] and status["stage"] == "preparing"
    finally:
        release.set()


def test_loading_screen_uses_server_stages_and_preserves_retry(monkeypatch, isolated):
    from playwright.sync_api import sync_playwright

    html = app.templates.env.get_template("loading.html").render()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 360, "height": 740})
            status = {"ready": False, "stage": "reading", "waiting": False}
            page.route("**/report/status*", lambda route: route.fulfill(json=status))
            page.route("http://loading.test/", lambda route: route.fulfill(body=html, content_type="text/html"))
            page.route("**/static/logo.svg", lambda route: route.fulfill(
                path=str(app._PROJ_ROOT / "src/web/static/logo.svg"), content_type="image/svg+xml"))
            page.goto("http://loading.test/")
            page.wait_for_function("document.querySelector('[data-stage=reading]').getAttribute('aria-current') === 'step'")
            assert page.locator(".spin").count() == 1
            assert page.locator("#report-loading-stage").inner_text() == "Reading the stats workbook…"
            assert page.locator(".logo").bounding_box()["y"] < page.locator("h1").bounding_box()["y"]
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            status.update(stage="preparing")
            page.wait_for_function("document.querySelectorAll('[data-state=complete]').length === 2")
            assert page.locator("[data-stage=preparing]").get_attribute("aria-current") == "step"
            assert page.locator("#report-loading-stage").inner_text() == "Preparing the dashboard…"
            status.update(stage=None, waiting=True)
            page.wait_for_function("document.querySelectorAll('[data-state=pending]').length === 3")
            assert page.locator(".spin").count() == 0
            assert "Waiting" in page.locator("#report-loading-stage").inner_text()
            page.unroute("**/report/status*")
            page.route("**/report/status*", lambda route: route.fulfill(status=503, body="offline"))
            page.locator("#report-loading-error").wait_for(state="visible")
            page.unroute("**/report/status*")
            page.route("**/report/status*", lambda route: route.fulfill(json={"ready": False, "stage": "processing"}))
            page.wait_for_function("document.querySelector('[data-stage=processing]').getAttribute('aria-current') === 'step'")
            assert page.locator("#report-loading-error").is_hidden()
            status.update(ready=True, stage="ready")
            page.unroute("**/report/status*")
            page.route("**/report/status*", lambda route: route.fulfill(json=status))
            with page.expect_navigation():
                pass
            page.close()
        finally:
            browser.close()
