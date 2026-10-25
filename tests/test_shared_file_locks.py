"""ACL-1512: журнал, тексты объявлений и библиотека команды — на общем замке.

ACL-1495 перевёл спрятанные аккаунты и реестр таргетов на
`config.paths.machine_file_lock`. Тот же старый шаблон — `msvcrt.locking
(LK_LOCK)`, который сдаётся после 10 × 1 с и бросает EDEADLK, и файл замка,
в который пишут `b"0"` при первом использовании, — оставался в трёх местах:
`src/acl_log.py`, `ad_text_registry._transaction` и
`team_library._sync_transaction`.

Здесь:

* проверка исходников: в `src/` не осталось ни одного прямого вызова замка ОС
  (`msvcrt.locking`, `fcntl.flock`/`lockf`, `LK_LOCK`) — только общий помощник;
* воспроизведение EDEADLK: первые попытки взять замок падают «Resource deadlock
  avoided», как у LK_LOCK под длинной очередью; на старом коде это падение
  выходило наружу, теперь его переждут;
* на каждое хранилище — несколько процессов пишут одновременно под фоновой
  нагрузкой на все ядра, и ни одна правка не теряется.
"""
from __future__ import annotations

import ast
import contextlib
import errno
import faulthandler
import hashlib
import json
import math
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(reason="msvcrt byte-range locks of the shared machine files")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import paths  # noqa: E402
from src import acl_log  # noqa: E402
from src.analysis_agent import ad_text_registry as ar  # noqa: E402
from src.analysis_agent import team_library as tl  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("ACL_LIBRARY_REPO", "ACL_LIBRARY_TOKEN", "ACL_LIBRARY_BRANCH"):
        monkeypatch.delenv(name, raising=False)
    tl.use_backend(None)
    yield
    tl.use_backend(None)


# ── в src/ нет своих замков ОС ────────────────────────────────────────────

_FORBIDDEN_ATTRS = {"LK_LOCK", "LK_RLCK", "LK_NBLCK", "LK_NBRLCK", "LOCK_EX", "LOCK_SH"}
_FORBIDDEN_CALLS = {("msvcrt", "locking"), ("fcntl", "flock"), ("fcntl", "lockf")}


def _os_lock_uses(source: str) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            pair = (node.value.id, node.attr)
            if pair in _FORBIDDEN_CALLS or node.attr in _FORBIDDEN_ATTRS:
                found.append((node.lineno, f"{pair[0]}.{pair[1]}"))
        elif isinstance(node, ast.ImportFrom) and node.module in ("msvcrt", "fcntl"):
            found.append((node.lineno, f"from {node.module} import ..."))
    return found


