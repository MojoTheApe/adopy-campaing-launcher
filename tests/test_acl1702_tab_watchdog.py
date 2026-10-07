# -*- coding: utf-8 -*-
"""Сторож вкладки на подставках: стек, порог, человек у браузера, чужие цели (ACL-1702).

Настоящий Chromium — в `test_acl1702_tab_unblock_real_chromium.py`. Здесь:

* поток, застрявший в вызове «Playwright» (кадр `_sync` приостановленного
  главного greenlet, как в sync Playwright), получает стек в
  `stuck_stacks/<run>_<ts>.txt` — с нашим местом вызова, шагом и адресом;
* разблокировка — только после порога, только раз на вызов, не во время
  перехвата человеком и ожидания обучения, и не с `ACL_STUCK_UNBLOCK=off`;
* к объектам Playwright сторож не обращается вовсе (подставка падает на
  любом вызове), соединение задания не закрывает;
* CDP: действует только на цель этой вкладки, по порядку terminate → reload →
  close; в редакторе reload пропускается; двух одинаковых вкладок — не
  трогает ни одну; браузер не закрывает;
* разблокированный вызов, бросивший ошибку, кончает кампанию красным
  `tab_hung:<шаг>` без повтора.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import greenlet
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import page_liveness  # noqa: E402
from src.fb_ads_rpa import pacing  # noqa: E402
from src.fb_ads_rpa import tab_watchdog as tw  # noqa: E402

URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&token=SECRET"


# ── Подставной sync Playwright: те же кадры, что у настоящего ────────────────

_SYNC_SRC = '''
def _sync(self, fiber):
    task = self._new_task()
    fiber.switch()          # как `self._dispatcher_fiber.switch()` в sync Playwright
    return task.value
'''
_ns: dict = {}
exec(compile(_SYNC_SRC, "site-packages/playwright/_impl/_sync_base.py", "exec"), _ns)


class _Task:
    def __init__(self):
        self._done = False
        self.value = 1

    def done(self):
        return self._done


class _MainFrame:
    def __init__(self, url):
        self._url = url


class Page:  # impl: «Page» с `_main_frame`, как playwright._impl._page.Page
    def __init__(self, url):
        self._main_frame = _MainFrame(url)


class _Strict:
    """Любое обращение, кроме полей `_impl_obj` и `__dict__`, — провал теста."""

    def __getattr__(self, name):
        raise AssertionError(f"the watchdog touched Playwright: {name}")


def _sync_api_page(impl):
    cls = type("Page", (_Strict,), {"__module__": "playwright.sync_api._generated"})
    obj = cls()
    vars(obj)["_impl_obj"] = impl
    from playwright._impl._impl_to_api_mapping import API_ATTR

    setattr(impl, API_ATTR, obj)
    return obj


_API_SRC = '''
def evaluate(self, fiber):
    return _sync(self, fiber)
'''
_api_ns: dict = {"_sync": _ns["_sync"]}
exec(compile(_API_SRC, "site-packages/playwright/sync_api/_generated.py", "exec"), _api_ns)


def _make_api_method():
    return _api_ns["evaluate"]


class FakeJob:
    """Поток задания, который «стоит» в page.evaluate, пока его не отпустят."""

    def __init__(self, url=URL):
        self.impl = Page(url)
        self.page = _sync_api_page(self.impl)
        self.task = _Task()
        self.release = threading.Event()
        self.ready = threading.Event()
        self.job: tw.JobThread | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error: BaseException | None = None
        self.hung_seen = ""

    def _run(self):
        self.job = tw.this_thread()
        main = greenlet.getcurrent()
        page_cls = type(self.page)
        page_cls._new_task = lambda _s: self.task
        page_cls.evaluate = _make_api_method()

        def dispatcher():
            self.ready.set()
            while not self.release.wait(0.02):
                pass
            self.task._done = True
            main.switch()

        fiber = greenlet.greenlet(dispatcher)
        try:
            self.my_step_function(fiber)
        except BaseException as exc:  # pragma: no cover - диагностика
            self.error = exc
        self.hung_seen = tw.take_hung()

    def my_step_function(self, fiber):
        page = self.page  # noqa: F841 — как в meta_dom: локальная `page`
        return type(self.page).evaluate(self.page, fiber)

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(5)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ACL_STUCK_UNBLOCK", raising=False)
    monkeypatch.delenv("ACL_STUCK_UNBLOCK_S", raising=False)
    pacing.release_takeover()
    pacing.end_teach_wait()
    yield
    pacing.release_takeover()
    pacing.end_teach_wait()
    with tw._lock:
        tw._HUNG.clear()
        tw._ENDPOINTS.clear()


# ── Стек ────────────────────────────────────────────────────────────────────

def test_the_stack_of_a_thread_stuck_in_a_call_names_the_call(tmp_path):
    with FakeJob() as fj:
        call = tw.blocked_call(fj.job)
        assert call is not None
        assert call.api == "Page.evaluate"
        assert call.page is fj.page and call.url == URL
        assert "my_step_function" in call.where
        path = tw.dump_stack(fj.job, run_id="0ab4aecb8e3b", step="ad_destination",
                             reason="step_stuck", idle_s=301.0, out_dir=tmp_path)
    assert path is not None and path.parent == tmp_path
    assert path.name.startswith("0ab4aecb8e3b_") and path.suffix == ".txt"
    text = path.read_text(encoding="utf-8")
    assert "last_step: ad_destination" in text
    assert "blocked_call: Page.evaluate" in text
    assert "my_step_function" in text, "стек — настоящий вызов, а не цикл диспетчера"
    assert "page_url: https://adsmanager.facebook.com/adsmanager/manage/campaigns" in text
    assert "SECRET" not in text, "запрос адреса (токены) в файл не пишем"
    assert "Playwright dispatcher" in text


def test_a_thread_running_python_has_no_blocked_call(tmp_path):
    stop = threading.Event()
    box: dict = {}

    def run():
        box["job"] = tw.this_thread()
        while not stop.wait(0.01):
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.1)
    try:
        assert tw.blocked_call(box["job"]) is None
        path = tw.dump_stack(box["job"], run_id="r1", step="x", reason="step_stuck", out_dir=tmp_path)
        assert "running Python" in path.read_text(encoding="utf-8")
    finally:
        stop.set()
        t.join(2)


def test_step_stuck_writes_the_stack_from_the_watcher(tmp_path, monkeypatch):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_ads_rpa import sentry_signal
    from src.web import publish_jobs as pj

    monkeypatch.setattr(tw, "stacks_dir", lambda: tmp_path)
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **k: "")
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw)))
    monkeypatch.setattr(rt, "last_activity", lambda job: (0.0, "ad_destination"))
    monkeypatch.setattr(rt, "last_run_id", lambda job: "0ab4aecb8e3b")
    monkeypatch.setattr(rt, "clock", lambda: 400.0)
    with FakeJob() as fj:
        with pj._jobs_lock:
            pj._jobs["j1702"] = {"status": "running", "campaign_names": ["B97"], "done_count": 0}
        try:
            state = {"fired": False, "since": 0.0, "job_thread": fj.job}
            assert pj._step_stuck_tick("j1702", state, "KLD") is True
        finally:
            with pj._jobs_lock:
                pj._jobs.pop("j1702", None)
    step, kw = rows[-1]
    assert step == "step_stuck"
    assert kw["run_id"] == "0ab4aecb8e3b"
    files = list(tmp_path.glob("0ab4aecb8e3b_*.txt"))
    assert len(files) == 1 and kw["diag"]["stack_file"] == files[0].name
    assert "my_step_function" in files[0].read_text(encoding="utf-8")


# ── Когда разблокировать ──────────────────────────────────────────────────

def _acts():
    calls: list = []

    def act(call):
        calls.append(call)
        call_done = {"ok": True, "method": "terminate", "tried": ["terminate:sent"]}
        return call_done

    return calls, act


def test_unblock_waits_for_the_threshold_then_acts_once_on_that_call():
    calls, act = _acts()
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job)
        assert tw.unblock_tick(watch, idle_s=10, step="ad_destination", now=0.0,
                               unblock_s=180, act=act) is None
        assert tw.unblock_tick(watch, idle_s=179, step="ad_destination", now=179.0,
                               unblock_s=180, act=act) is None
        assert calls == []
        got = tw.unblock_tick(watch, idle_s=181, step="ad_destination", now=181.0,
                              unblock_s=180, act=act)
        assert got and got["ok"] and got["method"] == "terminate" and got["marked"]
        assert len(calls) == 1 and calls[0].url == URL
        # Тот же вызов второй раз не трогаем.
        assert tw.unblock_tick(watch, idle_s=400, step="ad_destination", now=400.0,
                               unblock_s=180, act=act) is None
        assert len(calls) == 1
        # Вкладка для кампании «ушла» — стражи ACL-1673 остановят работу.
        assert page_liveness.tab_gone(fj.page) is True
    assert fj.hung_seen == "ad_destination", "поток кампании узнаёт шаг разблокировки"


def test_unblock_needs_the_same_call_for_the_whole_threshold():
    """Цикл коротких вызовов (каждый раз новый Task) порога не достигает."""
    calls, act = _acts()
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job)
        tw.unblock_tick(watch, idle_s=500, step="s", now=0.0, unblock_s=180, act=act)
        fj.task = _Task()   # «новый вызов»: подменяем Task в кадре
        frame, _ = tw.top_frame(fj.job)
        while frame.f_code.co_name != "_sync":
            frame = frame.f_back
        frame.f_locals["task"] = fj.task
        assert tw.unblock_tick(watch, idle_s=500, step="s", now=100.0, unblock_s=180, act=act) is None
        assert calls == []


@pytest.mark.parametrize("hold", ["takeover", "teach_wait"])
def test_never_unblocks_while_a_human_holds_the_browser(hold):
    calls, act = _acts()
    if hold == "takeover":
        pacing.request_takeover()
    else:
        pacing.begin_teach_wait("website_click")
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job)
        tw.unblock_tick(watch, idle_s=0, step="s", now=0.0, unblock_s=180, act=act)
        got = tw.unblock_tick(watch, idle_s=900, step="s", now=900.0, unblock_s=180, act=act)
        assert got == {"skipped": hold}
        assert calls == []
        assert page_liveness.tab_gone(fj.page) is False
    assert fj.hung_seen == ""


def test_off_switch_disables_the_unblocker(monkeypatch):
    monkeypatch.setenv("ACL_STUCK_UNBLOCK", "off")
    calls, act = _acts()
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job)
        tw.unblock_tick(watch, idle_s=0, step="s", now=0.0, unblock_s=180, act=act)
        assert tw.unblock_tick(watch, idle_s=900, step="s", now=900.0, unblock_s=180, act=act) is None
    assert calls == []


def test_nothing_sent_leaves_the_tab_unmarked():
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job)
        tw.unblock_tick(watch, idle_s=0, step="s", now=0.0, unblock_s=180,
                        act=lambda c: {"ok": False, "why": "no_endpoint", "tried": []})
        got = tw.unblock_tick(watch, idle_s=900, step="s", now=900.0, unblock_s=180,
                              act=lambda c: {"ok": False, "why": "no_endpoint", "tried": []})
        assert got["marked"] is False
        assert page_liveness.tab_gone(fj.page) is False
    assert fj.hung_seen == ""


def test_default_threshold_and_env(monkeypatch):
    assert tw.unblock_after_s() == 180.0
    monkeypatch.setenv("ACL_STUCK_UNBLOCK_S", "42")
    assert tw.unblock_after_s() == 42.0
    assert tw.enabled() is True
    for off in ("off", "OFF", "0", "false"):
        monkeypatch.setenv("ACL_STUCK_UNBLOCK", off)
        assert tw.enabled() is False


# ── CDP: только эта цель ──────────────────────────────────────────────────

class FakeCdp:
    def __init__(self, targets, *, works_on=None):
        self.targets = targets
        self.sent: list = []
        self.works_on = works_on
        self.closed = False
        self.flag = {"done": False}

    def call(self, method, params=None, *, session_id="", timeout=5.0):
        assert timeout <= 5.0, "у каждого вызова свой короткий срок"
        self.sent.append((method, dict(params or {}), session_id))
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-me"}
        if method == self.works_on:
            self.flag["done"] = True
        return {}

    def close(self):
        self.closed = True


def _targets(url=URL):
    return [
        {"type": "page", "targetId": "T-other", "url": "https://www.facebook.com/"},
        {"type": "page", "targetId": "T-me", "url": url},
        {"type": "service_worker", "targetId": "T-sw", "url": url},
        {"type": "browser", "targetId": "T-browser", "url": ""},
    ]


def _run(cdp, url=URL):
    return tw.unblock_tab("http://127.0.0.1:9222", url, lambda: cdp.flag["done"],
                          connect=lambda u, timeout: cdp, settle_s=0.2)


def _touched_targets(cdp):
    out = set()
    for method, params, session in cdp.sent:
        if "targetId" in params:
            out.add(params["targetId"])
        if session:
            assert session == "S-me"
    return out


def test_terminate_first_and_stop_when_it_works():
    cdp = FakeCdp(_targets(), works_on="Runtime.terminateExecution")
    got = _run(cdp)
    assert got["ok"] and got["method"] == "terminate"
    methods = [m for m, _p, _s in cdp.sent]
    assert methods[:3] == ["Target.getTargets", "Target.attachToTarget", "Runtime.terminateExecution"]
    assert "Page.reload" not in methods and "Target.closeTarget" not in methods
    assert methods[-1] == "Target.detachFromTarget"
    assert _touched_targets(cdp) == {"T-me"}
    assert cdp.closed


def test_escalates_to_reload_then_close_only_for_that_target():
    cdp = FakeCdp(_targets(), works_on=None)
    got = _run(cdp)
    methods = [m for m, _p, _s in cdp.sent]
    assert "Runtime.terminateExecution" in methods
    assert methods.index("Runtime.terminateExecution") < methods.index("Page.reload") \
        < methods.index("Target.closeTarget")
    assert ("Target.closeTarget", {"targetId": "T-me"}, "") in cdp.sent
    assert _touched_targets(cdp) == {"T-me"}, "чужие вкладки не трогаем"
    assert not any(m.startswith("Browser.close") for m in methods), "браузер не закрываем"
    assert got["ok"] is False and "close:no_effect" in got["tried"]


def test_close_ends_it_when_terminate_does_not():
    cdp = FakeCdp(_targets(), works_on="Target.closeTarget")
    got = _run(cdp)
    assert got["ok"] and got["method"] == "close"


def test_the_ad_editor_is_never_reloaded():
    editor = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit?act=1&selected_ad_ids=2"
    cdp = FakeCdp(_targets(editor), works_on="Target.closeTarget")
    got = _run(cdp, editor)
    methods = [m for m, _p, _s in cdp.sent]
    assert "Page.reload" not in methods and "reload:skipped_editor" in got["tried"]
    assert got["method"] == "close"


def test_two_tabs_with_the_same_address_are_left_alone():
    targets = _targets() + [{"type": "page", "targetId": "T-twin", "url": URL}]
    cdp = FakeCdp(targets, works_on="Target.closeTarget")
    got = _run(cdp)
    assert got["why"] == "ambiguous_target" and not got["ok"]
    assert [m for m, _p, _s in cdp.sent] == ["Target.getTargets"]


def test_no_such_tab_sends_nothing():
    cdp = FakeCdp([t for t in _targets() if t["targetId"] != "T-me"])
    got = _run(cdp)
    assert got["why"] == "no_target"
    assert [m for m, _p, _s in cdp.sent] == ["Target.getTargets"]


def test_the_watchdog_never_uses_the_jobs_playwright_objects():
    """Сторож ходит в браузер только своим соединением (ACL-1614: не рвать чужое)."""
    src = (ROOT / "src/fb_ads_rpa/tab_watchdog.py").read_text(encoding="utf-8")
    for banned in ("new_browser_cdp_session", "new_cdp_session", ".disconnect(", "browser.close",
                   "page.close", "playwright.stop", "_stop_thread_playwright"):
        assert banned not in src, banned


# ── Итог кампании: tab_hung без повтора ──────────────────────────────────────

def test_the_unblocked_call_raising_ends_the_campaign_red_without_retry(monkeypatch):
    from src.fb_ads_rpa import ads_manager_flow as amf
    from src.fb_ads_rpa import browser, failure_policy, retry_policy, sentry_signal
    from src.fb_ads_rpa import config as rpa_config
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    attempts: list = []
    page = type("P", (), {})()

    def fake_flow(pg, *, campaign_name, soft_fails_out, **_kw):
        attempts.append(1)
        # Сторож (в своём потоке) пометил вкладку, вызов вернулся ошибкой драйвера.
        tw._mark(tw.this_thread(), page, "ad2_destination")
        assert page_liveness.tab_gone(page) is True
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(amf, "run_recorded_campaign_flow", fake_flow)
    monkeypatch.setattr(rpa_config, "get_use_recorded_flow", lambda: True)
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, d, r: None)
    monkeypatch.setattr(amf.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(browser, "put_window_back",
                        lambda pg: (_ for _ in ()).throw(AssertionError("window of a hung tab")))
    monkeypatch.setattr(sentry_signal, "report_campaign_stop", lambda *a, **k: "")
    monkeypatch.setattr(sentry_signal, "report_blocking_failures", lambda *a, **k: [])
    from src.fb_ads_rpa import run_summary

    monkeypatch.setattr(run_summary, "report_finished", lambda **kw: "")
    flow = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    flow.page = page
    draft = CampaignDraft(name="B97", country="SA", account_label="KLD",
                          ad_sets=[AdSetDraft(name="adset", daily_budget=35.0, ads=[
                              AdDraft(creative_type="Image", creative_path="a.png",
                                      ad_title="t", ad_body="b")])])
    result = flow.publish_campaign(draft)
    assert attempts == [1], "без повтора кампании"
    assert result.success is False
    assert "tab_hung:ad2_destination" in result.blocking_failures
    assert "tab_hung:ad2_destination" in result.error
    assert tw.peek_hung() == "" and page_liveness.tab_gone(page) is False, "отметка снята"
    assert retry_policy.disposition("tab_hung").action == retry_policy.BLOCK_AND_REVIEW
    assert failure_policy.severity("tab_hung:ad2_destination") == failure_policy.BLOCKING
    assert "unclassified" not in failure_policy.reason("tab_hung:ad2_destination")


def test_a_plain_error_without_an_unblock_still_raises(monkeypatch):
    from src.fb_ads_rpa import ads_manager_flow as amf
    from src.fb_ads_rpa import browser
    from src.fb_ads_rpa import config as rpa_config
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    def fake_flow(pg, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(amf, "run_recorded_campaign_flow", fake_flow)
    monkeypatch.setattr(rpa_config, "get_use_recorded_flow", lambda: True)
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, d, r: None)
    monkeypatch.setattr(browser, "put_window_back", lambda pg: None)
    flow = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    flow.page = None
    draft = CampaignDraft(name="B97", country="SA", account_label="KLD",
                          ad_sets=[AdSetDraft(name="adset", daily_budget=35.0, ads=[
                              AdDraft(creative_type="Image", creative_path="a.png",
                                      ad_title="t", ad_body="b")])])
    with pytest.raises(RuntimeError, match="boom"):
        flow.publish_campaign(draft)
