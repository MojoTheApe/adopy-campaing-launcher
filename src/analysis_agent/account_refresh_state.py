"""ACL-2063: короткий замок состояния и отдельные аренды очереди/чтения.

Файл состояния не занят, пока профиль читается: страница может сразу показать
сохранённый список, статус и поставить более приоритетную заявку.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time
import hashlib

from config.paths import _atomic_write, get_local_config_dir, machine_file_lock


class FileLease:
    """Одна аренда на путь: повторный вход своего потока не открывает OS-замок заново."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._depth = 0
        self._hold = None
        self._owner = None

    def owned(self) -> bool:
        return self._owner == threading.get_ident() and self._depth > 0

    @contextmanager
    def holding(self, timeout: float = 120.0):
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError("Another account read is still running.")
        entered = False
        try:
            if self._depth == 0:
                hold = machine_file_lock(self.path, timeout=timeout)
                hold.__enter__()
                self._hold = hold
                self._owner = threading.get_ident()
            self._depth += 1
            entered = True
            yield
        finally:
            try:
                if entered:
                    self._depth -= 1
                    if self._depth == 0:
                        hold, self._hold = self._hold, None
                        self._owner = None
                        hold.__exit__(None, None, None)
            finally:
                self._lock.release()


_LEASES: dict[str, FileLease] = {}
_LEASES_LOCK = threading.Lock()


def machine_root() -> Path:
    from src.analysis_agent.account_refresh_machine import default_root
    return default_root()


def machine_lease(name: str) -> FileLease:
    from src.analysis_agent.account_refresh_machine import prepare_lock
    path = machine_root() / name
    prepare_lock(path)
    return lease(path)


def target_lease(profile: str) -> FileLease:
    from src.fb_ads_rpa.profile_lease import _canonical_key
    key = _canonical_key(profile)
    return machine_lease("target-" + hashlib.sha256(key.encode()).hexdigest() + ".lock")


@contextmanager
def target_activity(profile: str, *, cancelled=None):
    """Only this feature's reader/publisher interlock, per physical target.

    The caller keeps its existing physical-profile ownership. Different targets
    have different leases; an inline same-thread refresh is reentrant.
    """
    from src.fb_ads_rpa.profile_lease import ProfileLeaseError
    lock = target_lease(profile)
    end = time.monotonic() + 120
    while True:
        if cancelled is not None and cancelled():
            raise ProfileLeaseError("The operation was cancelled while waiting for its account read.")
        hold = lock.holding(timeout=min(.1, max(0, end - time.monotonic())))
        try:
            hold.__enter__()
            break
        except TimeoutError:
            if time.monotonic() >= end:
                raise ProfileLeaseError("This profile is still being used by another ACL account read.")
    try:
        yield
    finally:
        hold.__exit__(None, None, None)


def lease(path: Path) -> FileLease:
    key = str(path.resolve())
    with _LEASES_LOCK:
        return _LEASES.setdefault(key, FileLease(path))


def read_lease() -> FileLease:
    """Одна настоящая сессия чтения на машину, независимо от порта/ACL_CONFIG_DIR.

    Брать ПОСЛЕ физического замка профиля и ДО его открытия. Публикатор,
    уже владеющий своим профилем, тогда может обновить список без deadlock.
    """
    return machine_lease("account-structure-read.lock")


@contextmanager
def read_session(*, priority: bool = False, timeout: float = 120):
    """Действие, ожидающее конца чтения, идёт раньше следующего обхода.

    OS-аренда намерения отпускается при смерти процесса, не оставляя вечной
    отметки в JSON. Обычное чтение держит её лишь до входа, действие — весь
    свой заход. Физическая аренда профиля всегда уже взята вызывающим.
    """
    read = read_lease()
    if read.owned():
        with read.holding(timeout=timeout):
            yield
        return
    intent = machine_lease("account-read-priority.lock")
    if priority:
        with intent.holding(timeout=timeout), read.holding(timeout=timeout):
            yield
    else:
        with intent.holding(timeout=timeout):
            held = read.holding(timeout=timeout)
            held.__enter__()
        try:
            yield
        finally:
            held.__exit__(None, None, None)


def empty_state() -> dict:
    return {"version": 1, "seq": 0, "entries": {}, "running": None,
            "rows": {}, "halt": None, "sweep": None, "windows_done": {}}


class RefreshStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root if root is not None else get_local_config_dir())
        self.path = self.root / "account_refresh.json"
        self.state_lock = lease(self.root / "account_refresh.lock")
        self.dispatch_lock = lease(self.root / "account_refresh-worker.lock")

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_state()
        except (OSError, ValueError) as exc:
            raise RuntimeError("Saved account refresh state could not be read. "
                               "Restore its settings backup before retrying.") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise RuntimeError("This account refresh state is not supported by this ACL version.")
        if (not isinstance(raw.get("entries"), dict) or not isinstance(raw.get("rows"), dict)
                or not isinstance(raw.get("windows_done"), dict)):
            raise RuntimeError("Saved account refresh state is incomplete. Restore its settings backup.")
        return raw

    @contextmanager
    def transaction(self):
        with self.state_lock.holding():
            state = self._load()
            original = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != original:
                _atomic_write(self.path, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def read(self) -> dict:
        with self.state_lock.holding():
            return self._load()
