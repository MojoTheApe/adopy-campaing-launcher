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

ACL-1424 (01.10): четыре профиля заняли 13,6 ГБ — по ≈3,4 ГБ, не 1,5. Решение
владельца: «начинать с 3,5 и подстраиваться под свободное место».

* Оценка на старте прогона — 3,5 ГБ на профиль (`ACL_PROFILE_MEMORY_GB`).
* Как только хоть один идущий профиль прогрелся (`ACL_PROFILE_WARMUP_S`),
  меряем память его дерева процессов Mimic (`measure_profiles`: корневой
  `chrome.exe` из папки Mimic с `--client-session-id=<id профиля>` и все
  его потомки; чужой Chrome не считается) и берём среднее по прогретым
  профилям вместо оценки — но не ниже пола `ACL_PROFILE_MEMORY_FLOOR_GB`
  (1,5): профиль, ещё не открывший кампании, весит мало и пустил бы лишних.
* Замер идёт в своём потоке (`ProfileUsage`), решение берёт последний
  готовый ответ и никогда его не ждёт; замер дольше `MEASURE_TIMEOUT_S`,
  сбой или профиль, которого не нашли, — снова оценка 3,5 ГБ.
* Свободную память читаем каждый раз заново: человек открыл тяжёлое —
  следующий профиль ждёт, закрыл — стартует. Потолок и пол — как были.

Ревью PR #425 (02.10), «никогда не перегружать»:

* Профиль на 90-й секунде ещё может не открыть список кампаний — замер мал,
  среднее падает к полу, а профиль потом дорастёт до ~3,4 ГБ. Поэтому у
  каждого идущего профиля храним пик замера (не опускается, пока задание
  идёт: закрывающийся профиль ACL-1444 среднее вниз не тянет), а профиль
  моложе `ACL_PROFILE_SETTLE_S` (300 с) считаем не легче оценки.
* Рост уже идущих профилей резервируем: к «профиль + запас» прибавляем
  `growth_mb` — сумму «сколько каждому замеренному профилю ещё до
  `profile_mb`», и свободной памяти должно хватить и на это.
* В решение (и в телеметрию) идут только числа МБ списком — без id профилей.
* Замер, зависший дольше `MEASURE_RESTART_FACTOR` × `MEASURE_TIMEOUT_S`,
  адаптацию больше не держит: запускаем новый.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Сколько секунд ожидающий спит между перепроверками, если его не будили.
TICK_S = 2.0

#: Сколько разом, когда память прочитать не удалось: осторожно, но не один.
FALLBACK_LIMIT = 2

#: ACL-1603: жёсткий потолок — не больше двух профилей разом, что бы ни
#: показывала оценка памяти. Меняется на машине через `ACL_MAX_PROFILES`.
DEFAULT_CEILING = 2
DEFAULT_PROFILE_MEMORY_GB = 3.5
#: ACL-1603: замер профиля не ниже 3,5 ГБ, запас под ОС и чужие программы — 3 ГБ.
DEFAULT_PROFILE_FLOOR_GB = 3.5
DEFAULT_RESERVE_GB = 3.0
DEFAULT_WARMUP_S = 90.0
#: Моложе этого профиль считаем не легче оценки: кампании/редактор ещё впереди.
DEFAULT_SETTLE_S = 300.0

#: Замер памяти профилей: свежим ответ считается столько секунд, дальше —
#: перечитать; старше `MEASURE_MAX_AGE_S` — не верить и брать оценку.
MEASURE_TTL_S = 5.0
MEASURE_MAX_AGE_S = 30.0
#: ACL-1642: «профиль открыт?» — свежим ответ считается столько секунд; старше
#: `OPEN_MAX_AGE_S` — не верить (профиль считается закрытым, как до ACL-1642).
OPEN_TTL_S = 5.0
OPEN_MAX_AGE_S = 15.0
#: Проверка, не вернувшаяся за это время, считается зависшей: новую не ждём
#: бесконечно, а через `OPEN_RESTART_S` запускаем заново.
OPEN_RESTART_S = 20.0

