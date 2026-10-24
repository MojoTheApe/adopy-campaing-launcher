"""ACL-2063: короткий замок состояния и отдельные аренды очереди/чтения.

Файл состояния не занят, пока профиль читается: страница может сразу показать
сохранённый список, статус и поставить более приоритетную заявку.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading

from config.paths import _atomic_write, get_local_config_dir, machine_file_lock


class FileLease:
    """Одна аренда на путь: повторный вход своего потока не открывает OS-замок заново."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._depth = 0
        self._hold = None

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
            self._depth += 1
            entered = True
            yield
        finally:
            try:
                if entered:
                    self._depth -= 1
                    if self._depth == 0:
                        hold, self._hold = self._hold, None
                        hold.__exit__(None, None, None)
            finally:
                self._lock.release()


_LEASES: dict[str, FileLease] = {}
_LEASES_LOCK = threading.Lock()


def lease(path: Path) -> FileLease:
    key = str(path.resolve())
    with _LEASES_LOCK:
        return _LEASES.setdefault(key, FileLease(path))


def read_lease() -> FileLease:
    """Одна настоящая сессия чтения на машину, независимо от порта/ACL_CONFIG_DIR.

    Брать ПОСЛЕ физического замка профиля и ДО его открытия. Публикатор,
    уже владеющий своим профилем, тогда может обновить список без deadlock.
    """
    from src.fb_ads_rpa import profile_lease

    return lease(profile_lease._lease_root() / "account-structure-read.lock")


@contextmanager
def read_session(*, priority: bool = False):
    """Действие, ожидающее конца чтения, идёт раньше следующего обхода.

    OS-аренда намерения отпускается при смерти процесса, не оставляя вечной
    отметки в JSON. Обычное чтение держит её лишь до входа, действие — весь
    свой заход. Физическая аренда профиля всегда уже взята вызывающим.
    """
    from src.fb_ads_rpa import profile_lease

    intent = lease(profile_lease._lease_root() / "account-read-priority.lock")
    if priority:
        with intent.holding(), read_lease().holding():
            yield
    else:
        with intent.holding():
            held = read_lease().holding()
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
