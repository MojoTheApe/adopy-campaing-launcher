"""Общий журнал остановок профилей и файловый замок (ACL-1812, ревью #813).

Быстрое «закрыли → открыли заново» — вероятная причина двух разлогинов 5414
(ACL-1811). Поэтому ОДИН журнал, который читают ВСЕ места API-пути, где профиль
может быть поднят (`fb_api.profile.open_profile`), и в который пишут все места, где
дешборд знает об остановке (Close profiles, мягкое закрытие после чтения, конец
задания Publish, профиль, замеченный закрытым). Остановки RPA-пути — задача ACL-1811.

Файл `profile_stops.json` лежит в каталоге настроек машины, рядом с реестром.
Запись — под файловым замком и со слиянием (берётся более позднее время), чтобы два
процесса дешборда не затирали журнал друг друга. Токенов и паролей здесь нет.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

RESTART_COOLDOWN_ENV = "ACL_PROFILE_RESTART_COOLDOWN_S"
DEFAULT_RESTART_COOLDOWN_S = 180.0

#: ACL-2117: сколько секунд действие готово ЖДАТЬ конца паузы после остановки, а не
#: отказывать. По умолчанию — вся пауза; `0` — прежнее поведение (сразу отказ).
RESTART_MAX_WAIT_ENV = "ACL_PROFILE_RESTART_MAX_WAIT_S"
#: Что видит человек, пока профиль досохраняется в облако MLX.
SYNC_WAIT_MESSAGE = "Waiting for the profile to finish saving (cloud sync)"
RESTART_POLL_S = 1.0
#: Текст, когда ждавшее действие отменили (Stop): ничего не запущено.
CANCELLED_TEXT = "Stopped while waiting for the profile to finish saving (cloud sync). Nothing was started."

#: Отмена текущего действия (Stop у Publish), если вызывающий не передал `cancelled` явно.
_CANCEL: "ContextVar[Callable[[], bool] | None]" = ContextVar("acl_stop_log_cancel", default=None)


@contextmanager
def cancel_scope(cancelled: Callable[[], bool] | None):
    """Внутри блока ожидание паузы после остановки прерывается, когда `cancelled()` истинно."""
    token = _CANCEL.set(cancelled)
    try:
        yield
    finally:
        _CANCEL.reset(token)

_STALE_LOCK_S = 10.0
_LOCK_WAIT_S = 5.0


def restart_cooldown_s() -> float:
    try:
        value = float(str(os.environ.get(RESTART_COOLDOWN_ENV, "") or DEFAULT_RESTART_COOLDOWN_S).replace(",", "."))
    except ValueError:
        value = DEFAULT_RESTART_COOLDOWN_S
    return max(0.0, min(3600.0, value))


def restart_max_wait_s() -> float:
    raw = os.environ.get(RESTART_MAX_WAIT_ENV, "")
    if str(raw).strip() == "":
        return restart_cooldown_s()
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        return restart_cooldown_s()
    return max(0.0, min(3600.0, value))


@contextmanager
def file_lock(path: Path):
    """Замок-файл между процессами (`O_EXCL`). Старый (>10 с) считается брошенным.

    Не получили за 5 с — идём без замка и пишем в журнал: дешборд не должен виснуть.
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_S
    held = False
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _STALE_LOCK_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
        except OSError:
            break
        if time.monotonic() >= deadline:
            log.warning("could not take %s in time; continuing without the lock", lock.name)
            break
        time.sleep(0.02)
    try:
        yield
    finally:
        if held:
            try:
                lock.unlink()
            except OSError:
                pass


def atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class StopLog:
    def __init__(self, path: Callable[[], Path] | Path | str | None = None,
                 wall: Callable[[], float] = time.time):
        self._path = path
        self._wall = wall
        self._tlock = threading.Lock()
        #: Кому сказать об остановке (параллельные запуски): тёплый реестр забывает
        #: порт и вкладку, холодная отметка порта снимается — следующий заход
        #: поднимает профиль заново через MLX, а не цепляется к мёртвому порту.
        self._listeners: list[Callable[[str], None]] = []

    def on_stop(self, listener: Callable[[str], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def _notify(self, profile_id: str) -> None:
        for listener in list(self._listeners):
            try:
                listener(profile_id)
            except Exception:  # noqa: BLE001 — запись остановки не роняет слушатель
                log.exception("A profile-stop listener failed")

    def file(self) -> Path:
        path = self._path() if callable(self._path) else self._path
        if path:
            return Path(path)
        from config.paths import get_local_config_dir

        return Path(get_local_config_dir()) / "profile_stops.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))} \
            if isinstance(data, dict) else {}

    def record(self, profile_id: str, at: float | None = None) -> None:
        """Остановка профиля замечена/сделана `at` (по умолчанию — сейчас)."""
        pid = (profile_id or "").strip()
        if not pid:
            return
        when = float(at if at is not None else self._wall())
        with self._tlock:
            try:
                path = self.file()
                path.parent.mkdir(parents=True, exist_ok=True)
                with file_lock(path):
                    data = self._read()
                    data[pid] = max(when, data.get(pid, 0.0))
                    # Старше суток не храним: пауза давно прошла.
                    cutoff = self._wall() - 86400
                    data = {k: v for k, v in data.items() if v >= cutoff}
                    atomic_write(path, data)
            except OSError:
                log.exception("Could not record a profile stop")
        self._notify(pid)

    def last_stop(self, profile_id: str) -> float | None:
        with self._tlock:
            return self._read().get((profile_id or "").strip())

    def recent_stop_at(self, profile_id: str) -> float | None:
        """Время остановки (эпоха), если пауза после неё ещё идёт; иначе None."""
        last = self.last_stop(profile_id)
        if last is None or last + restart_cooldown_s() <= self._wall():
            return None
        return last

    def restart_wait_s(self, profile_id: str) -> float:
        last = self.last_stop(profile_id)
        if last is None:
            return 0.0
        return max(0.0, last + restart_cooldown_s() - self._wall())


_DEFAULT = StopLog()


def default() -> StopLog:
    return _DEFAULT


def record_stop(profile_id: str) -> None:
    """Для мест, где остановку не должна ронять запись в журнал."""
    try:
        _DEFAULT.record(profile_id)
    except Exception:  # noqa: BLE001
        log.exception("Could not record a profile stop")


def refusal(profile_id: str, log_: StopLog | None = None) -> str:
    """Текст отказа, если профиль нельзя запускать (остановлен недавно); иначе пусто."""
    wait = (log_ or _DEFAULT).restart_wait_s(profile_id)
    if wait <= 0:
        return ""
    elapsed = max(0, int(restart_cooldown_s() - wait))
    left = int(wait) + (1 if wait % 1 else 0)
    mins, secs = divmod(left, 60)
    span = f"{mins} min {secs} s" if mins and secs else (f"{mins} min" if mins else f"{secs} s")
    return (f"This profile was closed {elapsed} s ago \u2014 wait about {span} before opening it again "
            "(protects the account from logouts). Nothing was started.")


def _sleep(seconds: float) -> None:      # точка подмены в тестах
    time.sleep(seconds)


def wait_or_refusal(profile_id: str, log_: StopLog | None = None, *,
                    on_wait: Callable[[str], None] | None = None,
                    cancelled: Callable[[], bool] | None = None,
                    sleep: Callable[[float], None] | None = None) -> str:
    """ACL-2117: подождать конец паузы после остановки; пусто — профиль можно запускать.

    MLX досохраняет профиль в облако уже после остановки, а его API не
    говорит, закончил ли он (в клиенте launcher такого вызова нет), поэтому
    ориентир — пауза от ухода процесса (`restart_cooldown_s`). Раньше действие
    в эту паузу отказывало; теперь оно стоит в очереди и ждёт, если ждать не
    дольше `restart_max_wait_s()`. Дольше (или отменили) — прежний текст отказа.
    Профиль, который не останавливали, не ждёт ничего.
    """
    pid = (profile_id or "").strip()
    stops = log_ or _DEFAULT
    why = refusal(pid, stops)
    if not why:
        return ""
    left = stops.restart_wait_s(pid)
    if left <= 0 or left > restart_max_wait_s():
        return why
    nap = sleep or _sleep
    cancelled = cancelled or _CANCEL.get()
    began = stops._wall()
    stop_at = stops.last_stop(pid)
    waited_for = left
    for _ in range(int(waited_for / 0.1) + 10):     # страховка от часов, которые не идут
        left = stops.restart_wait_s(pid)
        if left <= 0:
            break
        if cancelled is not None and cancelled():
            return CANCELLED_TEXT
        if on_wait is not None:
            try:
                on_wait(SYNC_WAIT_MESSAGE)
            except Exception:  # noqa: BLE001 — строка ожидания не должна ронять запуск
                log.exception("A profile wait note failed")
        nap(max(0.1, min(RESTART_POLL_S, left)))
    else:
        return refusal(pid, stops)
    waited = max(0.0, stops._wall() - began)
    try:
        from src.fb_api import launch as _launch

        _launch._record({"event": "fb_api_profile_sync_wait", "sync_status": "cooldown",
                         "cooldown_s": round(restart_cooldown_s(), 1),
                         "waited_s": round(waited, 1), "planned_s": round(waited_for, 1),
                         "stop_at": stop_at})
        _launch.note_waited(pid, stop_at=stop_at, waited_s=waited)
    except Exception:  # noqa: BLE001
        pass
    if on_wait is not None:
        try:
            on_wait("")
        except Exception:  # noqa: BLE001
            pass
    return ""
