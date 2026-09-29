"""ACL-1380: the dashboard opens in seconds and never freezes while it builds.

Covered here:
  - the reading-rules file is parsed once per change, not once per row;
  - the faster paths (rules read once per table, alias matching per distinct
    value, interest buckets per distinct value) give the SAME enriched table,
    report and account map as the old per-row code on a fixture;
  - the page and /details build in a worker thread: a light request answers
    while a deliberately slow build is still running;
  - cancelling a job never runs Playwright teardown on the server's loop
    thread (the 29.09 12:55 "loop ... is not the running loop" freeze);
  - one report build at a time, and the background warm-up builds the
    default period together with the report;
  - the targeting library is not pulled over the network on the render path.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

import config.analysis_rules as rules
from src.analysis_agent import analyze_fb_stats as afs
from src.analysis_agent import morning_report, morning_service, section_filters
from src.web import app as web
from src.web import cache, publish_jobs, report_warmup


# ── helpers ───────────────────────────────────────────────────────────────

def _legacy_load_raw():
    """The pre-ACL-1380 reader: opens and parses the file on every call."""
    if not rules._RULES_PATH.is_file():
        return {}
    try:
        with open(rules._RULES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _legacy_normalize_text_segment(ad_title, *_ignored) -> str:
    if ad_title is None or (isinstance(ad_title, float) and pd.isna(ad_title)):
        return rules.get_text_segment_fallback()
    text = str(ad_title).lower()
    for segment, variants in rules.get_text_segment_dictionary().items():
        for v in variants:
            if v in text:
                return segment.upper()
    return rules.get_text_segment_fallback()


def _legacy_alias_matches(series, alias):
    target = afs.normalize_account_alias(alias)
    return series.fillna("").astype(str).map(afs.normalize_account_alias) == target


def _legacy_bucket_keys(values):
    return values.map(section_filters.interest_bucket_key)


def _raw_workbook() -> pd.DataFrame:
    """A small raw export: annotated aliases, NaNs, KB/unknown interests, segments."""
    today = datetime(2026, 9, 20)
    countries = [("Mexico", "MX"), ("Chile", "CL"), ("Colombia", "CO")]
    aliases = ["FacebookDQ365", "FacebookDQ365 EC", "FacebookMKTOBFLV37 EC Ait", "KLD_BFL_S15", None]
    interests = ["Trading", "trading", "HV|Trading", "AAY_Day", "day", "Zzqunknown", "wide", ""]
    titles = ["2046 1 - Story14 pro", "2257 Story3 reg", "Vid 2257 - learn", "Blue - modern", None, "Generic"]
    cms = ["Evgeni Sedin", "Other CM", None]
    rows = []
    for i in range(72):
        country, code = countries[i % 3]
        interest = interests[i % len(interests)]
        rows.append({
            "Country": country,
            "Account Alias": aliases[i % len(aliases)],
            "Adset Name": f"{code}_offer_{'ios' if i % 4 == 0 else 'all'}_vid_{interest or 'wide'}_{25 + i % 3 * 10}",
            "Ad Title": titles[i % len(titles)],
            "# Leads": 10 + i % 7,
            "# FTDs": i % 3,
            "$ Spend": 20.0 + i,
            "CM": cms[i % len(cms)],
            "Ad Asset": ["Amazon", "Favorita", "Bank"][i % 3],
            "$ Net": float(i % 5 * 10),
            "Date": today - timedelta(days=i % 14),
        })
    return pd.DataFrame(rows)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MY_CM_NAME", "Evgeni Sedin")
    monkeypatch.delenv("ACL_DASHBOARD_BACKGROUND", raising=False)
    source = tmp_path / "synthetic.xlsx"
    source.touch()
    monkeypatch.setenv("FB_STATS_EXCEL", str(source))
    monkeypatch.setattr(web, "_PERIOD_CACHE", {})
    monkeypatch.setattr(cache, "_CACHE", deepcopy(cache._CACHE))
    cache.cache_clear()
    monkeypatch.setattr(morning_service, "_fallback_accounts_from_csv", lambda cm: [])
    # The unknown-targets journal lives in the repo's output/: keep tests out of it.
    monkeypatch.setattr(afs, "log_unknown_target", lambda *a, **k: None)
    return source


def _asgi_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                             base_url="http://127.0.0.1:9090", timeout=30)


# ── 1. the rules file is read once per change ────────────────────────────

def test_rules_file_is_parsed_once_until_it_changes(monkeypatch, tmp_path):
    path = tmp_path / "analysis_reading_rules.json"
    path.write_text(json.dumps({"text_segments": {"alpha": ["aa"]}}), encoding="utf-8")
    monkeypatch.setattr(rules, "_RULES_PATH", path)
    monkeypatch.setattr(rules, "_raw_cache", None)
    reads = []
    monkeypatch.setattr(rules, "json", SimpleNamespace(
        load=lambda f: reads.append(1) or json.load(f), JSONDecodeError=json.JSONDecodeError))

    for _ in range(500):
        assert rules.get_text_segment_dictionary() == {"alpha": ["aa"]}
    assert len(reads) == 1

    # An edit is picked up without a restart (new size and a new mtime).
    path.write_text(json.dumps({"text_segments": {"beta": ["bbb"]}}), encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert rules.get_text_segment_dictionary() == {"beta": ["bbb"]}
    assert len(reads) == 2

    path.unlink()
    assert rules.get_text_segment_dictionary() == dict(rules._DEFAULT_TEXT_SEGMENTS)


def test_enrich_reads_the_rules_once_per_table(monkeypatch, isolated):
    calls = []
    real = rules._load_raw
    monkeypatch.setattr(rules, "_load_raw", lambda: calls.append(1) or real())
    afs.enrich_structure(afs.compute_metrics(_raw_workbook()))
    # device keywords, age range, skip prefixes, segments, fallback — per table.
    assert len(calls) <= 6


# ── 2. same numbers as the old per-row code ──────────────────────────────

def _build_everything(accounts):
    df = afs.enrich_structure(afs.compute_metrics(_raw_workbook()))
    report, _df, _w = morning_service.load_morning_report(
        enriched_df=df, my_cm="Evgeni Sedin", account_rows_cached=accounts)
    scoped = web.report_for_period(report, df, "2026-09-10", "2026-09-20")
    amap = web._build_account_map_view(report, df, "", "", "all")
    rows = [
        section_filters.filter_targeting_rows(df, c, b)["$ Spend"].sum()
        for c in ("Mexico", "Chile", "Colombia")
        for b in ("Trading", "Day", "__NA__", "zzqunknown")
    ]
    return df, report, scoped, amap, rows


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def test_report_output_is_identical_to_the_per_row_code(monkeypatch, isolated):
    accounts = [
        {"alias": "FacebookDQ365 EC", "account_label": "FacebookDQ365 EC", "country_code": "mx",
         "is_ready": True, "to_do": "", "cost": "100", "stock": "5"},
        {"alias": "KLD_BFL_S15", "account_label": "KLD_BFL_S15", "country_code": "cl",
         "is_ready": False, "to_do": "Fix", "cost": "80", "stock": "3"},
    ]
    new = _build_everything(accounts)

    with monkeypatch.context() as m:
        m.setattr(rules, "_load_raw", _legacy_load_raw)
        m.setattr(afs, "normalize_text_segment", _legacy_normalize_text_segment)
        m.setattr(afs, "alias_matches", _legacy_alias_matches)
        m.setattr(morning_report, "alias_matches", _legacy_alias_matches)
        m.setattr(section_filters, "alias_matches", _legacy_alias_matches)
        m.setattr(morning_report, "interest_bucket_keys", _legacy_bucket_keys)
        m.setattr(section_filters, "interest_bucket_keys", _legacy_bucket_keys)
        m.setattr(web, "_PERIOD_CACHE", {})
        old = _build_everything(accounts)

    pd.testing.assert_frame_equal(new[0], old[0])
    assert _canon(new[1]) == _canon(old[1])
    assert _canon(new[2]) == _canon(old[2])
    assert _canon(new[3]) == _canon(old[3])
    assert new[4] == old[4]
    # The fixture really exercises the paths it claims to.
    assert set(new[0]["TextSegment"]) >= {"PRO", "REG", "LEARN"}
    assert new[1]["countries"]


@pytest.mark.parametrize("dtype", [object, "string"])
def test_alias_matches_equals_the_per_row_mask(dtype):
    series = pd.Series(["FacebookDQ365", "FacebookDQ365 EC", None, " FacebookDQ365  x",
                        "KLD", "", "FacebookDQ3650"], dtype=dtype, name="Account Alias")
    for alias in ("FacebookDQ365 EC Ait", "KLD", "", None, "nothing"):
        got = afs.alias_matches(series, alias)
        want = _legacy_alias_matches(series, alias)
        assert got.tolist() == [bool(v) for v in want.tolist()]
        assert got.index.equals(series.index)


def test_interest_bucket_keys_equal_the_per_row_map():
    values = pd.Series(["Trading", "trading", None, float("nan"), "HV|Trading", 1, "1", "", "AAY_Day"] * 3)
    assert section_filters.interest_bucket_keys(values).tolist() == _legacy_bucket_keys(values).tolist()


# ── 3. the event loop keeps answering while the page builds ──────────────

@pytest.mark.parametrize("path", ["/", "/details?section=account&country=Mexico&account=X"])
def test_light_requests_answer_while_a_slow_build_runs(monkeypatch, isolated, path):
    started = threading.Event()
    release = threading.Event()

    def slow_report(**_kw):
        started.set()
        release.wait(15)
        raise FileNotFoundError("synthetic: build finished")

    monkeypatch.setattr(web, "get_cached_report", slow_report)

    async def scenario():
        async with _asgi_client() as client:
            page = asyncio.create_task(client.get(path))
            assert await asyncio.to_thread(started.wait, 10), "build never started"
            t = time.perf_counter()
            health = await asyncio.wait_for(client.get("/health"), 5)
            slots = await asyncio.wait_for(client.get("/publish-jobs/slots"), 5)
            answered_in = time.perf_counter() - t
            still_building = not page.done()
            release.set()
            resp = await page
            return health.status_code, slots.status_code, answered_in, still_building, resp.status_code

    health, slots, answered_in, still_building, page_status = asyncio.run(scenario())
    assert (health, slots) == (200, 200)
    assert still_building, "the light requests had to wait for the build"
    assert answered_in < 5
    assert page_status == 200


# ── 4. cancelling a job never clears the server loop ─────────────────────

class _LoopClearingPublisher:
    """Does what RpaPublisher.cancel → BrowserConnection.close does to asyncio."""

    def __init__(self):
        self.cancelled = threading.Event()

    def cancel(self):
        from src.fb_ads_rpa.browser import _reset_asyncio_running_loop

        _reset_asyncio_running_loop()
        self.cancelled.set()


def _register_running_job(monkeypatch, publisher) -> str:
    job_id = "acl1380-job"
    monkeypatch.setitem(publish_jobs._jobs, job_id, {"status": "running", "results": [], "errors": []})
    monkeypatch.setitem(publish_jobs._active_publishers, job_id, publisher)
    return job_id


def _run_in_own_loop_thread(coro_factory, timeout=20):
    """Run a scenario in its own loop thread: a broken loop hangs, not the suite."""
    box = {}

    def runner():
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "the event loop stopped making progress"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_cancel_route_keeps_the_server_loop_running(monkeypatch, isolated):
    publisher = _LoopClearingPublisher()
    job_id = _register_running_job(monkeypatch, publisher)

    async def scenario():
        loop = asyncio.get_running_loop()
        async with _asgi_client() as client:
            r = await client.post(f"/publish-jobs/{job_id}/cancel")
            after = await client.get("/health")
        return r.status_code, after.status_code, asyncio._get_running_loop() is loop

    cancel_status, health_status, loop_intact = _run_in_own_loop_thread(scenario)
    assert cancel_status == 200
    assert health_status == 200
    assert loop_intact
    assert publisher.cancelled.wait(5)


def test_cancel_job_called_from_async_code_leaves_the_loop_alone(monkeypatch):
    publisher = _LoopClearingPublisher()
    job_id = _register_running_job(monkeypatch, publisher)

    async def scenario():
        loop = asyncio.get_running_loop()
        result = publish_jobs.cancel_job(job_id)   # directly on the loop thread
        await asyncio.sleep(0.05)
        return result["ok"], asyncio._get_running_loop() is loop

    ok, loop_intact = _run_in_own_loop_thread(scenario)
    assert ok and loop_intact
    assert publisher.cancelled.wait(5)


# ── 5. one build at a time; warm-up builds the default period too ───────

def test_concurrent_callers_share_one_build(monkeypatch, isolated, enriched_df):
    calls = []

    def load(**kw):
        calls.append(kw)
        time.sleep(0.5)
        return {"countries": [], "source_excel": str(isolated)}, enriched_df, []

    monkeypatch.setattr(cache, "load_morning_report", load)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get_cached_report(my_cm="Evgeni Sedin")))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(calls) == 1
    assert len(results) == 4 and all(r[0] is results[0][0] for r in results)


def test_warm_up_builds_report_and_default_period(monkeypatch, isolated, enriched_df, accounts_rows):
    built = []
    real_load = morning_service.load_morning_report

    def load(**kw):
        built.append(kw.get("enriched_df") is None)
        return real_load(**{**kw, "enriched_df": enriched_df, "account_rows_cached": accounts_rows})

    monkeypatch.setattr(cache, "load_morning_report", load)
    monkeypatch.setattr(web, "_load_ref_options", lambda: {})
    assert not cache.is_warm(my_cm="Evgeni Sedin")

    monkeypatch.setattr(report_warmup, "_state", dict(report_warmup._state, key=None, ok=None))
    assert report_warmup.warm_once(web._warm_default_report) is True
    assert cache.is_warm(my_cm="Evgeni Sedin")
    report, df, _ = cache.get_cached_report(my_cm="Evgeni Sedin")
    date_from, date_to = web.resolve_period(df, "", "", "")
    assert any(k[3:] == (date_from, date_to) for k in web._PERIOD_CACHE)
    assert len(built) == 1

    # Same workbook → nothing to do; a new mtime → build again.
    assert report_warmup.warm_once(web._warm_default_report) is False
    later = time.time() + 10
    os.utime(isolated, (later, later))
    assert report_warmup.warm_once(web._warm_default_report) is True
    assert len(built) == 2


def test_warm_up_failure_is_not_retried_every_tick(monkeypatch):
    monkeypatch.setattr(report_warmup, "_state", dict(report_warmup._state, key=None, ok=None))
    attempts = []

    def broken():
        attempts.append(1)
        raise ValueError("bad workbook")

    key = lambda: ("book", 1.0, "cm")  # noqa: E731
    assert report_warmup.warm_once(broken, key) is True
    assert report_warmup.warm_once(broken, key) is False
    assert len(attempts) == 1
    assert report_warmup.status()["ok"] is False


def test_background_work_only_starts_when_serve_asks(monkeypatch):
    started = []
    monkeypatch.setattr(report_warmup, "start", lambda build, **kw: started.append(build))
    monkeypatch.setattr(web, "refresh_team_targets_in_background", lambda: started.append("targets"))
    monkeypatch.delenv("ACL_DASHBOARD_BACKGROUND", raising=False)
    web._start_background_work()
    assert started == []
    monkeypatch.setenv("ACL_DASHBOARD_BACKGROUND", "1")
    web._start_background_work()
    assert started == ["targets", web._warm_default_report]


# ── 6. no network call on the render path ────────────────────────────────

def test_render_path_reads_targets_without_pulling(monkeypatch, isolated):
    from src.analysis_agent import targeting_registry as tr

    pulled = threading.Event()
    gate = threading.Event()

    def slow_pull(**_kw):
        pulled.set()
        gate.wait(10)
        return {}

    monkeypatch.setattr(tr, "pull", slow_pull)
    monkeypatch.setattr(web, "_targets_refresh_started", -1e9)
    monkeypatch.setattr(web, "_targets_refresh_running", False)

    # Tests / plain imports: no background thread, and still no pull.
    monkeypatch.delenv("ACL_DASHBOARD_BACKGROUND", raising=False)
    web._load_sheet_targets()
    assert not pulled.is_set()

    # Under serve.py: the pull runs in the background; the render does not wait.
    monkeypatch.setenv("ACL_DASHBOARD_BACKGROUND", "1")
    t = time.perf_counter()
    web._load_sheet_targets()
    assert time.perf_counter() - t < 2
    assert pulled.wait(5)
    # A second render while the pull is still running does not start another.
    assert web.refresh_team_targets_in_background() is False
    gate.set()