#: Замер, не вернувшийся за это время, считается сбоем (решение — по оценке).
MEASURE_TIMEOUT_S = 5.0
#: Замер висит дольше стольких таймаутов — бросаем его и запускаем новый.
MEASURE_RESTART_FACTOR = 4.0


@dataclass(frozen=True)
class SlotSettings:
    """Настройки решения.

    `override` — число разом без вопроса к памяти; из окружения НЕ читается
    (ACL-1603: потолок и память действуют всегда), оставлено для тестов.
    """

    override: int | None = None
    ceiling: int = DEFAULT_CEILING
    profile_mb: int = int(DEFAULT_PROFILE_MEMORY_GB * 1024)
    profile_floor_mb: int = int(DEFAULT_PROFILE_FLOOR_GB * 1024)
    reserve_mb: int = int(DEFAULT_RESERVE_GB * 1024)
    warmup_s: float = DEFAULT_WARMUP_S
    settle_s: float = DEFAULT_SETTLE_S


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
    # ACL-1603: потолок — `ACL_MAX_PROFILES` (по умолчанию 2). Прежние имена
    # `ACL_MAX_PARALLEL_PROFILES` / `ACL_MAX_PARALLEL_CEILING` читаются как
    # его запасные: ручное число больше не отменяет память и потолок.
    ceiling = DEFAULT_CEILING
    for key in ("ACL_MAX_PROFILES", "ACL_MAX_PARALLEL_PROFILES", "ACL_MAX_PARALLEL_CEILING"):
        raw = str(env.get(key, "") or "").strip().lower()
        if not raw or raw in ("auto", "0"):
            continue
        try:
            ceiling = max(1, int(float(raw.replace(",", "."))))
            break
        except ValueError:
            log.warning("%s=%r is not a whole number; trying the next setting", key, raw)
    return SlotSettings(
        ceiling=ceiling,
        profile_mb=int(_env_number(env, "ACL_PROFILE_MEMORY_GB", DEFAULT_PROFILE_MEMORY_GB, minimum=0.1) * 1024),
        profile_floor_mb=int(_env_number(env, "ACL_PROFILE_MEMORY_FLOOR_GB", DEFAULT_PROFILE_FLOOR_GB,
                                         minimum=0.1) * 1024),
        reserve_mb=int(_env_number(env, "ACL_MEMORY_RESERVE_GB", DEFAULT_RESERVE_GB, minimum=0) * 1024),
        warmup_s=_env_number(env, "ACL_PROFILE_WARMUP_S", DEFAULT_WARMUP_S, minimum=0),
        settle_s=_env_number(env, "ACL_PROFILE_SETTLE_S", DEFAULT_SETTLE_S, minimum=0),
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


def _mimic_processes() -> list[Any]:
    """Корневые и дочерние `chrome.exe` с нужными полями — через psutil."""
    import psutil

    return [p for p in psutil.process_iter(["name", "exe", "cmdline"])
            if str(p.info.get("name") or "").lower() == "chrome.exe"]


def _root_profile(proc: Any, wanted: dict[str, str]) -> str | None:
    """Какому из `wanted` профилей принадлежит корневой `chrome.exe` Mimic.

    None — не Mimic, дочерний процесс или чужой профиль. Только чтение
    полей, уже снятых psutil (`name`/`exe`/`cmdline`): процесс не трогаем.
    """
    info = getattr(proc, "info", {}) or {}
    if "mimic" not in str(info.get("exe") or "").lower():
        return None
    args = [str(a) for a in (info.get("cmdline") or [])]
    if any(a.startswith("--type=") for a in args):
        return None   # дочерний процесс; его посчитаем в дереве корня
    found = None
    for a in args:
        key, _, value = a.partition("=")
        value = value.strip().strip('"').lower()
        if key == "--client-session-id" and value in wanted:
            found = wanted[value]
        elif key == "--user-data-dir":
            tail = value.replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]
            if tail in wanted:
                found = wanted[tail]
        if found:
            break
    return found


