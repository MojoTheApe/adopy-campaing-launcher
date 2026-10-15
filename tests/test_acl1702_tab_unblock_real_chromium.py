# -*- coding: utf-8 -*-
"""Сторож разблокирует зависшую вкладку настоящего Chromium (ACL-1702).

Chromium запускается с `--remote-debugging-port`, поток задания подключается к
нему по CDP (как к профилю Multilogin) и зависает в `page.evaluate` с
`while (true) {}`. Сторож из ДРУГОГО потока, своим websocket-соединением к
тому же порту, разблокирует только эту вкладку. Проверяем:

* заблокированный sync-вызов бросает ошибку в пределах порога плюс запас;
* поток задания не крутится: за время ожидания и после ошибки он почти не
  тратит процессор (`psutil`, время потока), соседний поток на чистом Python
  получает GIL (скорость счётчика — не ниже доли исходной);
* вторая вкладка того же браузера жива и отвечает, браузер не закрыт;
* после ошибки страж «вкладки нет» (ACL-1673) видит её ушедшей.

Способ один — `Target.closeTarget` (ревью PR #704): список кампаний,
одноэкранный редактор `simple_create` и редактор объявления закрываются, не
перезагружаются. Последняя вкладка браузера закрывается только после
запасной пустой — браузер (а с ним профиль Multilogin) остаётся жив.
Замер 07.10 (Chromium 1228), до решения: `Runtime.terminateExecution` на такой
вкладке не отвечал за 5 с и скрипт не прерывал.

Тяжёлый тест: настоящий браузер. Гонять по одному файлу и только при
свободной памяти > 6 ГБ; Chromium убивается в конце (`taskkill /T`).
"""
from __future__ import annotations

import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

import psutil
import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.windows_only(reason="stops a real Chromium process tree with taskkill /T")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import page_liveness  # noqa: E402
from src.fb_ads_rpa import tab_watchdog as tw  # noqa: E402
from tests.test_a_stopped_profile_never_spins_the_robot import (  # noqa: E402
    _Counter, _start_chromium)

#: Порог разблокировки в тесте (в работе — 180 с).
UNBLOCK_S = 3.0
#: Запас сверх порога: опрос сторожа + сами способы (каждый ≤ SETTLE_S).
MARGIN_S = 3 * tw.SETTLE_S + 10.0
#: Поток задания за секунду ожидания тратит процессора не больше этой доли.
MAX_CPU_SHARE = 0.25


