"""Обновление, отложенное до конца публикации (ACL-2668, решение владельца №11).

Прежде кнопка Update отказывала, пока шло задание кампании или чтение
аккаунта, и байер жал её снова, пока не получится. Теперь:

* идёт публикация — обновление встаёт в очередь и начинается само, как только
  заданий не осталось. Начаться посреди публикации оно не может: старт идёт
  только через `publish_jobs.reserve_update()`, а та под замком заданий
  проверяет, что живых заданий нет, и запрещает новые до рестарта;
* идёт чтение аккаунта — обновление его не ждёт: рестарт чтение обрывает, а
  очередь чтений после рестарта ставит его заново (`account_refresh`).

Состояние живёт в процессе сервера, а не в странице: перезагрузка страницы
видит то же «ждёт» (`status()` отдаёт `/acl/update/check`). Рестарт сервера
очередь, разумеется, стирает — но рестарт и есть то, чего она ждала.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Текст на кнопке, пока обновление ждёт конца публикации.
WAITING_TEXT = "Update starts after publishing finishes"

#: Как часто ожидающее обновление пробует занять место. Публикация длится
#: минуты, так что пара секунд задержки после её конца незаметна.
POLL_SECONDS = 2.0

_lock = threading.Lock()
_state: dict[str, Any] = {"state": "", "target": "", "error": ""}


def status() -> dict[str, Any]:
    """`state`: "" — ничего не ждёт, "waiting", "starting" или "failed"."""
    with _lock:
        out = dict(_state)
    if out["state"] == "waiting":
        out["message"] = WAITING_TEXT
    return out


def is_waiting() -> bool:
    with _lock:
        return _state["state"] == "waiting"


def _set(**values: Any) -> None:
    with _lock:
        _state.update(values)


def reset() -> None:
    """Для тестов: забыть очередь."""
    _set(state="", target="", error="")


def queue(target: str, *, reserve: Callable[[], bool], start: Callable[[], None],
          cancel: Callable[[], None], poll_s: float | None = None) -> bool:
    """Поставить обновление в очередь. False — оно уже стоит.

    `reserve()` — атомарно занять место (False, пока идёт публикация);
    `start()` — передать обновление и завершить процесс; `cancel()` —
    отпустить всё, что держалось, если `start()` упал.
    """
    with _lock:
        if _state["state"] == "waiting":
            return False
        _state.update(state="waiting", target=target, error="")
    delay = POLL_SECONDS if poll_s is None else poll_s
    threading.Thread(target=_wait_then_start, args=(reserve, start, cancel, delay),
                     daemon=True, name="acl-update-queued").start()
    return True


def _wait_then_start(reserve: Callable[[], bool], start: Callable[[], None],
                     cancel: Callable[[], None], delay: float) -> None:
    try:
        while not reserve():
            time.sleep(delay)
        _set(state="starting")
        start()
    except Exception as exc:  # noqa: BLE001 — сбой показываем на кнопке, сервер живёт дальше
        log.exception("Queued update did not start")
        from src.web.release_updates import UpdateError

        message = str(exc) if isinstance(exc, UpdateError) and str(exc) else             "The update could not be started. ACM is still running."
        _set(state="failed", error=message)
        try:
            cancel()
        except Exception:  # noqa: BLE001
            log.exception("Queued update cleanup failed")
