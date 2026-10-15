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
        tw.forget_thread(self.job.tid)

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


def _clear_registries():
    with tw._lock:
        tw._HUNG.clear()
        tw._ENDPOINTS.clear()
        tw._JOB_TIDS.clear()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Ревью PR #704 (C2): id потоков ОС раздаёт заново (Linux — сразу), и
    # запись одного теста не должна решать за следующий.
    monkeypatch.delenv("ACL_STUCK_UNBLOCK", raising=False)
    monkeypatch.delenv("ACL_STUCK_UNBLOCK_S", raising=False)
    pacing.release_takeover()
    pacing.end_teach_wait()
    _clear_registries()
    yield
    pacing.release_takeover()
    pacing.end_teach_wait()
    _clear_registries()


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
        tw.forget_thread(box["job"].tid)

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


# Шаги живых зависаний: B97 (ad_destination), Doc120 (next_to_adset),
# Sentry PYTHON-6C (campaigns_list_ready), PYTHON-6D (advantage_plus_off).
@pytest.mark.parametrize("last_step", ["ad_destination", "next_to_adset",
                                       "campaigns_list_ready", "advantage_plus_off"])
def test_step_stuck_writes_the_stack_from_the_watcher(tmp_path, monkeypatch, last_step):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_ads_rpa import sentry_signal
    from src.web import publish_jobs as pj

    monkeypatch.setattr(tw, "stacks_dir", lambda: tmp_path)
    monkeypatch.setattr(sentry_signal, "report_step_stuck", lambda *a, **k: "")
    rows: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw)))
    monkeypatch.setattr(rt, "last_activity", lambda job: (0.0, last_step))
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
    assert kw["diag"]["last_step"] == last_step
    files = list(tmp_path.glob("0ab4aecb8e3b_*.txt"))
    assert len(files) == 1 and kw["diag"]["stack_file"] == files[0].name
    text = files[0].read_text(encoding="utf-8")
    assert f"last_step: {last_step}" in text
    assert "blocked_call: Page.evaluate at" in text and "my_step_function" in text


def test_a_budget_stop_writes_the_stack_where_it_fired(tmp_path, monkeypatch):
    """Doc124 e3f55897e533 (PYTHON-6E): campaign_budget_exceeded после next_to_ad.

    Срок срабатывает в самом потоке задания; стек — до места срабатывания
    (какой цикл съел время), файл — в diag строки остановки.
    """
    from src.fb_ads_rpa import meta_dom as md
    from src.fb_ads_rpa import run_telemetry as rt

    monkeypatch.setattr(tw, "stacks_dir", lambda: tmp_path)
    rows: list = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(rt, "current_run_id", lambda: "e3f55897e533")
    detail = {"after_step": "next_to_ad", "campaign_s": 2768.9, "step_s": 170.2,
              "frozen_episodes": 3, "budget_s": 2700.0}

    def the_wait_loop_that_ate_the_time():
        raise md.FlowBudgetExceeded("campaign_budget_exceeded", detail)

    @md._under_flow_budget
    def flow(page):
        the_wait_loop_that_ate_the_time()

    page = _sync_api_page(Page("https://adsmanager.facebook.com/adsmanager/manage/ads/edit?act=1"))
    assert flow(page) == "campaign_budget_exceeded"
    step, ok, kw = rows[-1]
    assert step == "campaign_budget_exceeded" and ok is False
    files = list(tmp_path.glob("e3f55897e533_*.txt"))
    assert len(files) == 1 and kw["diag"]["stack_file"] == files[0].name
    assert kw["diag"]["after_step"] == "next_to_ad"
    text = files[0].read_text(encoding="utf-8")
    assert "reason: campaign_budget_exceeded" in text and "last_step: next_to_ad" in text
    assert "the_wait_loop_that_ate_the_time" in text, "стек называет место срабатывания"
    assert "page_url: https://adsmanager.facebook.com/adsmanager/manage/ads/edit" in text


# ── Когда разблокировать ──────────────────────────────────────────────────

