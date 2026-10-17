"""Открытие страницы не ходит в сеть, а цикл сервера не убивается (ACL-1380).

Два хвоста ACL-1380.

1. `_load_ref_options` (GET / и `/api/ref-data`) звал
   `targeting_registry.pull(force=True)` — запрос к api.github.com с таймаутом
   15 с на каждом открытии. Теперь отрисовка читает копию на машине, а
   обновление идёт в своём потоке.

2. Ошибки asyncio 14.09/22.09/29.09 («loop … is not the running loop», «There
   is no current event loop in thread 'MainThread'», после чего /health молчит
   при живом процессе). Причина воспроизводится одной строкой: снять отметку
   «идущий цикл» (`browser._reset_asyncio_running_loop`) в потоке, который этот
   цикл крутит. Каждый следующий шаг любой задачи падает. Отмена задания
   делала это из обработчика запроса до ACL-1399; здесь — тест, что маршрут
   отмены так больше не умеет, и что наш сброс в потоке задания отказывается
   работать в потоке цикла.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import app as web  # noqa: E402
from src.web import publish_jobs  # noqa: E402


# ── 1. отрисовка без сети ─────────────────────────────────────────────────

@pytest.fixture
def slow_pull(monkeypatch, tmp_path):
    """`pull`, который «ждёт GitHub», пока тест его не отпустит."""
    from src.analysis_agent import targeting_registry as tr

    release = threading.Event()
    calls: list[tuple[str, dict]] = []

    def pull(**kwargs):
        calls.append((threading.current_thread().name, kwargs))
        release.wait(10)
        return {}

    monkeypatch.setattr(tr, "pull", pull)
    monkeypatch.setattr(web, "get_upload_fb_excel_path", lambda: str(tmp_path / "missing.xlsx"))
    web._targets_refreshing = False
    yield calls, release
    release.set()
    deadline = time.monotonic() + 5
    while web._targets_refreshing and time.monotonic() < deadline:
        time.sleep(0.01)


def test_ref_options_answer_without_waiting_for_the_library(slow_pull):
    calls, _release = slow_pull
    t0 = time.perf_counter()
    result = web._load_ref_options()
    elapsed = time.perf_counter() - t0

    assert elapsed < 1.0, f"render path waited {elapsed:.1f}s for the network"
    assert "targetings" in result
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    thread_name, kwargs = calls[0]
    assert thread_name == "acl-targets-refresh"
    assert threading.current_thread().name != thread_name
    # Без force: `pull` сам не идёт в сеть чаще раза в PULL_EVERY_SECONDS.
    assert not kwargs.get("force")


def test_reloads_while_a_refresh_waits_do_not_stack_requests(slow_pull):
    calls, release = slow_pull
    for _ in range(5):
        web._load_ref_options()
    time.sleep(0.2)
    assert len(calls) == 1

    release.set()
    deadline = time.monotonic() + 5
    while web._targets_refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    web._load_ref_options()
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2, "after the first refresh finished the next reload may refresh again"


def test_a_failing_refresh_does_not_break_the_page(monkeypatch, tmp_path):
    from src.analysis_agent import targeting_registry as tr

    done = threading.Event()

    def pull(**_kwargs):
        done.set()
        raise OSError("network down")

    monkeypatch.setattr(tr, "pull", pull)
    monkeypatch.setattr(web, "get_upload_fb_excel_path", lambda: str(tmp_path / "missing.xlsx"))
    web._targets_refreshing = False
    assert "targetings" in web._load_ref_options()
    assert done.wait(2)
    deadline = time.monotonic() + 2
    while web._targets_refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert web._targets_refreshing is False


def test_no_forced_library_pull_left_on_the_render_path():
    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    block = source[source.index("def _load_sheet_targets"): source.index("def _load_kb_targets")]
    assert "pull(force=True)" not in block


# ── 2. цикл сервера ───────────────────────────────────────────────────────

def test_the_cause_clearing_the_mark_on_the_loop_thread_kills_every_next_step():
    """Подпись логов 29.09 воспроизводится ровно так — это и есть причина."""
    script = textwrap.dedent("""
        import asyncio, asyncio.events as ae, sys
        async def victim():
            ae._set_running_loop(None)
            await asyncio.sleep(0.05)
        async def other():
            await asyncio.sleep(0.02)
            print("other survived", flush=True)
        async def main():
            asyncio.ensure_future(victim()); asyncio.ensure_future(other())
            await asyncio.sleep(0.2)
        loop = asyncio.new_event_loop()
        loop.call_later(1.0, loop.stop)
        try:
            loop.run_until_complete(main())
        except Exception as exc:
            print("run failed", type(exc).__name__, flush=True)
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert "is not the running loop" in proc.stderr
    assert "other survived" not in proc.stdout


def test_our_reset_refuses_to_run_on_the_loop_thread():
    from src.web.publish_jobs import _clear_inherited_asyncio_loop

    async def other():
        await asyncio.sleep(0.02)
        return "alive"

    async def main():
        task = asyncio.ensure_future(other())
        _clear_inherited_asyncio_loop()
        assert asyncio.get_running_loop() is not None
        return await asyncio.wait_for(task, 2)

    # Убитый цикл не падает, а висит — поэтому в своём потоке и со сроком.
    result: dict = {}
    loop_thread = threading.Thread(target=lambda: result.update(r=asyncio.run(main())), daemon=True)
    loop_thread.start()
    loop_thread.join(5)
    assert result.get("r") == "alive", "the loop died after our reset ran on its thread"


def test_our_reset_still_clears_an_inherited_mark_in_a_worker():
    """В потоке задания отметка — чужая (цикл сервера в другом потоке): её снимаем."""
    import asyncio.events as ae

    from src.web.publish_jobs import _clear_inherited_asyncio_loop

    seen: dict = {}

    async def main():
        loop = asyncio.get_running_loop()

        def worker():
            ae._set_running_loop(loop)  # как унаследованная отметка
            _clear_inherited_asyncio_loop()
            seen["after"] = ae._get_running_loop()

        await asyncio.to_thread(worker)
        await asyncio.sleep(0)
        return "alive"

    assert asyncio.run(main()) == "alive"
    assert seen["after"] is None


def _serve():
    import socket

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=port,
                                        log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    return f"http://127.0.0.1:{port}", srv, thread


def _call(url: str, *, method: str = "GET", timeout: float = 10.0) -> int:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_cancel_route_survives_a_publisher_that_resets_the_loop_mark():
    """Отмена, которая (как BrowserConnection.close) снимает отметку цикла,
    не должна ронять сервер: следующий запрос отвечает."""
    from src.fb_ads_rpa.browser import _reset_asyncio_running_loop

    cancelled = threading.Event()

    class ResettingPublisher:
        def cancel(self):
            _reset_asyncio_running_loop()
            cancelled.set()

    job_id = "acl1380-loop-test"
    with publish_jobs._jobs_lock:
        publish_jobs._jobs[job_id] = {"status": "running", "cancel_watch": True}
        publish_jobs._active_publishers[job_id] = ResettingPublisher()
    base, srv, thread = _serve()
    try:
        assert _call(f"{base}/health") == 200
        assert _call(f"{base}/publish-jobs/{job_id}/cancel", method="POST") == 200
        assert cancelled.wait(5)
        time.sleep(0.2)
        for _ in range(3):
            assert _call(f"{base}/health", timeout=5) == 200, "server loop stopped answering after cancel"
    finally:
        srv.should_exit = True
        thread.join(10)
        with publish_jobs._jobs_lock:
            publish_jobs._jobs.pop(job_id, None)
            publish_jobs._active_publishers.pop(job_id, None)