def find_open_profiles(profiles: Any, *, processes: Callable[[], list[Any]] = _mimic_processes) -> set[str]:
    """Какие из `profiles` сейчас открыты в Multilogin (ACL-1642). Только чтение.

    Тот же признак, что у замера памяти: корневой `chrome.exe` Mimic этого
    профиля. Ничего не открываем, не закрываем и не цепляемся к порту —
    только список процессов. Сбой чтения — исключение наверх.
    """
    wanted = {str(p).strip().lower(): str(p) for p in profiles if str(p or "").strip()}
    if not wanted:
        return set()
    return {f for f in (_root_profile(proc, wanted) for proc in processes()) if f}


def measure_profiles(profiles: Any, *, processes: Callable[[], list[Any]] = _mimic_processes) -> dict[str, int]:
    """Память (МБ) дерева процессов Mimic каждого из `profiles`. Только чтение.

    Профиль узнаём по корневому `chrome.exe` из папки Mimic (Multilogin
    кладёт его в `mlx/deps/mimic_*`) с `--client-session-id=<id профиля>`
    (замер 01.10: это id профиля из карты аккаунтов) или с последней частью
    `--user-data-dir`, равной id. Свой Chrome человека не в папке Mimic — не
    считается. Память дерева — сумма рабочих наборов корня и всех потомков
    (общие страницы считаются дважды: оценка с запасом, а не в обрез).
    Профиля не нашли — его нет в ответе. Сбой чтения — исключение наверх.
    """
    wanted = {str(p).strip().lower(): str(p) for p in profiles if str(p or "").strip()}
    out: dict[str, int] = {}
    if not wanted:
        return out
    for proc in processes():
        found = _root_profile(proc, wanted)
        if not found:
            continue
        total = 0
        for member in [proc, *_children(proc)]:
            try:
                total += int(member.memory_info().rss)
            except Exception:
                continue   # процесс ушёл или закрыт для чтения — без него
        out[found] = out.get(found, 0) + total // 2**20
    return out


def _children(proc: Any) -> list[Any]:
    try:
        return list(proc.children(recursive=True))
    except Exception:
        return []