def _acts():
    calls: list = []

    def act(call):
        calls.append(call)
        return {"ok": True, "method": "close", "tried": ["close:sent"]}

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
        assert got and got["ok"] and got["method"] == "close" and got["marked"]
        assert len(calls) == 1 and calls[0].url == URL
        # Тот же вызов второй раз не трогаем.
        assert tw.unblock_tick(watch, idle_s=400, step="ad_destination", now=400.0,
                               unblock_s=180, act=act) is None
        assert len(calls) == 1
        # Вкладка для кампании «ушла» — стражи ACL-1673 остановят работу.
        assert page_liveness.tab_gone(fj.page) is True
    assert fj.hung_seen == "ad_destination", "поток кампании узнаёт шаг разблокировки"


def test_unblock_needs_the_job_silent_for_the_threshold():
    """Ревью PR #704 (m8): тот же вызов давно, но строка телеметрии свежая — не трогать."""
    calls, act = _acts()
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job)
        tw.unblock_tick(watch, idle_s=0, step="s", now=0.0, unblock_s=180, act=act)
        assert tw.unblock_tick(watch, idle_s=179, step="s", now=900.0, unblock_s=180, act=act) is None
        assert tw.unblock_tick(watch, idle_s=2, step="s", now=950.0, unblock_s=180, act=act) is None
        assert calls == []


def test_unblock_stops_after_three_per_job():
    """Ревью PR #704 (m8): не больше `MAX_PER_JOB` разблокировок на задание."""
    assert tw.MAX_PER_JOB == 3
    calls, act = _acts()
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job, count=tw.MAX_PER_JOB - 1)
        tw.unblock_tick(watch, idle_s=0, step="s", now=0.0, unblock_s=180, act=act)
        assert tw.unblock_tick(watch, idle_s=900, step="s", now=900.0, unblock_s=180, act=act)
        assert watch.count == tw.MAX_PER_JOB and len(calls) == 1
        tw.take_hung()
    with FakeJob() as fj:
        watch = tw.Watch(job=fj.job, count=tw.MAX_PER_JOB)
        tw.unblock_tick(watch, idle_s=0, step="s", now=0.0, unblock_s=180, act=act)
        assert tw.unblock_tick(watch, idle_s=900, step="s", now=900.0, unblock_s=180, act=act) is None
    assert len(calls) == 1


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
    assert tw.unblock_after_s() == 240.0, "выше 180 с таймаута загрузки файла"
    monkeypatch.setenv("ACL_STUCK_UNBLOCK_S", "42")
    assert tw.unblock_after_s() == 42.0
    assert tw.enabled() is True
    for off in ("off", "OFF", "0", "false"):
        monkeypatch.setenv("ACL_STUCK_UNBLOCK", off)
        assert tw.enabled() is False


# ── Адрес CDP: только за потоком задания ─────────────────────────────────

def test_the_endpoint_is_kept_only_for_job_threads_and_forgotten_on_reset():
    box: dict = {}

    def other():
        tw.note_endpoint("http://127.0.0.1:1111")
        box["tid"] = threading.get_ident()

    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    assert tw.endpoint_of(box["tid"]) == "", "не поток задания — не записываем"

    def job():
        me = tw.this_thread()
        tw.note_endpoint("http://127.0.0.1:2222")
        box["kept"] = tw.endpoint_of(me.tid)
        tw.forget_endpoint()
        box["after_reset"] = tw.endpoint_of(me.tid)
        tw.forget_thread(me.tid)

    t = threading.Thread(target=job)
    t.start()
    t.join(5)
    assert box["kept"] == "http://127.0.0.1:2222" and box["after_reset"] == ""


# ── CDP: только закрытие, только эта цель ───────────────────────────────

