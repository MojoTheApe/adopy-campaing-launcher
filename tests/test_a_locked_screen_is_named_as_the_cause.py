"""Заблокированный экран называется причиной, а не показывается пустым сбоем.

Блокировка останавливает сборку: система уходит на защищённый рабочий стол,
браузер перестаёт получать время, и креатив, который загружается за 74
секунды, грузился 42 минуты, после чего прогон сдался. Машина об этом знала —
признак `LogonUI.exe` пишется в телеметрию, — и молчала. Человек шёл искать
поломку в кампании, в разметке Meta, в аккаунте. За один день так дважды
увели разбор не туда.

Знать причину и не назвать её хуже, чем не знать: это отправляет искать.

Спрашивать в конце поздно — к тому моменту человек уже вернулся к машине и
разблокировал её. Поэтому признак снимается **по ходу работы**, и достаточно
одного раза: если экран был заблокирован хоть раз за сборку, это и есть
объяснение.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import publish_jobs as pj

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _job(job_id: str = "j1") -> None:
    with pj._jobs_lock:
        pj._jobs[job_id] = {"status": "running", "results": [], "errors": [], "total": 1}


def _flag(job_id: str = "j1"):
    with pj._jobs_lock:
        return pj._jobs[job_id].get("screen_was_locked")


def _drop(job_id: str = "j1") -> None:
    with pj._jobs_lock:
        pj._jobs.pop(job_id, None)


def _wait_for(check, timeout=3.0):
    """Наблюдатель живёт в своём потоке — ждём его, а не спим наугад."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_a_lock_during_the_run_is_remembered(monkeypatch):
    """Одного раза достаточно: экран разблокировали, но сборка уже встала."""
    monkeypatch.setattr("src.fb_ads_rpa.run_telemetry.screen_locked", lambda: True)
    _job()
    try:
        with pj._watch_screen_lock("j1"):
            assert _wait_for(lambda: _flag() is True), "блокировку не заметили"
    finally:
        _drop()


def test_an_unlocked_run_is_not_labelled(monkeypatch):
    """Иначе объяснение приклеится к сбою, у которого причина другая."""
    monkeypatch.setattr("src.fb_ads_rpa.run_telemetry.screen_locked", lambda: False)
    _job()
    try:
        with pj._watch_screen_lock("j1"):
            time.sleep(0.1)
        assert _flag() is None, "незаблокированный прогон назван заблокированным"
    finally:
        _drop()


def test_not_knowing_is_not_the_same_as_locked(monkeypatch):
    """`screen_locked()` отвечает `None`, когда спросить не удалось. Это
    честный ответ, и утверждать по нему нечего."""
    monkeypatch.setattr("src.fb_ads_rpa.run_telemetry.screen_locked", lambda: None)
    _job()
    try:
        with pj._watch_screen_lock("j1"):
            time.sleep(0.1)
        assert _flag() is None, "«не знаю» засчитано за «заблокирован»"
    finally:
        _drop()


def test_a_broken_check_never_touches_the_run(monkeypatch):
    """Проверка — страховка. Её поломка не должна касаться сборки."""
    def angry():
        raise OSError("нет доступа к списку процессов")

    monkeypatch.setattr("src.fb_ads_rpa.run_telemetry.screen_locked", angry)
    _job()
    try:
        with pj._watch_screen_lock("j1"):
            time.sleep(0.1)
        assert _flag() is None
    finally:
        _drop()


def test_the_watcher_stops_with_the_run(monkeypatch):
    """Иначе после сотни сборок за вечер по машине ходит сотня потоков."""
    monkeypatch.setattr("src.fb_ads_rpa.run_telemetry.screen_locked", lambda: False)
    _job()
    before = threading.active_count()
    try:
        with pj._watch_screen_lock("j1"):
            pass
        assert _wait_for(lambda: threading.active_count() <= before), "наблюдатель пережил работу"
    finally:
        _drop()


def test_the_watch_covers_the_run_itself():
    """Смотреть надо, пока идёт сборка: после неё экран уже разблокирован."""
    src = (ROOT / "src" / "web" / "publish_jobs.py").read_text(encoding="utf-8")
    start = src.index("def _run_job_inner(")
    body = src[start: src.index("\ndef ", start + 1)]
    assert "_run_rpa(" in body, "срез не захватил тело работы — проверка ничего не стережёт"
    assert "_watch_screen_lock(job_id)" in body, "за экраном никто не смотрит во время работы"
    assert body.index("_watch_screen_lock(job_id)") < body.index("_run_rpa("), (
        "наблюдение начато после сборки"
    )


# ── и дешборд это говорит ─────────────────────────────────────────────

def _render_status() -> str:
    page = TPL.read_text(encoding="utf-8")
    block = page[page.index("function renderStatus(job) {"):]
    return block[: block.index("\n  }") + 4]


def test_the_dashboard_names_the_cause():
    block = _render_status()
    assert "job.screen_was_locked" in block, "дешборд не смотрит на признак"
    assert re.search(r"screen was locked", block), "причина не названа словами"


def test_it_is_not_said_over_a_finished_draft():
    """При удачном черновике это ничего не объясняет, а место занимает."""
    block = _render_status()
    cond = block[block.index("job.screen_was_locked"):]
    cond = cond[: cond.index("\n")]
    assert "!jobPublishable" in cond, "объяснение показывается и на удачной сборке"
    assert "job.status === 'done'" in cond, "объяснение показывается посреди работы"
