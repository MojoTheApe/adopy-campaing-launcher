"""Synthetic transport, real process/profile/config and production coordinator.

Shared only by controlled test people; no browser, Graph, MLX or employee data.
The test injects a unique coordination root, never a production root override.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def write(path, value):
    from config.paths import _replace_when_free

    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    _replace_when_free(temporary, path)


def main():
    import psutil
    from config.paths import machine_file_lock
    from src.analysis_agent import account_refresh as ar, account_refresh_state as ars
    from src.analysis_agent import account_spend_api as api, campaign_inventory as ci
    from src.analysis_agent.account_refresh_machine import MachineStore
    from src.fb_ads_rpa import profile_lease
    from tests.profile_lease_fakes import fake_profile

    settings = json.loads(Path(sys.argv[1]).read_text())
    label = sys.argv[2]
    ipc = Path(settings['ipc'])
    status = ipc / (label + '.json')
    common = Path(settings['machine'])
    config = ((Path(os.environ['LOCALAPPDATA']) / ('acl2063-' + settings['run'] + '-' + label))
              if os.name == 'nt' else ipc / ('private-' + label))
    config.mkdir(parents=True, exist_ok=True)
    os.environ['ACL_CONFIG_DIR'] = str(config)
    os.environ['ACL_AUTO_REFRESH'] = '1'
    # Different homes/physical lease domains are intentional: only the new
    # machine interlock, not an accidentally shared per-user lease, can protect.
    profile_lease._lease_root = lambda: config / 'leases'
    ars.machine_root = lambda: common
    api._open_session = lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected live session'))
    members = settings['maps'][label]
    def save_map():
        rows = ['account_name,ad_account_id,profile_id']
        for account in members:
            number = {'A': '12345678', 'B': '22345678', 'C': '32345678'}[account]
            rows.append(f'{account},act_{number},{fake_profile("machine-" + account)}')
        (config / 'ad_account_map.csv').write_text('\n'.join(rows) + '\n')
    save_map()
    private = config / 'private-sentinel.json'
    private.write_text('{"synthetic_private": "unchanged"}')
    private_sha = hashlib.sha256(private.read_bytes()).hexdigest()
    stamp = datetime(2026, 10, 24, 8).astimezone()
    mode = ['ok']
    def reader(account, trigger):
        trace = ipc / 'trace.json'
        with machine_file_lock(ipc / 'trace.lock', timeout=10):
            value = json.loads(trace.read_text()) if trace.exists() else {'active': 0, 'max': 0, 'reads': []}
            assert value['active'] == 0, 'two actual machine readers overlapped'
            value['active'] += 1
            value['max'] = max(value['max'], value['active'])
            value['reads'].append([label, account, trigger])
            write(trace, value)
        time.sleep(.04)
        with machine_file_lock(ipc / 'trace.lock', timeout=10):
            value = json.loads(trace.read_text()); value['active'] -= 1; write(trace, value)
        if mode[0] == 'checkpoint':
            return ci.Snapshot(account=account, state='failed', stop='checkpoint',
                               finished_at_utc=stamp.isoformat(), error='synthetic checkpoint')
        return ci.Snapshot(account=account, state='empty', finished_at=stamp.strftime('%Y-%m-%d %H:%M:%S'),
                           finished_at_utc=stamp.isoformat(), steps={s: 'ok' for s in ci.STRUCTURE_STEPS})
    queue = ar.RefreshQueue(reader, clock=lambda: stamp.timestamp(), store=ars.RefreshStore(config),
                            accounts=lambda: list(members), machine=MachineStore(common))
    process = psutil.Process()
    identity = {'pid': os.getpid(), 'created_at': process.create_time(), 'username': process.username(),
                'profile': str(Path.home()), 'config': str(config), 'machine': str(common)}
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        token = wintypes.HANDLE()
        adv = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        adv.GetTokenInformation.restype = wintypes.BOOL
        assert adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token))
        try:
            size = wintypes.DWORD()
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            assert adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size))
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
            text = wintypes.LPWSTR()
            adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
            assert adv.ConvertSidToStringSidW(sid, ctypes.byref(text))
            identity['sid'] = text.value
            kernel.LocalFree.argtypes = [ctypes.c_void_p]; kernel.LocalFree(text)
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle(token)
        identity['admin'] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        if settings.get('require_standard'):
            assert not identity['admin'], 'synthetic people must run as standard users'
        assert str(config).lower().startswith(os.environ['LOCALAPPDATA'].lower())
    sequence = 0
    write(status, {**identity, 'phase': 'prepared', 'sequence': sequence})
    try:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            command = ipc / (label + '.command.json')
            value = json.loads(command.read_text()) if command.exists() else {}
            if value.get('sequence', 0) <= sequence:
                time.sleep(.02); continue
            sequence = value['sequence']; action = value['action']; extra = {}
            if action == 'crash':
                from src.analysis_agent.account_refresh_machine import binding
                with ars.read_session():
                    queue._machine.begin(binding('B'), queue._owner, 'retry')
                    write(status, {**identity, 'phase': 'crashed', 'sequence': sequence})
                    os._exit(77)  # own process only; kernel leases must release
            if action == 'exit':
                write(status, {**identity, 'phase': 'exited', 'sequence': sequence}); return
            if action == 'schedule':
                extra['admitted'] = queue.page_open(now=stamp, port=9090)
            elif action == 'drain':
                extra['reads'] = queue._drain_once()
            elif action in ('checkpoint', 'retry'):
                mode[0] = 'checkpoint' if action == 'checkpoint' else 'ok'
                queue.request('B', 'retry', force=True)
                extra['reads'] = queue._drain_once()
                mode[0] = 'ok'
            elif action == 'restart':
                queue = ar.RefreshQueue(reader, clock=lambda: stamp.timestamp(), store=ars.RefreshStore(config),
                                        accounts=lambda: list(members), machine=MachineStore(common))
            elif action == 'deny':
                from src.analysis_agent.account_refresh_machine import CoordinationError
                try:
                    MachineStore(ipc / 'unsupported-permissions').halt()
                except CoordinationError:
                    extra['unsupported_permissions_refused'] = True
                else:
                    raise AssertionError('unsupported permissions were not refused')
            elif action != 'state':
                raise AssertionError(action)
            snapshot = queue.state()
            visible = {a: {'snapshot': ci.get_snapshot(a) is not None, 'attempt': ci.get_attempt(a)} for a in members}
            assert hashlib.sha256(private.read_bytes()).hexdigest() == private_sha
            write(status, {**identity, 'phase': 'ready', 'sequence': sequence, 'state': snapshot,
                           'visible': visible, 'private_unchanged': True, **extra})
        raise TimeoutError('controlled person lifecycle exceeded its bound')
    except BaseException:
        write(status, {**identity, 'phase': 'error', 'sequence': sequence, 'error': traceback.format_exc()})
        raise


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        # Scheduled tasks do not attach a console. Preserve even an early
        # import/token/config failure, rather than leaving a lone exit code.
        if len(sys.argv) > 2:
            settings = json.loads(Path(sys.argv[1]).read_text())
            path = Path(settings['ipc']) / (sys.argv[2] + '.json')
            previous = json.loads(path.read_text()) if path.exists() else {}
            write(path, {**previous, 'phase': 'error', 'error': traceback.format_exc()})
        raise