class ProfileUsage:
    """Замер памяти идущих профилей — в своём потоке, решение его не ждёт.

    `read(profiles)` отдаёт последний готовый замер этих профилей и, если он
    устарел (`ttl_s`), запускает новый в фоне. Ответ `status`:
    `ok` (есть `per_profile`), `pending` (первый замер ещё идёт), `timeout`
    (замер висит дольше `timeout_s`; второй поверх не запускаем, пока он не
    провисит `restart_factor` таймаутов — тогда запускаем новый), `failed`
    (замер упал), `stale` (последний ответ старше `max_age_s`). Кроме `ok`
    решает оценка. Готовый фоновый замер зовёт `on_ready` — очередь
    перепроверяет решение сразу, не дожидаясь таймера.

    `background=False` — мерить прямо в вызывающем потоке (тесты с
    подставным замером).
    """

    def __init__(
        self,
        measure: Callable[[list[str]], dict[str, int]] = measure_profiles,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = MEASURE_TTL_S,
        max_age_s: float = MEASURE_MAX_AGE_S,
        timeout_s: float = MEASURE_TIMEOUT_S,
        restart_factor: float = MEASURE_RESTART_FACTOR,
        background: bool = True,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        self._measure = measure
        self._clock = clock
        self._ttl_s = ttl_s
        self._max_age_s = max_age_s
        self._timeout_s = timeout_s
        self._restart_after_s = timeout_s * restart_factor
        self._background = background
        self.on_ready = on_ready
        self._lock = threading.Lock()
        # Последний ответ: когда, каких профилей, что (None — сбой).
        self._at: float | None = None
        self._for: frozenset[str] = frozenset()
        self._value: dict[str, int] | None = None
        self._error = ""
        self._inflight_since: float | None = None
        # Номер замера: брошенный зависший замер, вернувшись, не снимает
        # отметку «идёт» с нового.
        self._gen = 0

    def _run(self, profiles: list[str], gen: int = 0) -> None:
        try:
            value, error = dict(self._measure(profiles)), ""
        except Exception as exc:   # нечем прочитать, нет прав — решит оценка
            value, error = None, type(exc).__name__
            log.info("profile memory probe failed: %s", exc)
        with self._lock:
            self._at, self._for, self._value, self._error = self._clock(), frozenset(profiles), value, error
            if gen == self._gen:
                self._inflight_since = None
        if self._background and self.on_ready is not None:
            try:
                self.on_ready()
            except Exception:
                pass

    def read(self, profiles: list[str]) -> dict[str, Any]:
        wanted = frozenset(profiles)
        now = self._clock()
        with self._lock:
            fresh = self._at is not None and self._for == wanted and now - self._at < self._ttl_s
            hung = (self._inflight_since is not None
                    and now - self._inflight_since > self._restart_after_s)
            if hung:
                log.info("profile memory probe hung for %.0f s; starting a new one",
                         now - self._inflight_since)
            start = not fresh and (self._inflight_since is None or hung)
            if start:
                self._inflight_since = now
                self._gen += 1
            gen = self._gen
        if start:
            if self._background:
                threading.Thread(target=self._run, args=(sorted(wanted), gen), daemon=True,
                                 name="acl-profile-memory").start()
            else:
                self._run(sorted(wanted), gen)
        with self._lock:
            if self._at is not None and self._for == wanted and now - self._at < self._max_age_s:
                if self._value is None:
                    return {"status": "failed", "error": self._error}
                return {"status": "ok", "per_profile": dict(self._value), "age_s": round(now - self._at, 1)}
            if self._inflight_since is not None and now - self._inflight_since > self._timeout_s:
                return {"status": "timeout"}
            if self._inflight_since is not None:
                return {"status": "pending"}
            return {"status": "stale"}


class OpenProfileProbe:
    """Открыт ли профиль в Multilogin (ACL-1642) — только чтение, не ждёт.

    Проверка — список процессов Mimic (`find_open_profiles`): ничего не
    открывает, не закрывает и не цепляется к порту. Идёт в своём потоке;
    `read` отдаёт последний готовый ответ и никогда его не ждёт, поэтому
    под замком очереди вызывать безопасно. Нет свежего ответа, проверка
    упала или зависла — `None`: профиль считаем закрытым (как до ACL-1642),
    память спрашиваем как раньше. Готовый ответ будит ожидающих (`on_ready`).
    `background=False` — проверять прямо в вызывающем потоке (тесты).
    """

    def __init__(
        self,
        check: Callable[[list[str]], set[str]] = find_open_profiles,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = OPEN_TTL_S,
        max_age_s: float = OPEN_MAX_AGE_S,
        restart_s: float = OPEN_RESTART_S,
        background: bool = True,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        self._check = check
        self._clock = clock
        self._ttl_s = ttl_s
        self._max_age_s = max_age_s
        self._restart_s = restart_s
        self._background = background
        self.on_ready = on_ready
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, bool]] = {}
        self._want: set[str] = set()
        self._inflight_since: float | None = None
        self._gen = 0
        #: Сколько раз реально проверяли (для тестов: кэш работает).
        self.checks = 0

    def _run(self, profiles: list[str], gen: int) -> None:
        try:
            opened = {str(p) for p in self._check(profiles)}
        except Exception as exc:   # нет прав / нет psutil — профиль «неизвестен»
            opened = None
            log.info("open-profile probe failed: %s", exc)
        with self._lock:
            self.checks += 1
            if opened is not None:
                now = self._clock()
                for p in profiles:
                    self._cache[p] = (now, p in opened)
            if gen == self._gen:
                self._inflight_since = None
        if self._background and self.on_ready is not None:
            try:
                self.on_ready()
            except Exception:
                pass

    def read(self, profile: str) -> bool | None:
        profile = str(profile or "")
        if not profile:
            return None
        now = self._clock()
        with self._lock:
            hit = self._cache.get(profile)
            fresh = hit is not None and now - hit[0] < self._ttl_s
            hung = self._inflight_since is not None and now - self._inflight_since > self._restart_s
            start = False
            if not fresh:
                self._want.add(profile)
                if self._inflight_since is None or hung:
                    start = True
                    self._inflight_since = now
                    self._gen += 1
            gen = self._gen
            batch = sorted(self._want) if start else []
            if start:
                self._want.clear()
        if start:
            if self._background:
                threading.Thread(target=self._run, args=(batch, gen), daemon=True,
                                 name="acl-open-profile").start()
            else:
                self._run(batch, gen)
        with self._lock:
            hit = self._cache.get(profile)
            if hit is not None and self._clock() - hit[0] < self._max_age_s:
                return hit[1]
            return None