class FakeCdp:
    def __init__(self, targets, *, works_on="Target.closeTarget", refuse=()):
        self.targets = targets
        self.sent: list = []
        self.works_on = works_on
        self.refuse = refuse
        self.closed = False
        self.flag = {"done": False}

    def call(self, method, params=None, *, session_id="", timeout=5.0):
        assert timeout <= 5.0, "у каждого вызова свой короткий срок"
        self.sent.append((method, dict(params or {}), session_id))
        if method in self.refuse:
            raise tw.CdpError(f"{method}: refused")
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == self.works_on:
            self.flag["done"] = True
        return {}

    def close(self):
        self.closed = True


def _targets(url=URL):
    return [
        {"type": "page", "targetId": "T-other", "url": "https://www.facebook.com/",
         "browserContextId": "C1"},
        {"type": "page", "targetId": "T-me", "url": url, "browserContextId": "C1"},
        {"type": "service_worker", "targetId": "T-sw", "url": url},
        {"type": "browser", "targetId": "T-browser", "url": ""},
    ]


def _run(cdp, url=URL):
    return tw.unblock_tab("http://127.0.0.1:9222", url, lambda: cdp.flag["done"],
                          connect=lambda u, timeout: cdp, settle_s=0.2)


def _methods(cdp):
    return [m for m, _p, _s in cdp.sent]


# Ревью PR #704 (B1): одноэкранный редактор без `/edit` в адресе, список
# кампаний (reload выбросил бы начатое создание, ACL-1313) и редактор — все
# только закрываются.
@pytest.mark.parametrize("url", [
    "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1&campaign_id=2",
    "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1",
    "https://adsmanager.facebook.com/adsmanager/manage/ads/edit?act=1&selected_ad_ids=2",
])
def test_the_hung_tab_is_closed_never_reloaded_or_terminated(url):
    cdp = FakeCdp(_targets(url))
    got = _run(cdp, url)
    assert got["ok"] and got["method"] == "close"
    assert _methods(cdp) == ["Target.getTargets", "Target.closeTarget"]
    assert ("Target.closeTarget", {"targetId": "T-me"}, "") in cdp.sent
    for banned in ("Page.reload", "Runtime.terminateExecution", "Target.attachToTarget",
                   "Browser.close"):
        assert banned not in _methods(cdp)
    assert cdp.closed


def test_the_last_tab_gets_a_spare_before_it_is_closed():
    """Последняя вкладка: Chrome закрылся бы целиком (и профиль) — сперва запасная."""
    only = [t for t in _targets() if t["targetId"] != "T-other"]
    cdp = FakeCdp(only)
    got = _run(cdp)
    assert got["ok"] and got["method"] == "close"
    assert _methods(cdp) == ["Target.getTargets", "Target.createTarget", "Target.closeTarget"]
    create = cdp.sent[1][1]
    assert create["url"] == "about:blank" and create["background"] is True
    assert create["browserContextId"] == "C1"


def test_without_a_spare_tab_the_last_tab_is_not_closed():
    only = [t for t in _targets() if t["targetId"] != "T-other"]
    cdp = FakeCdp(only, refuse=("Target.createTarget",))
    got = _run(cdp)
    assert got["why"] == "last_tab" and not got["ok"]
    assert "Target.closeTarget" not in _methods(cdp)


def test_close_without_effect_is_reported():
    cdp = FakeCdp(_targets(), works_on=None)
    got = _run(cdp)
    assert got["ok"] is False and "close:no_effect" in got["tried"]


def test_two_tabs_with_the_same_address_are_left_alone():
    targets = _targets() + [{"type": "page", "targetId": "T-twin", "url": URL}]
    cdp = FakeCdp(targets)
    got = _run(cdp)
    assert got["why"] == "ambiguous_target" and not got["ok"]
    assert _methods(cdp) == ["Target.getTargets"]


def test_no_such_tab_sends_nothing():
    cdp = FakeCdp([t for t in _targets() if t["targetId"] != "T-me"])
    got = _run(cdp)
    assert got["why"] == "no_target"
    assert _methods(cdp) == ["Target.getTargets"]