class _Pages(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = f"<html><body><p id=p>{self.path}</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _kill_tree(proc) -> str:
    """Весь Chromium теста с детьми; вернуть, что осталось живым ("" — ничего).

    Сначала `taskkill /T /F`; кто пережил его (замер 07.10: процесс браузера
    оставался после taskkill, пока его вкладка висела в `while (true)`), —
    добиваем через psutil по списку детей, снятому ДО taskkill.
    """
    try:
        family = [proc.pid] + [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
    except psutil.Error:
        family = [proc.pid]
    out = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        pass
    # Замер 07.10: на нагруженной машине убитый Chromium уходил до пары минут.
    procs = []
    for pid in family:
        try:
            p = psutil.Process(pid)
            p.kill()
            procs.append(p)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            procs.append(None)
    _gone, still = psutil.wait_procs([p for p in procs if p is not None], timeout=180)
    alive = [f"{p.pid}:alive" for p in still]
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        alive.append(f"{proc.pid}:launcher rc={out.returncode} "
                     f"{out.stdout.decode('cp866', 'replace')[-200:]!r}")
    return ", ".join(alive)


@pytest.fixture
def chromium():
    with sync_playwright() as pw:
        exe = os.environ.get("ACL_BROWSER_EXECUTABLE") or pw.chromium.executable_path
    proc, port = _start_chromium(exe)
    try:
        yield proc, port
    finally:
        left = _kill_tree(proc)
        if left:
            # Замер 07.10: taskkill отвечал SUCCESS, а процесс уходил минутами
            # (завершение ждёт ОС). Убит он уже; провал теста тут — шум.
            warnings.warn(f"Chromium is still exiting after kill: {left}")


def _thread_cpu(native_id: int) -> float:
    for t in psutil.Process().threads():
        if t.id == native_id:
            return t.user_time + t.system_time
    return 0.0


def _others_cpu(skip_native_id: int) -> float:
    """Процессор всех потоков процесса, кроме счётчика.

    GIL держит только поток, который работает: если никто, кроме счётчика, не
    тратит процессор, GIL никто не занимает. Скорость счётчика сама по себе
    шумит от нагрузки машины (07.10: 0.2x при нулевом процессоре задания).
    """
    return sum(t.user_time + t.system_time for t in psutil.Process().threads()
               if t.id != skip_native_id)


class HungJob:
    """Поток задания: свой sync Playwright, подключение по CDP, зависший evaluate."""

    def __init__(self, port: int, url: str, other_url: str, *, only: bool = False):
        self.only = only
        self.cdp_url = f"http://127.0.0.1:{port}"
        self.url = url
        self.other_url = other_url
        self.job: tw.JobThread | None = None
        self.native_id = 0
        self.blocking = threading.Event()
        self.raised = threading.Event()
        self.finish = threading.Event()
        self.error: BaseException | None = None
        self.t_block = 0.0
        self.t_raise = 0.0
        self.gone_after = None
        self.other_answers = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="acl1702-job")

    def _run(self):
        self.job = tw.this_thread()
        self.native_id = threading.get_native_id()
        with sync_playwright() as pw:
            browser = None
            deadline = time.time() + 20
            while browser is None:
                try:
                    browser = pw.chromium.connect_over_cdp(self.cdp_url)
                except Exception:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.2)
            tw.note_endpoint(self.cdp_url)
            ctx = browser.contexts[0]
            other = None
            if not self.only:
                other = ctx.new_page()
                other.goto(self.other_url)
            page = ctx.new_page()
            page.goto(self.url)
            if self.only:
                # Зависшая — единственная вкладка браузера.
                for extra in list(ctx.pages):
                    if extra is not page:
                        extra.close()
            self.t_block = time.monotonic()
            self.blocking.set()
            try:
                page.evaluate("() => { while (true) {} }")
            except Exception as exc:
                self.error = exc
            self.t_raise = time.monotonic()
            self.raised.set()
            # Страж «вкладки нет» (ACL-1673) — локальный, без вызова страницы.
            self.gone_after = page_liveness.tab_gone(page)
            self.hung = tw.take_hung()
            # Вторая вкладка того же браузера жива и отвечает.
            if other is not None:
                try:
                    self.other_answers = other.evaluate("() => document.getElementById('p').textContent")
                except Exception as exc:  # pragma: no cover - провал теста ниже
                    self.other_answers = exc
            self.finish.wait(60)
            browser.close()   # отключение — из СВОЕГО потока, как в работе
        tw.forget_thread(self.job.tid)


