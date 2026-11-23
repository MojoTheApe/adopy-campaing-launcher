"""Открытие страницы не ходит в сеть, а цикл сервера не убивается (ACL-1380).

Два хвоста ACL-1380.

1. `_load_ref_options` (GET / и `/api/ref-data`) звал
   `targeting_registry.pull(force=True)` — запрос к api.github.com с таймаутом
   15 с на каждом открытии. Теперь отрисовка читает копию на машине, а
   обновление идёт в своём потоке. ACL-2668: только по событиям, без порога.

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

def _wait_idle(timeout: float = 5) -> None:
    from src.analysis_agent import team_library as tl
    deadline = time.monotonic() + timeout
    while tl._bg_running and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture
def slow_pull(monkeypatch, tmp_path):
    """`pull` всех трёх видов библиотеки, который «ждёт GitHub», пока тест не отпустит."""
    from src.analysis_agent import ad_text_registry as ar, targeting_registry as tr, team_library as tl
    from src.fb_api import form_templates as ft

    release = threading.Event()
    calls: list[tuple[str, str, dict]] = []

    def slow(kind):
        def pull(**kwargs):
            calls.append((kind, threading.current_thread().name, kwargs))
            release.wait(10)
            return {}
        return pull

    monkeypatch.setattr(tl, "configured", lambda: True)
    monkeypatch.setattr(tr, "pull", slow("targets"))
    monkeypatch.setattr(ar, "pull", slow("ad_texts"))
    monkeypatch.setattr(ft, "pull", slow("form_templates"))
    monkeypatch.setattr(web, "get_upload_fb_excel_path", lambda: str(tmp_path / "missing.xlsx"))
    _wait_idle()
    yield calls, release
    release.set()
    _wait_idle()


def _wait_calls(calls, n, timeout=2):
    deadline = time.monotonic() + timeout
    while len(calls) < n and time.monotonic() < deadline:
        time.sleep(0.01)


def test_ref_options_answer_without_waiting_for_the_library(slow_pull):
    calls, _release = slow_pull
    t0 = time.perf_counter()
    result = web._load_ref_options()
    elapsed = time.perf_counter() - t0

    assert elapsed < 1.0, f"render path waited {elapsed:.1f}s for the network"
    assert "targetings" in result
    _wait_calls(calls, 3)
    # ACL-2668: загрузка страницы — событие: все три вида идут в GitHub, каждый
    # в своём потоке, принудительно (порога в 15 с больше нет).
    assert sorted(c[0] for c in calls) == ["ad_texts", "form_templates", "targets"]
    for kind, thread_name, kwargs in calls:
        assert thread_name == f"acl-library-sync-{kind}"
        assert kwargs.get("force") is True
    assert threading.current_thread().name.startswith("acl-library-sync-") is False


def test_reloads_while_a_refresh_waits_do_not_stack_requests(slow_pull):
    calls, release = slow_pull
    for _ in range(5):
        web._load_ref_options()
    time.sleep(0.2)
    assert len(calls) == 3

    release.set()
    _wait_idle()
    web._load_ref_options()
    _wait_calls(calls, 6)
    assert len(calls) == 6, "after the first refresh finished the next reload may refresh again"


def test_builder_form_picker_answers_from_the_copy_and_syncs_alongside(slow_pull, monkeypatch):
    from src.fb_api import form_templates as ft

    calls, _release = slow_pull
    monkeypatch.setattr(ft, "listing", lambda force=False: {
        "rows": [], "deleted": [], "library": {}, "migration": {}})
    t0 = time.perf_counter()
    resp = web.api_form_templates_list(force=False)
    assert time.perf_counter() - t0 < 1.0
    assert resp.status_code == 200
    _wait_calls(calls, 1)
    assert [(c[0], c[2]) for c in calls] == [("form_templates", {"force": True})]


def test_no_timer_without_an_event(monkeypatch, tmp_path):
    """Без событий библиотека не тянется: ни потока, ни сетевого чтения."""
    from src.analysis_agent import team_library as tl

    hits = []
    monkeypatch.setattr(tl, "configured", lambda: True)
    monkeypatch.setattr(tl, "fetch", lambda kind: hits.append(kind) or tl.empty(kind))
    # Чтение без force (как при публикации шаблона формы на каждое объявление).
    for _ in range(3):
        tl.pull("form_templates")
    assert hits == []
    assert not [t for t in threading.enumerate() if t.name.startswith("acl-library-sync-")]
    source = (ROOT / "src" / "analysis_agent" / "team_library.py").read_text(encoding="utf-8")
    assert "PULL_EVERY_SECONDS" not in source


def test_a_failing_refresh_does_not_break_the_page(monkeypatch, tmp_path):
    from src.analysis_agent import targeting_registry as tr, team_library as tl

    done = threading.Event()

    def pull(**_kwargs):
        done.set()
        raise OSError("network down")

    monkeypatch.setattr(tl, "configured", lambda: True)
    monkeypatch.setattr(tr, "pull", pull)
    monkeypatch.setattr(web, "get_upload_fb_excel_path", lambda: str(tmp_path / "missing.xlsx"))
    _wait_idle()
    assert "targetings" in web._load_ref_options()
    assert done.wait(2)
    _wait_idle()
    assert "targets" not in tl._bg_running


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
    # Формулировка зависит от версии Python: 3.11/3.12 — «is not the running loop»,
    # 3.13 — «no running event loop». Суть одна: метка текущего цикла сброшена.
    assert ("is not the running loop" in proc.stderr) or ("no running event loop" in proc.stderr)
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
