"""Фоновый прогрев отчёта дешборда (ACL-1380).

Холодная сборка отчёта — десятки секунд: книга статистики, аккаунты из
GSheet, отчёт за период по умолчанию. Раньше всё это ждал первый, кто открыл
страницу после старта или после обновления книги. Здесь та же сборка идёт
в своём потоке: сразу при старте сервера и каждый раз, когда меняется книга
(её путь, mtime) или выбранный байер. Открытие страницы после этого берёт
готовое из кэша.

Две сборки разом не идут: кэш отчёта (`src.web.cache`) держит замок сборки,
и страница, открытая во время прогрева, дожидается его результата вместо
второго чтения книги. Отчёт за период считается под своим замком в
`report_for_period`.

Поток запускает только serve.py (через `ACL_DASHBOARD_BACKGROUND=1`); тесты
и разовые импорты `app` его не видят.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Hashable, Optional

#: Как часто смотреть на mtime книги. Это один stat — дёшево.
POLL_SECONDS = 5.0
#: После ошибки сборки не повторять её на каждом тике: книга, которую
#: не удалось прочитать, не станет читаемой за пять секунд.
RETRY_AFTER_ERROR_SECONDS = 60.0

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_state: dict = {"key": None, "ok": None, "error": "", "seconds": None, "finished_at": None}


def workbook_key() -> Optional[Hashable]:
    """Что делает отчёт устаревшим: путь книги, её mtime и выбранный байер."""
    from src.analysis_agent.morning_service import _resolve_excel_path
    from src.web.buyer import get_selected_cm

    path = _resolve_excel_path(None)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return (path, mtime, (get_selected_cm() or "").strip())


def warm_once(build: Callable[[], None], key: Callable[[], Optional[Hashable]] = workbook_key) -> bool:
    """Собрать отчёт, если ключ книги сменился с прошлой удачной сборки.

    Возвращает True, если сборка запускалась. Ошибки не бросаются — они в
    `status()` и в stderr: прогрев не должен ронять сервер.
    """
    try:
        current = key()
    except Exception as exc:  # noqa: BLE001
        current = None
        _state.update(ok=False, error=f"workbook key: {exc}")
    if current is None:
        return False
    if current == _state["key"]:
        if _state["ok"] or time.monotonic() - (_state["finished_at"] or 0.0) < RETRY_AFTER_ERROR_SECONDS:
            return False
    started = time.monotonic()
    try:
        build()
    except Exception as exc:  # noqa: BLE001
        _state.update(key=current, ok=False, error=str(exc),
                      seconds=round(time.monotonic() - started, 2), finished_at=time.monotonic())
        print(f"[warmup] report build failed: {exc}", file=sys.stderr)
        return True
    seconds = round(time.monotonic() - started, 2)
    _state.update(key=current, ok=True, error="", seconds=seconds, finished_at=time.monotonic())
    print(f"[warmup] report ready in {seconds:.1f}s", file=sys.stderr)
    return True


def _loop(build: Callable[[], None], key: Callable[[], Optional[Hashable]], poll: float) -> None:
    while not _stop.is_set():
        warm_once(build, key)
        _stop.wait(poll)


def start(build: Callable[[], None], *, key: Callable[[], Optional[Hashable]] = workbook_key,
          poll: float = POLL_SECONDS) -> bool:
    """Запустить поток прогрева (один на процесс). True — запущен сейчас."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, args=(build, key, poll),
                                   daemon=True, name="acl-report-warmup")
        _thread.start()
        return True


def stop(timeout: float = 5.0) -> None:
    """Остановить поток (для тестов и остановки сервера)."""
    global _thread
    _stop.set()
    with _lock:
        thread, _thread = _thread, None
    if thread is not None:
        thread.join(timeout)


def status() -> dict:
    return dict(_state)
