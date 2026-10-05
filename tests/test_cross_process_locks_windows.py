"""Cross-process writes through the machine file lock, on Windows (ACL-1582).

On Windows `config.paths.machine_file_lock` is `msvcrt.locking` and the atomic
write ends in `os.replace`, which Windows refuses (WinError 5) while another
process holds the target open; ACL-1419 was that exact failure. On Linux the
same code is `fcntl.flock` and a POSIX rename that never refuses, so these two
real multi-process tests prove nothing there. The rest of both original files
stays on Linux.
"""
import multiprocessing
import time

import pytest

from src.analysis_agent import targeting_registry as tr, team_library as tl
from src.web import hidden_accounts as ha
from tests import targeting_worker

pytestmark = pytest.mark.windows_only(
    reason="msvcrt locking + os.replace sharing violations (ACL-1419)")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('ACL_CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(tr, '_store', lambda: tmp_path / 'targets.json')
    monkeypatch.delenv('ACL_LIBRARY_REPO', raising=False)
    monkeypatch.delenv('ACL_LIBRARY_TOKEN', raising=False)
    tl.use_backend(None)


def _process_updates(start, index):
    assert start.wait(20)
    for n in range(15):
        ha.set_hidden(f'{index}-{n}', True)


def test_processes_preserve_each_update():
    ctx = multiprocessing.get_context('spawn')
    start = ctx.Event()
    processes = [ctx.Process(target=_process_updates, args=(start, i)) for i in range(3)]
    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(45)
            assert process.exitcode == 0
        assert ha.load() == {f'{i}-{n}' for i in range(3) for n in range(15)}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join()


def test_multiple_processes_do_not_lose_writes():
    # ACL-1419: the worker sits in a light module (see tests/targeting_worker.py),
    # every worker is joined before its exit code is read, and a worker that
    # outlives the deadline is reported as a hang, not as `None == 0`.
    ctx = multiprocessing.get_context('spawn')
    workers = [ctx.Process(target=targeting_worker.add_eight, args=(str(tr._store()), i))
               for i in range(3)]
    for w in workers:
        w.start()
    deadline = time.monotonic() + 120
    for w in workers:
        w.join(max(0.0, deadline - time.monotonic()))
    hung = [w.name for w in workers if w.is_alive()]
    for w in workers:
        if w.is_alive():
            w.terminate()
            w.join(10)
    assert not hung, f'workers still running after 120 s: {hung}'
    assert [w.exitcode for w in workers] == [0, 0, 0]
    assert len(tr.all_targets()) == 24