def decide(running: int, warming: int, memory: MemoryReading | None,
           settings: SlotSettings, measured_mb: int | None = None,
           current_mb: list[int] | None = None,
           profile_open: bool = False) -> dict[str, Any]:
    """Можно ли стартовать ещё один профиль. Чистая функция — всё на входе.

    `running` — сколько профилей идёт, `warming` — сколько из них стартовали
    недавно и память ещё не заняли, `measured_mb` — средняя замеренная
    память прогретого профиля (None — замера нет, решает оценка),
    `current_mb` — сколько каждый замеренный профиль занимает сейчас: до
    `profile_mb` он ещё дорастёт, этот рост (`growth_mb`) резервируем. Ответ —
    факты для телеметрии: `allow`, `reason`, `limit` (сколько разом сейчас
    выходит), память, `profile_mb` (по чём считали профиль) и его источник.

    `profile_open` (ACL-1642) — профиль задания уже открыт: его память уже
    занята и из «свободно» вычтена, нового профиля стартом не прибавится.
    Тогда `memory_low` / `below_reserve` не спрашиваем; потолок действует.
    """
    out: dict[str, Any] = {"running": running, "warming": warming, "cap": settings.ceiling,
                           "reserve_mb": settings.reserve_mb, "profile_open": bool(profile_open)}
    if memory is not None:
        out.update(total_mb=memory.total_mb, available_mb=memory.available_mb)
    if settings.override is None:
        if measured_mb is not None:
            settings = replace(settings, profile_mb=max(int(measured_mb), settings.profile_floor_mb))
            out.update(measured_mb=int(measured_mb), profile_mb_source="measured")
        else:
            out["profile_mb_source"] = "estimate"
        out["profile_mb"] = settings.profile_mb
    growth = sum(max(0, settings.profile_mb - int(c)) for c in (current_mb or []))
    if settings.override is None and current_mb:
        out["growth_mb"] = growth
    if settings.override is not None:
        limit = settings.override
        out.update(limit=limit, allow=running < limit,
                   reason="override" if running < limit else "override_full")
        return out
    if running >= settings.ceiling:
        out.update(limit=settings.ceiling, allow=False, reason="ceiling")
        return out
    if memory is None:
        # Память не прочиталась: не блокируем навсегда, но и не больше двух.
        limit = min(FALLBACK_LIMIT, settings.ceiling)
        out.update(limit=limit, allow=running < limit,
                   reason="memory_unknown" if running < limit else "memory_unknown_full")
        return out
    if profile_open:
        # ACL-1642: профиль уже открыт — новой памяти нет, потолок проверен выше.
        out.update(limit=min(settings.ceiling, running + 1), allow=True, reason="profile_open")
        return out
    need = settings.profile_mb * (1 + warming) + settings.reserve_mb + growth
    out["need_mb"] = need
    if memory.available_mb < settings.reserve_mb:
        # ACL-1603: свободно меньше запаса — новый профиль не стартует, пока
        # память не вернётся (идущие профили при этом не трогаем).
        out.update(limit=running, allow=False, reason="below_reserve")
        return out
    limit = _auto_limit(running, warming, memory, settings, growth)
    ok = memory.available_mb >= need
    out.update(limit=limit, allow=ok, reason="memory_ok" if ok else "memory_low")
    return out