def _run_case(port: int, site: str, path: str, *, only: bool = False):
    """Поток задания зависает; сторож из этого потока разблокирует его вкладку."""
    url = site + path
    job = HungJob(port, url, site + "/other", only=only)
    job.thread.start()
    assert job.blocking.wait(60), "the job did not reach the hanging call"
    time.sleep(0.5)
    out: dict = {}
    with _Counter() as counter:
        base = counter.baseline(0.5)
        cpu0 = _thread_cpu(job.native_id)
        counter_id = counter._t.native_id
        others0 = _others_cpu(counter_id)
        watch = tw.Watch(job=job.job)
        t0 = time.monotonic()
        result = None
        # Сторож: круги опроса, пока не сработает (как в publish_jobs).
        while result is None and time.monotonic() - t0 < UNBLOCK_S + 30:
            now = time.monotonic()
            result = tw.unblock_tick(watch, idle_s=now - job.t_block, step="ad_destination",
                                     now=now, unblock_s=UNBLOCK_S)
            if result is None:
                time.sleep(0.25)
        n_before = counter.n
        t_mid = time.perf_counter()
        assert job.raised.wait(MARGIN_S), f"the blocked call did not return: {result}"
        # После ошибки поток задания не должен крутиться: ещё 2 с замера.
        time.sleep(2.0)
        rate = (counter.n - n_before) / max(time.perf_counter() - t_mid, 1e-6)
        cpu = _thread_cpu(job.native_id) - cpu0
        others = _others_cpu(counter_id) - others0
        wall = time.monotonic() - t0
    out.update(result=result, base=base, rate=rate, cpu=cpu, others=others, wall=wall,
               blocked_s=job.t_raise - job.t_block)
    job.finish.set()
    job.thread.join(30)
    print(f"\n[ACL-1702] url={url} method={result.get('method')} tried={result.get('tried')} "
          f"blocked_s={out['blocked_s']:.2f} unblock_seconds={result.get('seconds')} "
          f"settle_s={result.get('settle_s')} job_cpu_s={cpu:.3f} others_cpu_s={others:.3f} "
          f"over {wall:.1f}s "
          f"neighbour_rate={rate / max(base, 1):.2f}x error={type(job.error).__name__}: "
          f"{str(job.error)[:120]!r}")
    return job, out


def _common_asserts(job, out, *, only: bool = False):
    result = out["result"]
    assert result["ok"] is True, result
    assert result["method"] == "close", result
    assert job.error is not None, "the blocked evaluate must raise"
    assert out["blocked_s"] <= UNBLOCK_S + MARGIN_S, out
    assert out["cpu"] <= MAX_CPU_SHARE * out["wall"], f"job thread spun: {out}"
    # GIL: кроме счётчика, Python-потоки процесса (задание, сторож, сервер
    # страниц) почти не работают — держать GIL некому. Скорость счётчика — в
    # отчёт строкой выше, без проверки: её роняет нагрузка всей машины.
    assert out["others"] <= MAX_CPU_SHARE * out["wall"], f"GIL busy: {out}"
    assert job.gone_after is True, "the dead-page guard must see the tab as gone"
    assert job.hung == "ad_destination"
    if not only:
        assert job.other_answers == "/other", f"the other tab must be untouched: {job.other_answers!r}"


def _browser_pages(port: int) -> list:
    cdp = tw.RawCdp.connect(f"http://127.0.0.1:{port}")
    try:
        infos = cdp.call("Target.getTargets").get("targetInfos") or []
    finally:
        cdp.close()
    return [t for t in infos if t.get("type") == "page"]


# Ревью PR #704 (B1): все три — только закрытие, без reload и terminate.
@pytest.mark.parametrize("path", [
    "/adsmanager/manage/campaigns",
    "/adsmanager/manage/simple_create",
    "/adsmanager/manage/ads/edit",
])
def test_close_target_unblocks_a_hung_evaluate(chromium, site, path):
    proc, port = chromium
    job, out = _run_case(port, site, path)
    _common_asserts(job, out)
    assert out["result"]["tried"] == ["close:sent"], out["result"]
    assert proc.poll() is None, "the browser stays open"


def test_the_last_tab_is_closed_only_after_a_spare_one(chromium, site):
    proc, port = chromium
    job, out = _run_case(port, site, "/adsmanager/manage/campaigns", only=True)
    _common_asserts(job, out, only=True)
    assert out["result"]["tried"] == ["spare_tab:sent", "close:sent"], out["result"]
    assert proc.poll() is None, "the browser (the Multilogin profile) must stay open"
    pages = _browser_pages(port)
    assert [p["url"] for p in pages] == ["about:blank"], pages
