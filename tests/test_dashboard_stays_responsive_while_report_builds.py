"""Сервер отвечает, пока собирается отчёт (ACL-1399).

30.09 после перезапуска первая страница собирала отчёт прямо в обработчике
GET / — минуту в цикле событий. Всё это время /health и очередь публикаций
молчали по 15–20 с, а Publish со страницы упал «Failed to fetch»: задание
владельца потерялось.

Здесь настоящий uvicorn на свободном порту и сборка-заглушка, которая
несколько секунд честно жжёт процессор (как разбор книги — чистый Python,
держит GIL). Пока она идёт, управляющие запросы обязаны отвечать меньше
чем за секунду, а страница — сразу показать «цифры загружаются».
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BUILD_S = 4.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _burn(seconds: float) -> None:
    end = time.perf_counter() + seconds
    x = 0
    while time.perf_counter() < end:
        x += 1


def _get(url: str, *, method: str = "GET", data: bytes | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Host", "127.0.0.1")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode("utf-8", "replace"), e.code
    return status, body, time.perf_counter() - t0


@pytest.fixture
def server(tmp_path, monkeypatch):
    import uvicorn

    from src.web import app as web_app
    from src.web import cache

    book = tmp_path / "stats.xlsx"
    book.write_bytes(b"stub")
    builds = []

    def slow_build(*, enriched_df=None, my_cm=None, **_kw):
        builds.append(threading.current_thread().name)
        _burn(BUILD_S)
        return {"countries": [], "date": "2026-09-30", "my_cm": my_cm or ""}, None, []

    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p=None: str(book))
    monkeypatch.setattr(cache, "load_morning_report", slow_build)
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Tester")
    cache.cache_clear()
    web_app._PERIOD_CACHE.clear()

    port = _free_port()
    config = uvicorn.Config(web_app.app, host="127.0.0.1", port=port, log_level="warning",
                            lifespan="off")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}", builds
    srv.should_exit = True
    thread.join(10)
    # Дать фоновой сборке кончиться, чтобы не протекла в соседний тест.
    for _ in range(100):
        if not cache.background_build_state()["building"]:
            break
        time.sleep(0.1)
    cache.cache_clear()


CONTROL = (("/health", "GET"), ("/publish-jobs/slots", "GET"),
           ("/rpa-teach/status", "GET"), ("/report/status", "GET"),
           ("/publish-jobs/no-such-job/cancel", "POST"),
           ("/publish-jobs/no-such-job", "GET"))


def test_control_endpoints_answer_while_a_cold_report_builds(server):
    base, builds = server
    # Сервер уже работал: первые вызовы (импорт psutil, git за версией)
    # случились до сборки — на старте их прогревает _warm_control_paths.
    for path, method in CONTROL:
        if path != "/report/status":
            _get(base + path, method=method, data=b"" if method == "POST" else None)

    status, body, took = _get(base + "/")
    assert status == 200 and took < 1.0, f"GET / waited {took:.1f}s for the report"
    assert 'id="report-loading"' in body, "the page did not say the numbers are loading"
    time.sleep(0.3)
    assert builds, "the background build did not start"
    state = json.loads(_get(base + "/report/status")[1])
    assert state["building"] is True and state["ready"] is False, state

    # Страница, которой нужен отчёт, ждёт его — но в пуле, не в цикле.
    slow = {}
    t = threading.Thread(target=lambda: slow.update(r=_get(base + "/api/buyers")), daemon=True)
    t.start()

    worst = 0.0
    for _ in range(3):
        for path, method in CONTROL:
            status, _body, took = _get(base + path, method=method, data=b"" if method == "POST" else None)
            assert status in (200, 404), (path, status)
            worst = max(worst, took)
            assert took < 1.0, f"{path} took {took:.2f}s while the report was building"
        time.sleep(0.4)

    t.join(BUILD_S * 3)
    assert slow["r"][0] == 200
    assert len(builds) == 1, f"{len(builds)} cold builds ran at once instead of one"

    status, body, _ = _get(base + "/report/status")
    assert json.loads(body)["ready"] is True


def test_the_page_says_loading_then_the_report_is_served(server):
    base, builds = server
    _status, body, _ = _get(base + "/")
    assert 'id="report-loading"' in body
    # Экран сам спрашивает /report/status и перезагружается, когда готово.
    assert "/report/status" in body and "location.reload()" in body
    for _ in range(int(BUILD_S * 10) + 50):
        _s, st, _t = _get(base + "/report/status")
        if json.loads(st)["ready"]:
            break
        time.sleep(0.1)
    else:
        pytest.fail("report never became ready")
    assert len(builds) == 1


def test_a_failed_background_build_shows_the_real_error(tmp_path, monkeypatch):
    """Упавшая фоновая сборка не зацикливает «загружается» — страница
    рисуется обычным путём и показывает ошибку."""
    import httpx

    from src.web import app as web_app
    from src.web import cache

    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p=None: str(tmp_path / "missing.xlsx"))

    def missing(**_kw):
        raise FileNotFoundError("Stats workbook not found: missing.xlsx")

    monkeypatch.setattr(cache, "load_morning_report", missing)
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Tester")
    cache.cache_clear()

    def get(path):
        async def go():
            transport = httpx.ASGITransport(app=web_app.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                return await c.get(path)
        return asyncio.run(go())

    first = get("/")
    assert 'id="report-loading"' in first.text
    for _ in range(50):
        if not cache.background_build_state()["building"]:
            break
        time.sleep(0.05)
    assert get("/report/status").json()["error"]
    second = get("/")
    assert "No data file" in second.text and "missing.xlsx" in second.text


def test_cancel_never_runs_the_rpa_teardown_on_the_event_loop(monkeypatch):
    """29.09 12:54:59: Stop → cancel_job → publisher.cancel() в обработчике
    запроса. Отмена RPA сбрасывает у своего потока отметку идущего цикла
    asyncio — в потоке сервера это ломало сам цикл («loop … is not the running
    loop»), и дешборд переставал отвечать. Отмена уходит в свой поток."""
    from src.fb_ads_rpa.browser import _reset_asyncio_running_loop
    from src.web import publish_jobs as pj

    done = threading.Event()

    class Publisher:
        def cancel(self):
            _reset_asyncio_running_loop()      # то, что делает BrowserConnection.close
            time.sleep(1.5)                    # и ждёт браузер
            done.set()

    job_id = "acl1399-cancel"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "cancel_requested": False,
                            "results": [], "errors": []}
    pj._active_publishers[job_id] = Publisher()

    async def scenario():
        t0 = time.perf_counter()
        result = pj.cancel_job(job_id)
        took = time.perf_counter() - t0
        await asyncio.sleep(0.05)              # цикл жив и планирует дальше
        assert asyncio.get_running_loop() is not None
        return result, took

    # Ревью PR #408, R5: регресс (отмена снова в цикле) вешает asyncio.run
    # навсегда — сценарий идёт в своём потоке, и тест падает по таймауту, а
    # не зависает.
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    try:
        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        runner.join(10)
        assert not runner.is_alive(), "the event loop hung: cancel ran the RPA teardown on it"
        if "error" in box:
            raise box["error"]
        result, took = box["value"]
        assert result["ok"] is True
        assert took < 0.5, f"cancel_job blocked its caller for {took:.1f}s"
        assert done.wait(5), "the publisher was never told to stop"
        assert pj.get_job(job_id)["status"] == "cancelling"
    finally:
        pj._active_publishers.pop(job_id, None)
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)


def test_a_retried_publish_gets_the_same_job(monkeypatch):
    """Publish, чей ответ потерялся, повторяется с тем же ключом — и получает
    то же задание, а не второе на тот же аккаунт."""
    from src.web import publish_jobs as pj

    started = []

    def fake_start(account_label, country, slots, **_kw):
        time.sleep(0.2)                         # исходный запрос ещё идёт
        started.append(account_label)
        job_id = f"job-{len(started)}"
        with pj._jobs_lock:
            pj._jobs[job_id] = {"job_id": job_id, "status": "running"}
        return job_id

    monkeypatch.setattr(pj, "_start_job", fake_start)
    monkeypatch.setattr(pj, "_request_jobs", {})
    out = []
    threads = [threading.Thread(target=lambda: out.append(pj.start_job("A", "SA", [{}], request_id="k1")))
               for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert out == ["job-1", "job-1"], out
        assert pj.start_job("A", "SA", [{}], request_id="k1") == "job-1"
        assert pj.start_job("A", "SA", [{}], request_id="k2") == "job-2"
        assert pj.start_job("A", "SA", [{}]) == "job-3", "a publish without a key was deduplicated"
        assert started == ["A", "A", "A"]
    finally:
        with pj._jobs_lock:
            for j in ("job-1", "job-2", "job-3"):
                pj._jobs.pop(j, None)


def test_refresh_during_a_slow_build_does_not_reuse_the_older_build(tmp_path, monkeypatch):
    """Ревью PR #408, R3. Refresh, нажатый, пока идёт сборка, не получает её
    результат: та сборка начата ДО нажатия и может не знать свежих данных.
    Он ждёт её и собирает заново."""
    from src.web import cache

    book = tmp_path / "stats.xlsx"
    book.write_bytes(b"stub")
    first_started, release_first = threading.Event(), threading.Event()
    builds = []

    def build(*, enriched_df=None, my_cm=None, **_kw):
        n = len(builds) + 1
        builds.append(n)
        if n == 1:
            first_started.set()
            assert release_first.wait(10)
        return {"countries": [], "build": n}, None, []

    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p=None: str(book))
    monkeypatch.setattr(cache, "load_morning_report", build)
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Tester")
    cache.cache_clear()
    out = {}
    try:
        plain = threading.Thread(target=lambda: out.update(plain=cache.get_cached_report()[0]["build"]))
        plain.start()
        assert first_started.wait(5)
        refresh = threading.Thread(
            target=lambda: out.update(refresh=cache.get_cached_report(force_rebuild=True)[0]["build"]))
        refresh.start()
        time.sleep(0.2)
        # И обычный запрос во время сборки не начинает свою — он её ждёт.
        waiter = threading.Thread(target=lambda: out.update(waiter=cache.get_cached_report()[0]["build"]))
        waiter.start()
        time.sleep(0.2)
        release_first.set()
        for t in (plain, refresh, waiter):
            t.join(10)
        assert out["plain"] == 1
        assert out["refresh"] == 2, "Refresh returned the build that started before it was pressed"
        assert out["waiter"] in (1, 2)
        assert builds == [1, 2], builds
    finally:
        release_first.set()
        cache.cache_clear()


def test_the_status_poll_builds_another_period_itself(tmp_path, monkeypatch):
    """Ревью PR #408, R4. Экран «загружается» для периода B, когда фоновая
    сборка шла (или уже кончилась) для периода A: /report/status сам
    запускает сборку B — иначе экран опрашивал бы вечно."""
    import httpx
    import pandas as pd

    from src.web import app as web_app
    from src.web import cache

    book = tmp_path / "stats.xlsx"
    book.write_bytes(b"stub")
    frame = pd.DataFrame({"Date": ["2026-09-01", "2026-09-02"]})
    periods = []

    def base(*, enriched_df=None, my_cm=None, **_kw):
        return {"countries": [], "my_cm": my_cm or "", "source_excel": str(book)}, frame, []

    def period(*, enriched_df=None, my_cm=None, **_kw):
        periods.append(list(enriched_df["Date"]))
        time.sleep(0.2)
        return {"countries": []}, enriched_df, []

    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p=None: str(book))
    monkeypatch.setattr(cache, "load_morning_report", base)
    monkeypatch.setattr("src.analysis_agent.morning_service.load_morning_report", period)
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Tester")
    cache.cache_clear()
    web_app._PERIOD_CACHE.clear()

    def get(path):
        async def go():
            transport = httpx.ASGITransport(app=web_app.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                return await c.get(path)
        return asyncio.run(go())

    def wait_ready(query):
        for _ in range(100):
            if get("/report/status" + query).json()["ready"]:
                return True
            time.sleep(0.05)
        return False

    try:
        a, b = "?date_from=2026-09-01&date_to=2026-09-01", "?date_from=2026-09-02&date_to=2026-09-02"
        assert 'id="report-loading"' in get("/" + a).text
        assert wait_ready(a)
        assert periods == [["2026-09-01"]]
        # Период B: его никто не начинал — начинает сам опрос.
        assert wait_ready(b), "the loading page for another period would poll forever"
        assert periods == [["2026-09-01"], ["2026-09-02"]]
    finally:
        for _ in range(50):
            if not cache.background_build_state()["building"]:
                break
            time.sleep(0.05)
        cache.cache_clear()
        web_app._PERIOD_CACHE.clear()
