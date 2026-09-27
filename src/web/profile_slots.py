"""Сколько профилей Multilogin публикуют разом — по свободной памяти машины (ACL-1321).

«Publish all» отправляет все аккаунты сразу (acl-1210), и до ACL-1321 сервер
поднимал все профили параллельно. Замер 25–26.09 на ноутбуке владельца: три
профиля Mimic с Ads Manager — память занята на 85–89 %, шаги в 1,2–2,7 раза
медленнее, вкладки подвисают (ACL-1320). Каждый профиль берёт ~1–2 ГБ. Шесть
аккаунтов разом такой ноутбук не вытянет, а машина с 64 ГБ — вытянет.

Решение владельца (26.09): сколько профилей идёт разом, дешборд решает сам,
по свободной физической памяти; пол — один, потолок есть, и на машине его
можно переопределить настройкой.

Правило (`decide`), проверяется перед КАЖДЫМ стартом:

* ничего не идёт — старт всегда (пол: одна работа идёт при любой памяти);
* `ACL_MAX_PARALLEL_PROFILES=N` в машинном `.env` — ровно N разом, память
  не спрашивается (ручное переопределение);
* иначе потолок `ACL_MAX_PARALLEL_CEILING` (4) и память: новый профиль
  стартует, если свободно не меньше «профиль + запас»
  (`ACL_PROFILE_MEMORY_GB` 1,5 + `ACL_MEMORY_RESERVE_GB` 1,5). Профиль,
  стартовавший меньше `ACL_PROFILE_WARMUP_S` (90 с) назад, свою память ещё
  не занял — её вычитаем из свободной сами, иначе шесть заданий,
  пришедших в одну секунду, увидели бы одну и ту же свободную память и
  стартовали бы все;
* память не прочиталась — не больше двух разом (`FALLBACK_LIMIT`).

Очередь (`ProfileSlots`) — в порядке отправки. Задание, чей профиль уже
занят другим заданием, ждёт своего профиля и не держит места: следующие за
ним аккаунты на других профилях идут вперёд, а оно остаётся первым в
очереди и стартует, как только профиль освободится. Ожидающие
перепроверяют решение по концу любого задания, по отмене и по таймеру
(`TICK_S`) — память освобождается и без конца заданий.

Настройки читаются из `os.environ` при каждом решении: машинный `.env`
загружает `config.paths.load_env` при старте дешборда, а
`update_machine_env` меняет окружение сразу, без перезапуска.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Сколько секунд ожидающий спит между перепроверками, если его не будили.
TICK_S = 2.0

#: Сколько разом, когда память прочитать не удалось: осторожно, но не один.
FALLBACK_LIMIT = 2

DEFAULT_CEILING = 4
DEFAULT_PROFILE_MEMORY_GB = 1.5
DEFAULT_RESERVE_GB = 1.5
DEFAULT_WARMUP_S = 90.0


@dataclass(frozen=True)
class SlotSettings:
    """Настройки решения; `override` — ручное число разом или None (авто)."""

    override: int | None = None
    ceiling: int = DEFAULT_CEILING
    profile_mb: int = int(DEFAULT_PROFILE_MEMORY_GB * 1024)
    reserve_mb: int = int(DEFAULT_RESERVE_GB * 1024)
    warmup_s: float = DEFAULT_WARMUP_S


@dataclass(frozen=True)
class MemoryReading:
    total_mb: int
    available_mb: int


def _env_number(env: dict[str, str] | Any, key: str, default: float, *, minimum: float) -> float:
    raw = str(env.get(key, "") or "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", key, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%r is below %s; using %s", key, raw, minimum, default)
        return default
    return value


def load_settings(env: dict[str, str] | Any = None) -> SlotSettings:
    """Настройки из окружения (машинный `.env`). Мусор в значении — умолчание."""
    env = os.environ if env is None else env
    override: int | None = None
    raw = str(env.get("ACL_MAX_PARALLEL_PROFILES", "") or "").strip().lower()
    if raw and raw not in ("auto", "0"):
        try:
            override = max(1, int(raw))
        except ValueError:
            log.warning("ACL_MAX_PARALLEL_PROFILES=%r is not a whole number; using automatic", raw)
    return SlotSettings(
        override=override,
        ceiling=int(_env_number(env, "ACL_MAX_PARALLEL_CEILING", DEFAULT_CEILING, minimum=1)),
        profile_mb=int(_env_number(env, "ACL_PROFILE_MEMORY_GB", DEFAULT_PROFILE_MEMORY_GB, minimum=0.1) * 1024),
        reserve_mb=int(_env_number(env, "ACL_MEMORY_RESERVE_GB", DEFAULT_RESERVE_GB, minimum=0) * 1024),
        warmup_s=_env_number(env, "ACL_PROFILE_WARMUP_S", DEFAULT_WARMUP_S, minimum=0),
    )


def read_memory() -> MemoryReading | None:
    """Свободная физическая память машины; None, если прочитать нечем.

    psutil стоит в venv (requirements.txt); без него — GlobalMemoryStatusEx.
    """
    try:
        import psutil

        vm = psutil.virtual_memory()
        return MemoryReading(total_mb=int(vm.total // 2**20), available_mb=int(vm.available // 2**20))
    except Exception:
        pass
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return MemoryReading(total_mb=int(status.ullTotalPhys // 2**20),
                             available_mb=int(status.ullAvailPhys // 2**20))
    except Exception:
        return None


def decide(running: int, warming: int, memory: MemoryReading | None,
           settings: SlotSettings) -> dict[str, Any]:
    """Можно ли стартовать ещё один профиль. Чистая функция — всё на входе.

    `running` — сколько профилей идёт, `warming` — сколько из них стартовали
    недавно и память ещё не заняли. Ответ — факты для телеметрии: `allow`,
    `reason`, `limit` (сколько разом сейчас выходит), память.
    """
    out: dict[str, Any] = {"running": running, "warming": warming}
    if memory is not None:
        out.update(total_mb=memory.total_mb, available_mb=memory.available_mb)
    if settings.override is not None:
        limit = settings.override
        out.update(limit=limit, allow=running < limit,
                   reason="override" if running < limit else "override_full")
        return out
    if running == 0:
        out.update(limit=_auto_limit(running, warming, memory, settings), allow=True, reason="floor")
        return out
    if running >= settings.ceiling:
        out.update(limit=settings.ceiling, allow=False, reason="ceiling")
        return out
    if memory is None:
        limit = min(FALLBACK_LIMIT, settings.ceiling)
        out.update(limit=limit, allow=running < limit,
                   reason="memory_unknown" if running < limit else "memory_unknown_full")
        return out
    need = settings.profile_mb * (1 + warming) + settings.reserve_mb
    out["need_mb"] = need
    limit = _auto_limit(running, warming, memory, settings)
    ok = memory.available_mb >= need
    out.update(limit=limit, allow=ok, reason="memory_ok" if ok else "memory_low")
    return out


def _auto_limit(running: int, warming: int, memory: MemoryReading | None,
                settings: SlotSettings) -> int:
    """Сколько профилей разом выходит по памяти сейчас (для показа и журнала)."""
    if memory is None:
        return max(1, min(FALLBACK_LIMIT, settings.ceiling))
    spare = memory.available_mb - settings.reserve_mb - settings.profile_mb * warming
    more = max(0, spare // settings.profile_mb) if settings.profile_mb > 0 else 0
    return int(max(1, min(settings.ceiling, running + more)))


class ProfileSlots:
    """Очередь заданий публикации к профилям: сколько разом — решает `decide`.

    Потокобезопасна; `acquire` блокирует поток задания, пока ему не дадут
    место (или его не отменят). Порядок замков: сначала свой, потом
    вызываемые из-под него `cancelled`/`on_wait` (замок заданий). Снаружи
    под замком заданий сюда не входить.
    """

    def __init__(
        self,
        *,
        memory: Callable[[], MemoryReading | None] = read_memory,
        settings: Callable[[], SlotSettings] = load_settings,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = TICK_S,
    ) -> None:
        self._memory = memory
        self._settings = settings
        self._clock = clock
        self._tick_s = tick_s
        self._cond = threading.Condition()
        self._waiting: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._running: dict[str, dict[str, Any]] = {}

    # ── решение под замком ─────────────────────────────────────────────

    def _busy_profiles(self) -> set[str]:
        return {r["profile"] for r in self._running.values()}

    def _head(self) -> str | None:
        """Первое в очереди задание, чей профиль свободен."""
        busy = self._busy_profiles()
        for job_id, w in self._waiting.items():
            if w["profile"] not in busy:
                return job_id
        return None

    def _warming(self) -> int:
        warmup = self._settings().warmup_s
        now = self._clock()
        return sum(1 for r in self._running.values() if now - r["started"] < warmup)

    def _verdict(self, job_id: str) -> dict[str, Any]:
        w = self._waiting[job_id]
        ahead = list(self._waiting).index(job_id)
        if w["profile"] in self._busy_profiles():
            return {"allow": False, "reason": "profile", "ahead": ahead}
        if self._head() != job_id:
            return {"allow": False, "reason": "order", "ahead": ahead}
        decision = decide(len(self._running), self._warming(), self._memory(), self._settings())
        decision["ahead"] = ahead
        return decision

    # ── для заданий ────────────────────────────────────────────────────

    def acquire(
        self,
        job_id: str,
        profile: str,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        on_wait: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        """Ждать места для задания. None — задание отменили, пока ждало.

        Ответ: `waited_s`, `decision` (что разрешило старт), `first_hold`
        (почему пришлось ждать; None — не ждало).
        """
        profile = str(profile or job_id)
        with self._cond:
            enqueued = self._clock()
            self._waiting[job_id] = {"profile": profile, "since": enqueued}
            first_hold: dict[str, Any] | None = None
            last_note: tuple | None = None
            try:
                while True:
                    if cancelled():
                        return None
                    verdict = self._verdict(job_id)
                    if verdict.get("allow"):
                        del self._waiting[job_id]
                        self._running[job_id] = {"profile": profile, "started": self._clock()}
                        self._cond.notify_all()   # следующий в очереди — перепроверить
                        return {"waited_s": round(self._clock() - enqueued, 1),
                                "decision": verdict, "first_hold": first_hold}
                    if first_hold is None:
                        first_hold = verdict
                    note = (verdict.get("reason"), verdict.get("ahead"))
                    if on_wait is not None and note != last_note:
                        last_note = note
                        try:
                            on_wait(dict(verdict))
                        except Exception:
                            pass   # показ очереди не должен ронять задание
                    self._cond.wait(timeout=self._tick_s)
            finally:
                if job_id in self._waiting:
                    del self._waiting[job_id]
                    self._cond.notify_all()

    def release(self, job_id: str) -> None:
        """Задание кончилось — место свободно, ожидающие перепроверяют."""
        with self._cond:
            self._running.pop(job_id, None)
            self._cond.notify_all()

    def wake(self) -> None:
        """Перепроверить сейчас (отмена ждущего задания)."""
        with self._cond:
            self._cond.notify_all()

    def snapshot(self) -> dict[str, Any]:
        """Что идёт, что ждёт и что решилось бы для следующего — для дешборда."""
        with self._cond:
            settings = self._settings()
            memory = self._memory()
            running = len(self._running)
            return {
                "settings": asdict(settings),
                "mode": "override" if settings.override is not None else "auto",
                "running": [{"job_id": j, "profile": r["profile"]} for j, r in self._running.items()],
                "waiting": [{"job_id": j, "profile": w["profile"]} for j, w in self._waiting.items()],
                "next": decide(running, self._warming(), memory, settings),
            }
