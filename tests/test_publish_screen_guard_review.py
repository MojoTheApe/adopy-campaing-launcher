"""Behavioral regressions for the publish-job screen guard (offline)."""
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.web import keep_awake as ka
from src.web import publish_jobs as pj
from src.fb_ads_rpa import run_telemetry as rt

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(reason="real Windows session and screen APIs (ProcessIdToSessionId, keep-awake)")


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setattr(pj, 'use_rpa_publish', lambda: True)
    monkeypatch.setattr(pj, '_clear_inherited_asyncio_loop', lambda: None)
    monkeypatch.setattr(pj, '_write_draft_status', lambda *a, **kw: None)
    monkeypatch.setattr(rt, 'screen_locked', lambda: None)
    # ACL-1603: первому профилю тоже нужен запас памяти; своя память — не хоста.
    from src.web import profile_slots as ps
    monkeypatch.setattr(pj, '_slots', ps.ProfileSlots(
        memory=lambda: ps.MemoryReading(total_mb=65536, available_mb=49152)))
    key = pj.create_job('Offline', 'Test', [{}])
    yield key
    with pj._jobs_lock:
        pj._jobs.pop(key, None)


def run_job(key):
    pj._run_job(key, [{}], account_label='Offline', country='Test',
                via_multilogin=True, activate=False)


def test_watcher_start_failure_does_not_strand_the_job(job, monkeypatch):
    def fail(*a):
        raise RuntimeError('cannot start new thread')
    monkeypatch.setattr(threading.Thread, 'start', fail)
    calls = []
    def run(key, *a, **kw):
        calls.append(key)
        pj._finish(key)
    monkeypatch.setattr(pj, '_run_rpa', run)
    run_job(job)
    assert calls == [job]
    assert pj.get_job(job)['status'] == 'done'


@pytest.mark.parametrize('terminal', ['done', 'cancelled', 'exited'])
def test_delayed_probe_cannot_change_a_finished_job(job, monkeypatch, terminal):
    entered, release = threading.Event(), threading.Event()
    def probe():
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(rt, 'screen_locked', probe)
    with pj._jobs_lock:
        pj._jobs[job]['status'] = 'running'
    try:
        with pj._watch_screen_lock(job):
            assert entered.wait(5)
            if terminal != 'exited':
                with pj._jobs_lock:
                    pj._jobs[job]['status'] = terminal
                release.set()
                for t in threading.enumerate():
                    if t.name == 'screenlock-' + job[:8]: t.join(5)
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name == 'screenlock-' + job[:8]: t.join(5)
    assert not pj.get_job(job).get('screen_was_locked')


@pytest.mark.parametrize('outcome', ['success', 'fatal', 'cancelled', 'exception'])
def test_real_runner_holds_guard_inside_queue_and_releases(job, monkeypatch, outcome):
    acquired, calls = threading.Event(), []
    gate = threading.Lock()
    gate.acquire()
    # С acl-1210 очередь берётся по ключу профиля; подставной замок — один
    # на любой ключ, чтобы проверка порядка осталась той же.
    monkeypatch.setattr(pj, '_publish_serial', lambda key: gate)
    def set_state(flags):
        calls.append((flags, threading.get_ident()))
        if flags == ka._BUSY: acquired.set()
        return True
    monkeypatch.setattr(ka, '_set', set_state)
    errors = []
    def run(key, *a, **kw):
        assert acquired.is_set()
        assert gate.locked()
        assert kw['activate'] is False and kw['hand_off'] is True
        if outcome == 'fatal': pj._fatal(key, 'synthetic failure')
        elif outcome == 'cancelled': pj._finish(key, cancelled=True)
        elif outcome == 'exception': raise RuntimeError('synthetic exception')
        else: pj._finish(key)
    monkeypatch.setattr(pj, '_run_rpa', run)
    def worker():
        try: run_job(job)
        except RuntimeError as e: errors.append(str(e))
    t = threading.Thread(target=worker)
    t.start()
    try:
        assert not acquired.wait(.1), 'queued job acquired display guard'
    finally:
        gate.release()
    t.join(5)
    assert not t.is_alive()
    assert [c[0] for c in calls] == [ka._BUSY, ka._IDLE]
    assert len({c[1] for c in calls}) == 1
    assert errors == (['synthetic exception'] if outcome == 'exception' else [])


def test_cancelled_queued_job_never_acquires_guard(job, monkeypatch):
    setter = Mock()
    runner = Mock()
    monkeypatch.setattr(ka, '_set', setter)
    monkeypatch.setattr(pj, '_run_rpa', runner)
    pj.cancel_job(job)
    run_job(job)
    assert pj.get_job(job)['status'] == 'cancelled'
    setter.assert_not_called()
    runner.assert_not_called()


@pytest.mark.parametrize('sessions,expected', [({20: 2}, False), ({20: 1}, True),
    ({20: None}, None), ({20: None, 21: 1}, True)])
def test_lock_is_scoped_to_our_windows_session(monkeypatch, sessions, expected):
    import os
    import psutil
    monkeypatch.setattr(rt.sys, 'platform', 'win32')
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [
        SimpleNamespace(pid=pid, info={'name': 'LogonUI.exe'}) for pid in sessions])
    monkeypatch.setattr(rt, '_process_session_id', lambda pid: 1 if pid == os.getpid() else sessions[pid])
    assert rt.screen_locked() is expected


def test_unknown_own_session_is_not_reported_as_locked(monkeypatch):
    import psutil
    monkeypatch.setattr(rt.sys, 'platform', 'win32')
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [SimpleNamespace(pid=20, info={'name': 'LogonUI.exe'})])
    monkeypatch.setattr(rt, '_process_session_id', lambda pid: None)
    assert rt.screen_locked() is None


@pytest.mark.parametrize('result,expected', [(1, 7), (0, None), ('error', None)])
def test_windows_session_api_success_refusal_and_error(monkeypatch, result, expected):
    import ctypes
    from ctypes import wintypes
    def query(pid, output):
        assert pid == 123
        if result == 'error': raise OSError('query failed')
        ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = 7
        return result
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(kernel32=SimpleNamespace(ProcessIdToSessionId=query)), raising=False)
    assert rt._process_session_id(123) == expected


@pytest.mark.skipif(sys.platform != 'win32', reason='real Windows APIs')
def test_real_windows_api_accepts_guard_and_reads_own_session():
    import os
    result = []
    def worker():
        with ka.keep_display_awake() as held:
            result.append(held)
        result.append(rt._process_session_id(os.getpid()))
    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert result[0] is True
    assert isinstance(result[1], int)
