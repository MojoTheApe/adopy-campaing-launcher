"""ACL-1495: файловый замок машины не теряет правку и не падает под нагрузкой.

Две гонки, найденные в ACL-1491 на `hidden_accounts._transaction` (тот же
шаблон в `targeting_registry`):

1. `msvcrt.locking(LK_LOCK)` сдаётся после 10 × 1 с и бросает EDEADLK
   («Resource deadlock avoided»), если замок держат дольше — под нагрузкой
   это обычный случай, а не взаимная блокировка.
2. Первое использование `.lock`: каждый процесс, увидевший пустой файл,
   пишет в него `b"0"`; если байт 0 уже заблокирован другим процессом,
   `flush()` падает с PermissionError (Errno 13).

Тесты воспроизводят обе гонки детерминированно, без десяти секунд ожидания.
"""
from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path

import pytest

from config import paths
from src.analysis_agent import targeting_registry as tr
from src.web import hidden_accounts as ha

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(reason="msvcrt byte-range lock behaviour (LK_LOCK give-up, write conflicts)")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(tr, "_store", lambda: tmp_path / "targeting_registry.json")


def _hidden_lock() -> Path:
    return Path(paths.get_hidden_accounts_path()).with_suffix(".lock")


def _targeting_lock() -> Path:
    return tr._store().with_suffix(".lock")


STORES = [
    pytest.param(ha._transaction, _hidden_lock, id="hidden_accounts"),
    pytest.param(tr._transaction, _targeting_lock, id="targeting_registry"),
]


def _fail_first_lock_attempts(monkeypatch, failures: int) -> list:
    """Первые `failures` попыток взять замок падают EDEADLK — так ведёт себя
    LK_LOCK, когда соседний процесс держит замок дольше десяти секунд."""
    calls = []
    if os.name == "nt":
        import msvcrt
        real = msvcrt.locking

        def fake(fd, mode, nbytes):
            if mode != msvcrt.LK_UNLCK:
                calls.append(mode)
                if len(calls) <= failures:
                    raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return real(fd, mode, nbytes)

        monkeypatch.setattr(msvcrt, "locking", fake)
    else:
        import fcntl
        real = fcntl.flock

        def fake(fd, op):
            if not op & fcntl.LOCK_UN:
                calls.append(op)
                if len(calls) <= failures:
                    raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return real(fd, op)

        monkeypatch.setattr(fcntl, "flock", fake)
    return calls


@pytest.mark.parametrize("transaction, lock_path", STORES)
def test_deadlock_avoided_is_retried_not_fatal(monkeypatch, transaction, lock_path):
    calls = _fail_first_lock_attempts(monkeypatch, failures=3)
    entered = False
    with transaction():
        entered = True
    assert entered
    assert len(calls) >= 4  # три отказа и успешная попытка


def test_hidden_update_survives_deadlock_avoided(monkeypatch):
    _fail_first_lock_attempts(monkeypatch, failures=2)
    assert ha.set_hidden("A", True) == {"a"}
    assert ha.load() == {"a"}


@pytest.mark.skipif(os.name != "nt", reason="byte-range lock conflicts with writes only on Windows")
@pytest.mark.parametrize("transaction, lock_path", STORES)
def test_first_use_lock_file_is_not_written_under_a_held_lock(transaction, lock_path):
    """Файл замка только что создан (пуст), а соседний процесс уже держит
    байт 0. Старый код писал туда `b"0"` и падал на flush; новый ждёт."""
    import msvcrt

    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    holder = open(path, "r+b")
    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)  # за концом файла — законно
    errors: list[BaseException] = []
    entered = threading.Event()

    def worker():
        try:
            with transaction():
                entered.set()
        except BaseException as exc:  # noqa: BLE001 — тест собирает любое падение
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        time.sleep(0.3)
        assert not entered.is_set(), "the transaction must wait for the held lock"
    finally:
        holder.seek(0)
        msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        holder.close()
    thread.join(10)
    assert not errors, errors
    assert entered.is_set()


@pytest.mark.parametrize("transaction, lock_path", STORES)
def test_lock_held_too_long_fails_with_a_bounded_wait(monkeypatch, transaction, lock_path):
    """Ожидание ограничено: если замок держат всерьёз, честная ошибка лучше
    зависшего запроса дешборда."""
    monkeypatch.setattr(paths, "_LOCK_TIMEOUT", 0.5)
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with paths.machine_file_lock(path):
        done: list = []

        def worker():
            started = time.monotonic()
            try:
                with transaction():
                    done.append("entered")
            except TimeoutError:
                done.append(time.monotonic() - started)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(10)
    assert len(done) == 1 and isinstance(done[0], float), done
    assert 0.4 <= done[0] < 5


def test_lock_file_is_created_once_and_never_written(tmp_path):
    path = tmp_path / "nested" / "x.lock"
    for _ in range(3):
        with paths.machine_file_lock(path):
            pass
    assert path.exists()
    assert path.read_bytes() == b""