def test_the_watchdog_never_uses_the_jobs_playwright_objects():
    """Сторож ходит в браузер только своим соединением (ACL-1614: не рвать чужое)."""
    src = (ROOT / "src/fb_ads_rpa/tab_watchdog.py").read_text(encoding="utf-8")
    for banned in ("new_browser_cdp_session", "new_cdp_session", ".disconnect(", "browser.close",
                   "page.close", "playwright.stop", "_stop_thread_playwright",
                   '"Page.reload"', '"Runtime.terminateExecution"'):
        assert banned not in src, banned


# ── Стеки: чистка текста и предел числа файлов ──────────────────────────

def test_dump_exception_strips_queries_and_ids(tmp_path):
    exc = RuntimeError("Page.goto: net::ERR at https://adsmanager.facebook.com/adsmanager/manage/"
                       "campaigns?act=1234567890123456&business_id=987654321012 (ad account 1234567890123456)")
    path = tw.dump_exception(exc, run_id="r1", step="campaigns_list_ready", reason="step_budget_exceeded",
                             detail={"url": "https://x.test/p?token=SECRET", "ad": 120211234567890},
                             out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "https://adsmanager.facebook.com/adsmanager/manage/campaigns" in text
    for leak in ("1234567890123456", "987654321012", "120211234567890", "SECRET", "act="):
        assert leak not in text, leak


def test_stack_files_are_capped(tmp_path):
    import os as _os

    old = tmp_path / "old_run.txt"
    old.write_text("x")
    long_ago = time.time() - (tw.STACKS_KEEP_DAYS + 1) * 86400
    _os.utime(old, (long_ago, long_ago))
    for i in range(tw.STACKS_KEEP + 5):
        f = tmp_path / f"r{i:04d}.txt"
        f.write_text("x")
        _os.utime(f, (time.time() - 1000 + i, time.time() - 1000 + i))
    tw._prune(tmp_path)
    left = sorted(p.name for p in tmp_path.glob("*.txt"))
    assert len(left) == tw.STACKS_KEEP and "old_run.txt" not in left
    assert "r0000.txt" not in left and f"r{tw.STACKS_KEEP + 4:04d}.txt" in left


# ── Жёсткая остановка после разблокировки (ревью PR #704, B2) ───────────

def test_a_helper_that_swallows_the_error_does_not_act_again(monkeypatch):
    from src.fb_ads_rpa import meta_dom as md

    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(pacing.time, "sleep", lambda *_a: None)
    actions: list = []

    def helper(page):
        try:
            raise RuntimeError("Target page, context or browser has been closed")
        except Exception:
            pass            # мягкий отказ: так делают десятки помощников
        pacing.pause_short(page)
        actions.append("click")

    @md._under_flow_budget
    def flow(page):
        md._flow_budget_arm()
        helper(page)
        actions.append("fill")
        return ""

    tw._mark(tw.this_thread(), None, "ad_destination")
    try:
        assert flow(None) == "tab_hung"
    finally:
        tw.take_hung()
    assert actions == [], "после разблокировки — ни одного действия"


def test_sleep_h_stops_too_and_nothing_happens_outside_a_campaign(monkeypatch):
    from src.fb_ads_rpa import meta_dom as md

    monkeypatch.setattr(md.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(pacing.time, "sleep", lambda *_a: None)
    tw._mark(tw.this_thread(), None, "s")
    try:
        md._flow_budget_disarm()
        pacing.pause_short(None)        # вне потока кампании — не бросает
        md._sleep_h(0.01)
        md._flow_budget_arm()
        with pytest.raises(md.FlowBudgetExceeded) as stop:
            md._sleep_h(0.01)
        assert stop.value.reason == "tab_hung" and stop.value.detail["after_step"] == "s"
    finally:
        md._flow_budget_disarm()
        tw.take_hung()


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


# ── Зависшая вкладка не отравляет остаток задания (ревью PR #704, B3) ──────

class _Draft:
    def __init__(self, name):
        self.status = "DRAFT"
        self.errors = []
        self.name = name
        self.account_label = "Doc19"


def _publisher():
    from unittest.mock import MagicMock

    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    p = RpaPublisher.__new__(RpaPublisher)
    p._conn = None
    p._entry = None
    p._provider = MagicMock()
    p.hand_off = False
    p.dry_run = False
    p.selectors_path = None
    return p


def test_a_hang_on_the_campaigns_list_lets_the_next_campaign_reattach_and_run(monkeypatch):
    """PYTHON-6C: зависание в `_goto_campaigns`/`_ensure_campaigns_ready` — вне цикла
    попыток. Кампания A — `tab_hung`; кампания B того же задания подключается
    заново и идёт, без `browser_closed`."""
    from unittest.mock import MagicMock

    from src.fb_ads_rpa import rpa_publisher as rp
    from src.fb_ads_rpa.draft import RpaPublishResult

    p = _publisher()
    conns: list = []

    def ensure(label):
        if p._conn is None:
            page = type("P", (), {"is_closed": lambda self: False})()
            p._conn = MagicMock(provider="multilogin", page=page)
            conns.append(p._conn)
        return p._conn

    seen_pages: list = []

    def publish_campaign(draft, dry_run=False):
        page = p._conn.page
        seen_pages.append(page)
        if draft.name == "A":
            tw._mark(tw.this_thread(), page, "campaigns_list_ready")
            raise RuntimeError("Page.goto: Target page, context or browser has been closed")
        assert not page_liveness.tab_gone(page), "следующая кампания — на живой вкладке"
        return RpaPublishResult(campaign_name=draft.name, success=True)

    monkeypatch.setattr(p, "_ensure_browser", ensure)
    monkeypatch.setattr(p, "_flow", lambda conn, label: MagicMock(publish_campaign=publish_campaign))
    a = p.publish_draft(_Draft("A"))
    assert a.success is False and a.blocking_failures == ["tab_hung:campaigns_list_ready"]
    assert p._conn is None, "соединение сброшено — следующая подключится заново"
    assert not page_liveness.tab_gone(seen_pages[0]), "метка снята"
    conns[0].close.assert_called_once_with(keep_browser_open=True)
    b = p.publish_draft(_Draft("B"))
    assert b.success is True and not b.blocking_failures
    assert len(conns) == 2 and seen_pages[1] is not seen_pages[0]
    assert tw.peek_hung() == ""


def test_an_unblock_outside_a_campaign_does_not_brand_the_next_one(monkeypatch):
    from unittest.mock import MagicMock

    from src.fb_ads_rpa.draft import RpaPublishResult

    p = _publisher()
    conn = MagicMock(provider="multilogin")
    monkeypatch.setattr(p, "_ensure_browser", lambda label: conn)
    flags: list = []

    def publish_campaign(draft, dry_run=False):
        flags.append(tw.peek_hung())
        return RpaPublishResult(campaign_name=draft.name, success=True)

    monkeypatch.setattr(p, "_flow", lambda c, label: MagicMock(publish_campaign=publish_campaign))
    p._conn = conn
    # Сторож сработал после конца прошлой кампании (уборка окна и т. п.).
    tw._mark(tw.this_thread(), None, "put_window_back")
    got = p.publish_draft(_Draft("B"))
    assert flags == [""], "отметка снята в начале кампании"
    assert got.success is True and not got.blocking_failures


def test_a_prerender_target_does_not_count_as_another_tab():
    """Prerender (`subtype`) — не вкладка: последняя настоящая получает запасную."""
    only = [t for t in _targets() if t["targetId"] != "T-other"]
    only.append({"type": "page", "subtype": "prerender", "targetId": "T-pre",
                 "url": "https://www.facebook.com/", "browserContextId": "C1"})
    cdp = FakeCdp(only)
    got = _run(cdp)
    assert got["ok"] and _methods(cdp) == ["Target.getTargets", "Target.createTarget",
                                           "Target.closeTarget"]
    assert ("Target.closeTarget", {"targetId": "T-me"}, "") in cdp.sent