def _auto_limit(running: int, warming: int, memory: MemoryReading | None,
                settings: SlotSettings, growth_mb: int = 0) -> int:
    """Сколько профилей разом выходит по памяти сейчас (для показа и журнала)."""
    if memory is None:
        return max(1, min(FALLBACK_LIMIT, settings.ceiling))
    spare = (memory.available_mb - settings.reserve_mb - settings.profile_mb * warming
             - growth_mb)
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
        usage: ProfileUsage | None = None,
        open_probe: OpenProfileProbe | None = None,
    ) -> None:
        self._memory = memory
        # Замер идущих профилей (ACL-1424): готовый ответ будит ожидающих.
        self._usage = usage if usage is not None else ProfileUsage()
        if self._usage.on_ready is None:
            self._usage.on_ready = self.wake
        # ACL-1642: открыт ли профиль задания — отдельный неблокирующий кэш.
        self._open = open_probe if open_probe is not None else OpenProfileProbe()
        if self._open.on_ready is None:
            self._open.on_ready = self.wake
        self._settings = settings
        self._clock = clock
        self._tick_s = tick_s
        self._cond = threading.Condition()
        self._waiting: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._running: dict[str, dict[str, Any]] = {}
        # ACL-1580: сколько мест выдано каждому профилю за всё время — по
        # счётчику чтение узнаёт, что профилем успело воспользоваться другое
        # задание, пока оно ждало закрыть запущенный им профиль.
        self._grants: dict[str, int] = {}

    # ── решение под замком ─────────────────────────────────────────────

    def _busy_profiles(self) -> set[str]:
        # Оставленное открытым окно профиль не «занимает»: его новое задание
        # берёт себе (см. `_reuse_kept`), а не ждёт.
        return {r["profile"] for r in self._running.values() if not r.get("kept")}

    def _kept(self) -> list[str]:
        return [j for j, r in self._running.items() if r.get("kept")]

    def _count_running(self, profile: str = "") -> int:
        return len([1 for r in self._running.values()
                    if not (r.get("kept") and profile and r["profile"] == profile)])

    def _kept_accounts(self) -> list[str]:
        return sorted({str(r.get("label") or "") for r in self._running.values()
                       if r.get("kept") and r.get("label")})

    def release_kept(self, label: str) -> int:
        """Снять место с оставленного окна аккаунта, не закрывая его (ACL-1603).

        Выход, когда окно не закрыть из панели задания: человек сам решил,
        что окно можно не считать. Возвращает, сколько мест снято.
        """
        label = str(label or "").strip().lower()
        with self._cond:
            gone = [j for j, r in self._running.items()
                    if r.get("kept") and str(r.get("label") or "").strip().lower() == label]
            for j in gone:
                del self._running[j]
            self._cond.notify_all()
        return len(gone)

    def _reuse_kept(self, profile: str) -> None:
        """Новое задание на профиль берёт место оставленного там окна."""
        for j in [j for j, r in self._running.items() if r.get("kept") and r["profile"] == profile]:
            del self._running[j]

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
        return sum(1 for r in self._running.values()
                   if not r.get("kept") and not r.get("open") and now - r["started"] < warmup)

    def _is_open(self, profile: str) -> bool:
        """Профиль уже открыт в Multilogin (ACL-1642). Не ждёт: кэш, `None` = нет."""
        return bool(profile) and self._open.read(profile) is True

    def _may_start_ahead(self, job_id: str, head: str | None) -> bool:
        """ACL-1642: голова очереди ждёт память, а профиль этого задания уже открыт.

        Такое задание новой памяти не просит — пропускаем его вперёд. Не
        голодает ли голова: вперёд идут только задания на УЖЕ открытых
        профилях, их число конечно (по одному на профиль), и память головы
        они не берут. Среди таких идёт первое по очереди.
        """
        if head is None:
            return False
        if not self._is_open(self._waiting[job_id]["profile"]):
            return False
        held = self._decide_now(self._waiting[head]["profile"])
        if held.get("allow") or held.get("reason") not in ("memory_low", "below_reserve"):
            return False
        busy = self._busy_profiles()
        for other, w in self._waiting.items():
            if other == job_id:
                break
            if other != head and w["profile"] not in busy and self._is_open(w["profile"]):
                return False   # раньше стоит такое же задание — оно первое
        return True

    def _verdict(self, job_id: str) -> dict[str, Any]:
        w = self._waiting[job_id]
        ahead = list(self._waiting).index(job_id)
        if w["profile"] in self._busy_profiles():
            return {"allow": False, "reason": "profile", "ahead": ahead}
        head = self._head()
        jumped = False
        if head != job_id:
            if not self._may_start_ahead(job_id, head):
                return {"allow": False, "reason": "order", "ahead": ahead}
            jumped = True
        decision = self._decide_now(w["profile"])
        decision["ahead"] = ahead
        if jumped:
            if not (decision.get("allow") and decision.get("reason") == "profile_open"):
                # Потолок или сбой чтения: вперёд не пускаем, ждём своей очереди.
                return {"allow": False, "reason": "order", "ahead": ahead}
            decision["started_ahead"] = True
        return decision

    def _decide_now(self, profile: str = "") -> dict[str, Any]:
        """`decide` с замером прогретых профилей вместо оценки, если он есть.

        `profile` — профиль ждущего задания: оставленное на нём окно не
        считаем, задание займёт его место само.
        """
        settings = self._settings()
        # ACL-1603: окна, оставленные открытыми после сбоя (ревью), держат место.
        running = self._count_running(profile)
        measured_mb: int | None = None
        probe: dict[str, Any] = {}
        # Мерим, только когда это что-то решает: авто, кто-то идёт, потолок не взят.
        current: list[int] = []
        kept_n = len(self._kept())
        if settings.override is None and 0 < running and (running < settings.ceiling or kept_n):
            now = self._clock()
            settled = [r for r in self._running.values() if now - r["started"] >= settings.warmup_s]
            if settled:
                probe = self._usage.read(sorted({r["profile"] for r in settled}))
                per = probe.get("per_profile") or {}
                if probe.get("status") == "ok":
                    # Окно закрыли руками (в Multilogin) — место больше не держим.
                    for j in [j for j, r in self._running.items()
                              if r.get("kept") and r in settled and r["profile"] not in per]:
                        del self._running[j]
                    # Пересчёт, а не «минус один»: убранное окно могло быть тем,
                    # что мы и так не считали (тот же профиль) — вычли бы дважды.
                    running = self._count_running(profile)
                    kept_n = len(self._kept())
                if probe.get("status") == "ok" and per:
                    values: list[int] = []
                    for r in settled:
                        if r["profile"] not in per:
                            continue   # не нашли — не в среднем (закрылся / ещё не виден)
                        now_mb = int(per[r["profile"]])
                        # Пик: дерево, которое сжимается (профиль закрывается,
                        # ACL-1444) или ещё не выросло, среднее вниз не тянет.
                        r["peak_mb"] = max(int(r.get("peak_mb") or 0), now_mb)
                        value = r["peak_mb"]
                        if now - r["started"] < settings.settle_s:
                            # Молодой профиль кампании/редактор ещё не открыл —
                            # не легче оценки, что бы ни показал замер.
                            value = max(value, settings.profile_mb)
                        values.append(value)
                        current.append(now_mb)
                    if values:
                        # ACL-1603: самый тяжёлый из недавних замеров, не среднее.
                        measured_mb = int(max(values))
        decision = decide(running, self._warming(), self._memory(), settings, measured_mb, current,
                          profile_open=self._is_open(profile))
        if kept_n:
            decision["kept"] = kept_n
            decision["kept_accounts"] = self._kept_accounts()
            if not decision.get("allow") and decision.get("reason") == "ceiling":
                # Потолок занят окнами на проверке: человеку надо их закрыть.
                decision["reason"] = "review_windows"
        if probe:
            decision["measure"] = probe.get("status")
            if current:
                # Только числа: id профилей в телеметрию не уходят (ревью #425).
                decision["per_profile_mb"] = sorted(current)
        return decision

    # ── для заданий ────────────────────────────────────────────────────

    def acquire(
        self,
        job_id: str,
        profile: str,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        on_wait: Callable[[dict[str, Any]], None] | None = None,
        share_profile: bool = False,
    ) -> dict[str, Any] | None:
        """Ждать места для задания. None — задание отменили, пока ждало.

        Ответ: `waited_s`, `decision` (что разрешило старт), `first_hold`
        (почему пришлось ждать; None — не ждало), `shared`.

        ACL-1580: `share_profile=True` (чтение спенда запускает закрытый
        профиль) — если место этого профиля уже держит другое задание, чтение
        берёт его же место (`shared`, в «running» не встаёт, `release` ему
        отпускать нечего). Память профиля уже учтена; а ждать в очереди
        нельзя: Publish на этом профиле держит место и ждёт замок профиля,
        который держит чтение, — ждали бы друг друга. Проверка — под замком
        очереди, на каждом круге ожидания.
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
                    if share_profile and profile in self._busy_profiles():
                        del self._waiting[job_id]
                        self._cond.notify_all()
                        return {"waited_s": round(self._clock() - enqueued, 1),
                                "decision": {"allow": True, "reason": "shared_profile"},
                                "first_hold": first_hold, "shared": True}
                    verdict = self._verdict(job_id)
                    if verdict.get("allow"):
                        del self._waiting[job_id]
                        self._reuse_kept(profile)
                        self._running[job_id] = {"profile": profile, "started": self._clock(),
                                                 "open": verdict.get("reason") == "profile_open"}
                        self._grants[profile] = self._grants.get(profile, 0) + 1
                        self._cond.notify_all()   # следующий в очереди — перепроверить
                        return {"waited_s": round(self._clock() - enqueued, 1),
                                "decision": verdict, "first_hold": first_hold, "shared": False}
                    if first_hold is None:
                        first_hold = verdict
                    note: tuple = (verdict.get("reason"), verdict.get("ahead"))
                    if verdict.get("reason") == "memory_low":
                        # Причина на экране — «свободно X ГБ, профиль ~Y ГБ»:
                        # заметная перемена цифр тоже новость (шаг 0,5 / 0,25 ГБ).
                        note += (int(verdict.get("available_mb") or 0) // 512,
                                 int(verdict.get("profile_mb") or 0) // 256)
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

    def keep(self, job_id: str, label: str = "") -> None:
        """Задание кончилось, но окно профиля оставлено открытым (ACL-1603).

        Место остаётся занятым, пока профиль не закроют (`release`): иначе
        очередь не видела бы открытых окон и пускала третий и четвёртый Mimic.
        """
        with self._cond:
            if job_id in self._running:
                self._running[job_id]["kept"] = True
                self._running[job_id]["label"] = str(label or "")
            self._cond.notify_all()

    def grants(self, profile: str) -> int:
        """Сколько мест выдано этому профилю (ACL-1580)."""
        with self._cond:
            return self._grants.get(str(profile or ""), 0)

    def others_on(self, profile: str, job_id: str = "") -> bool:
        """Держит ли место этого профиля или ждёт его кто-то, кроме `job_id` (ACL-1580)."""
        profile = str(profile or "")
        with self._cond:
            return any(j != job_id and r["profile"] == profile and not r.get("kept")
                       for j, r in [*self._running.items(), *self._waiting.items()])

    def wake(self) -> None:
        """Перепроверить сейчас (отмена ждущего задания)."""
        with self._cond:
            self._cond.notify_all()

    def snapshot(self) -> dict[str, Any]:
        """Что идёт, что ждёт и что решилось бы для следующего — для дешборда."""
        with self._cond:
            settings = self._settings()
            return {
                "settings": asdict(settings),
                "mode": "override" if settings.override is not None else "auto",
                "running": [{"job_id": j, "profile": r["profile"], "kept": bool(r.get("kept"))}
                            for j, r in self._running.items()],
                "waiting": [{"job_id": j, "profile": w["profile"]} for j, w in self._waiting.items()],
                "next": self._decide_now(),
            }