def test_no_inline_os_file_locks_remain_in_src():
    """Комментарии и докстроки не в счёт — смотрим дерево разбора."""
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for line, what in _os_lock_uses(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{line} {what}")
    assert not offenders, (
        "use config.paths.machine_file_lock instead of an inline OS lock:\n" + "\n".join(offenders))


def test_the_source_check_sees_the_old_pattern():
    """Контроль: проверка выше действительно ловит прежний шаблон."""
    old = (
        "import msvcrt\n"
        "def grab(f):\n"
        "    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)\n"
        "    # msvcrt.LK_LOCK in a comment is fine\n"
    )
    assert [w for _, w in _os_lock_uses(old)] == ["msvcrt.locking", "msvcrt.LK_LOCK"]


def test_the_three_stores_go_through_the_shared_helper(monkeypatch, tmp_path):
    seen: list[str] = []
    real = paths.machine_file_lock

    @contextlib.contextmanager
    def spy(path, timeout=None):
        seen.append(Path(path).name)
        with real(path, timeout):
            yield

    monkeypatch.setattr(paths, "machine_file_lock", spy)
    monkeypatch.setattr(acl_log, "_os_lock", spy)
    acl_log.append_line(tmp_path / "j.jsonl", "{}")
    with ar._transaction():
        pass
    with tl._sync_transaction("ad_texts"):
        pass
    assert seen == ["j.jsonl.lock", "ad_text_registry.lock", "team_library_ad_texts.lock"]


# ── EDEADLK переживается, а не выходит наружу ─────────────────────────────

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


def test_journal_line_survives_deadlock_avoided(monkeypatch, tmp_path):
    calls = _fail_first_lock_attempts(monkeypatch, failures=3)
    path = tmp_path / "journal.jsonl"
    acl_log.append_line(path, '{"n": 1}')
    assert path.read_text(encoding="utf-8").splitlines() == ['{"n": 1}']
    assert len(calls) >= 4


def test_ad_text_survives_deadlock_avoided(monkeypatch):
    calls = _fail_first_lock_attempts(monkeypatch, failures=3)
    ar.add("Spring", "Headline", "Body")
    assert [t.name for t in ar.all_texts()] == ["Spring"]
    assert len(calls) >= 4


def test_team_library_sync_survives_deadlock_avoided(monkeypatch):
    calls = _fail_first_lock_attempts(monkeypatch, failures=3)
    entered = False
    with tl._sync_transaction("ad_texts"):
        entered = True
    assert entered and len(calls) >= 4


def test_ad_text_save_waits_out_a_briefly_held_target(monkeypatch):
    """Найдено прогоном под нагрузкой: `os.replace` отказал WinError 5, пока
    файл реестра на миг открыт кем-то ещё (прошлый писатель, Defender), и
    правка терялась. Теперь замена ждёт, как у таргетов (ACL-1419)."""
    ar.add("First", "Headline 1", "Body 1")
    real = os.replace
    refused = []

    def flaky(src, dst):
        if str(dst).endswith("ad_text_registry.json") and len(refused) < 2:
            refused.append(dst)
            raise PermissionError(13, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    ar.add("Second", "Headline 2", "Body 2")
    assert len(refused) == 2
    assert {t.name for t in ar.all_texts()} == {"First", "Second"}


@pytest.mark.skipif(os.name != "nt", reason="byte-range lock conflicts with writes only on Windows")
@pytest.mark.parametrize("store", ["ad_texts", "team_library"])
def test_first_use_lock_file_is_not_written_under_a_held_lock(store):
    """Файл замка пуст, а байт 0 уже держит соседний процесс. Старый код писал
    туда `b"0"` и падал PermissionError на flush; новый ждёт."""
    import msvcrt

    if store == "ad_texts":
        path, transaction = ar._store().with_suffix(".lock"), ar._transaction
    else:
        path = Path(paths.get_local_config_dir()) / "team_library_ad_texts.lock"
        transaction = lambda: tl._sync_transaction("ad_texts")  # noqa: E731
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    holder = open(path, "r+b")
    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
    errors: list[BaseException] = []
    entered = threading.Event()

    def worker():
        try:
            with transaction():
                entered.set()
        except BaseException as exc:  # noqa: BLE001
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


# ── несколько процессов под нагрузкой ─────────────────────────────────────

PROCS = 4
PER_PROC = 12


@pytest.fixture
def busy_machine():
    """Занять все ядра, как в прогонах ACL-1491/1495: очередь за замком
    растягивается, и ожидание LK_LOCK (10 × 1 с) переставало хватать."""
    burners = [
        subprocess.Popen([sys.executable, "-c", "while True: pass"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(os.cpu_count() or 2)
    ]
    try:
        yield
    finally:
        for proc in burners:
            proc.kill()
        for proc in burners:
            proc.wait(10)


_WRITER_STATE = None
_WRITER_FIELDS = 6
_WRITER_PHASES = {"diagnostic-setup": 1, "call-prepared": 2, "target-entry": 3,
                  "before-write": 4, "after-write": 5}


def _writer_progress(phase: str, iteration: int) -> None:
    """Best-effort observations only: no queue, extra lock or acceptance input."""
    if _WRITER_STATE is not None:
        state, index = _WRITER_STATE
        offset = index * _WRITER_FIELDS
        state[offset + 2] = iteration
        state[offset + 1] = _WRITER_PHASES[phase]
        if phase == "target-entry":
            state[offset + 5] = 1  # Only a real target body records this event.


def _diagnosed_writer(target, start, index, extra, state, trace_dir, diagnostic_after):
    global _WRITER_STATE
    _WRITER_STATE = (state, index)
    offset = index * _WRITER_FIELDS
    state[offset] = 1  # Wrapper entered; not yet evidence of target entry.
    state[offset + 3] = os.getpid()
    _writer_progress("diagnostic-setup", -1)
    trace = None
    armed = False
    try:
        try:
            if trace_dir is None:
                raise OSError("diagnostic directory unavailable")
            trace = open(Path(trace_dir) / f"writer-{index}.trace", "w", encoding="utf-8")
            trace.write(f"writer_pid={os.getpid()} index={index} target={target.__name__}\n")
            trace.flush()
            faulthandler.dump_traceback_later(diagnostic_after, file=trace, repeat=False)
            armed = True
            state[offset + 4] = 1
        except (OSError, RuntimeError):
            # Missing diagnostics never replaces or skips the actual workload.
            state[offset + 4] = -1
        _writer_progress("call-prepared", -1)
        state[offset] = 2
        try:
            target(start, index, *extra)
        except BaseException:
            state[offset] = 4
            raise
        else:
            state[offset] = 3
    finally:
        if armed:
            faulthandler.cancel_dump_traceback_later()
        if trace is not None:
            with contextlib.suppress(OSError):
                trace.close()
        _WRITER_STATE = None


def _writer_observations(procs, state):
    phases = {v: k for k, v in _WRITER_PHASES.items()}
    observed = []
    for index, proc in enumerate(procs):
        offset = index * _WRITER_FIELDS
        status = state[offset]
        observed.append({
            "index": index, "pid": proc.pid, "name": proc.name,
            "alive_at_failure": proc.is_alive(), "exitcode_at_failure": proc.exitcode,
            "wrapper_entered": status != 0, "target_entry_observed": bool(state[offset + 5]),
            "last_wrapper_event": {0: "not-observed", 1: "diagnostic-setup", 2: "call-prepared", 3: "call-returned", 4: "call-raised"}[status],
            "observed_pid": state[offset + 3] or None,
            "phase": phases.get(state[offset + 1], "not-observed"),
            "iteration": state[offset + 2] if status else None,
            "capture": {0: "not-armed", 1: "armed", -1: "unavailable"}[state[offset + 4]],
        })
    return observed


def _writer_evidence_note(target, observed, trace_dir, cleanup_errors):
    # Read only after children are reaped. Never wait for a message/queue flush.
    for item in observed:
        if trace_dir is None:
            item["trace"] = ""
            item["trace_read_error"] = "DirectoryUnavailable"
            continue
        try:
            with open(Path(trace_dir) / f"writer-{item['index']}.trace", "rb") as stream:
                raw = stream.read(4_097)
            item["trace"] = raw[:4_096].decode("utf-8", errors="replace")
            if len(raw) > 4_096:
                item["trace"] += "\n[truncated]"
        except OSError as exc:
            item["trace"] = ""
            item["trace_read_error"] = type(exc).__name__
    return "Windows writer diagnostics: " + json.dumps({
        "target": target.__name__, "progress_is_last_observation": True,
        "observations_are_not_an_atomic_snapshot": True,
        "planned_writers": PROCS,
        "writers": observed, "cleanup_errors": cleanup_errors,
    }, ensure_ascii=True)


@contextlib.contextmanager
def _writer_trace_directory():
    try:
        directory = tempfile.TemporaryDirectory(prefix="acl-writer-diagnostics-", ignore_cleanup_errors=True)
    except OSError:
        # Diagnostic storage is optional; the original real workload is not.
        yield None
    else:
        with directory as path:
            yield path


def _run_processes(target, *extra, join_timeout=180, diagnostic_after=150) -> None:
    if not (math.isfinite(join_timeout) and math.isfinite(diagnostic_after)
            and 0 < diagnostic_after < join_timeout):
        raise ValueError("diagnostic timing must be finite and precede the writer join limit")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    # Lock-free per-writer observations cannot become a new IPC deadlock.
    state = ctx.RawArray("q", PROCS * _WRITER_FIELDS)
    # A scanner retaining a diagnostic file must not replace the original failure.
    with _writer_trace_directory() as trace_dir:
        procs = []
        failure = None
        try:
            for index in range(PROCS):
                proc = ctx.Process(target=_diagnosed_writer,
                                   args=(target, start, index, extra, state, trace_dir, diagnostic_after))
                proc.start()
                procs.append(proc)
            start.set()
            for proc in procs:
                proc.join(join_timeout)
                assert proc.exitcode == 0, f"writer {proc.name} exit code {proc.exitcode}"
        except BaseException as exc:
            failure = exc
            raise
        finally:
            observed = _writer_observations(procs, state) if failure is not None else []
            cleanup_errors = []
            for proc in procs:
                try:
                    if proc.is_alive():
                        proc.terminate()
                    proc.join()
                except Exception as exc:
                    cleanup_errors.append({"pid": proc.pid, "error": type(exc).__name__})
            if failure is not None:
                failure.add_note(_writer_evidence_note(target, observed, trace_dir, cleanup_errors))
            elif cleanup_errors:
                raise RuntimeError("writer cleanup failed: " + json.dumps(cleanup_errors))


def _journal_writer(start, index, path):
    _writer_progress("target-entry", -1)
    assert start.wait(60)
    for n in range(PER_PROC):
        acl_log.append_line(path, json.dumps({"who": index, "n": n, "diag": "x" * 400}))


def test_journal_processes_keep_every_line(tmp_path, busy_machine):
    path = tmp_path / "shared.jsonl"
    _run_processes(_journal_writer, str(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    got = {(r["who"], r["n"]) for r in map(json.loads, lines)}
    assert len(lines) == PROCS * PER_PROC
    assert got == {(i, n) for i in range(PROCS) for n in range(PER_PROC)}


def _ad_text_writer(start, index):
    _writer_progress("target-entry", -1)
    assert start.wait(60)
    for n in range(PER_PROC):
        ar.add(f"T{index}-{n}", f"Headline {index} {n}", f"Body {index} {n}")


def test_ad_text_processes_keep_every_text(busy_machine):
    _run_processes(_ad_text_writer)
    assert {t.name for t in ar.all_texts()} == {
        f"T{i}-{n}" for i in range(PROCS) for n in range(PER_PROC)}


class FileBackend:
    """Библиотека в файлах без проверки sha: последняя запись побеждает.

    Так от потерянной правки защищает только `team_library._sync_transaction`
    — его замок и проверяется."""

    def __init__(self, root: str):
        self.root = Path(root)

    def get(self, path: str) -> tl.Remote:
        f = self.root / path
        if not f.exists():
            return tl.Remote(None, None)
        text = f.read_text(encoding="utf-8")
        return tl.Remote(text, hashlib.sha1(text.encode("utf-8")).hexdigest())

    def put(self, path: str, text: str, sha, message: str) -> str:
        time.sleep(0.002)  # окно между чтением и записью, как у сети
        paths._atomic_write(self.root / path, text.encode("utf-8"))
        return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _library_writer(start, index, root):
    _writer_progress("target-entry", -1)
    tl.use_backend(FileBackend(root))
    assert start.wait(60)
    for n in range(PER_PROC):
        _writer_progress("before-write", n)
        ar.add(f"L{index}-{n}", f"Headline {index} {n}", f"Body {index} {n}")
        _writer_progress("after-write", n)


def test_team_library_processes_keep_every_text(tmp_path, busy_machine):
    root = tmp_path / "library"
    root.mkdir()
    (root / tl.FILES["ad_texts"][0]).write_text(tl._dump(tl.empty("ad_texts")), encoding="utf-8")
    _run_processes(_library_writer, str(root))
    data = json.loads((root / tl.FILES["ad_texts"][0]).read_text(encoding="utf-8"))
    assert set(data["texts"]) == {f"L{i}-{n}" for i in range(PROCS) for n in range(PER_PROC)}
