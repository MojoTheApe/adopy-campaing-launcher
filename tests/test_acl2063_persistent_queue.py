"""Полная очередь использует общий файл; медленный профиль не запирает HTTP-статус."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from src.analysis_agent import account_refresh as ar
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_state import RefreshStore


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    monkeypatch.setattr(ci, "get_snapshot", lambda account: None)
    monkeypatch.setattr(ci, "get_attempt", lambda account: None)
    monkeypatch.setattr(ci, "latest_stops", lambda: {})


def test_instances_share_priority_dedup_results_and_restart(tmp_path):
    seen = []

    def read(account, trigger):
        seen.append((account, trigger))
        return ci.Snapshot(account=account, state="empty", finished_at="08:01")

    def new():
        return ar.RefreshQueue(reader=read, store=RefreshStore(tmp_path), accounts=lambda: ["A", "B"])

    first, second = new(), new()
    first.request("B", "schedule")
    second.request("B", "attention")
    second.request("A", "action", force=True)
    assert first._drain_once() == 2
    assert seen == [("A", "action"), ("B", "attention")]
    assert second._drain_once() == 0
    restarted = new().state()
    assert restarted["queued"] == [] and restarted["running"] is None
    assert restarted["rows"]["A"]["list_at"] == "08:01"


def test_live_read_keeps_state_and_priority_request_available(tmp_path):
    began, release, requested = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def read(account, trigger):
        seen.append(account)
        began.set()
        assert release.wait(3)
        return ci.Snapshot(account=account, state="ok", finished_at="08:01")

    first = ar.RefreshQueue(reader=read, store=RefreshStore(tmp_path))
    second = ar.RefreshQueue(reader=read, store=RefreshStore(tmp_path))
    first.request("A", "schedule")
    worker = threading.Thread(target=first._drain_once)
    worker.start()
    try:
        assert began.wait(3)
        assert second.state()["running"] == "A"

        def request():
            second.request("B", "action", force=True)
            requested.set()

        caller = threading.Thread(target=request)
        caller.start()
        assert requested.wait(3), "A slow profile must not hold the state-file lock"
        caller.join(3)
        assert second._drain_once() == 0
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and seen == ["A", "B"]
    assert second.state()["running"] is None


def test_removed_account_does_not_open_or_erase_inventory(tmp_path):
    accounts, seen = ["A"], []
    q = ar.RefreshQueue(reader=lambda a, t: seen.append(a), store=RefreshStore(tmp_path),
                        accounts=lambda: list(accounts))
    q.request("A", "schedule")
    accounts.clear()
    assert q._drain_once() == 0
    assert q.request("A", "retry", force=True) == {"queued": False, "reason": "removed"}
    assert seen == []


def test_corrupt_state_refuses_without_resetting_a_recorded_window(tmp_path):
    store = RefreshStore(tmp_path)
    store.path.write_text('{"windows_done":', encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be read"):
        ar.RefreshQueue(store=store).request("A", "schedule")
    assert store.path.read_text() == '{"windows_done":'


_CHILD = r'''
import json,sys,time
from pathlib import Path
from src.analysis_agent.account_refresh import RefreshQueue
from src.analysis_agent.account_refresh_state import RefreshStore
from src.analysis_agent.campaign_inventory import Snapshot
root=Path(sys.argv[1]);events=root/'events.jsonl'
def read(account,trigger):
    with events.open('a') as f:f.write(json.dumps(['start',account])+'\n')
    time.sleep(.05)
    with events.open('a') as f:f.write(json.dumps(['end',account])+'\n')
    return Snapshot(account=account,state='empty',finished_at='08:01')
RefreshQueue(reader=read,store=RefreshStore(root),accounts=lambda:['A','B'])._drain_once()
'''


def test_two_real_processes_share_one_dispatcher_and_do_not_duplicate(tmp_path):
    q = ar.RefreshQueue(reader=lambda a, t: None, store=RefreshStore(tmp_path))
    q.request("A", "schedule")
    q.request("B", "schedule")
    env = {**os.environ, "ACL_CONFIG_DIR": str(tmp_path), "ACL_FB_BACKEND": "rpa",
           "ACL_AUTO_REFRESH": "0"}
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(tmp_path)],
                             cwd=Path(__file__).resolve().parents[1], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=20)
            assert proc.returncode == 0, stdout + stderr
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events == [["start", "A"], ["end", "A"], ["start", "B"], ["end", "B"]]
    assert q.state()["running"] is None and q.state()["queued"] == []


_WINDOW_CHILD = r'''
import json,sys
from pathlib import Path
from datetime import datetime
from src.analysis_agent.account_refresh import RefreshQueue
from src.analysis_agent.account_refresh_state import RefreshStore
root=Path(sys.argv[1]); now=datetime(2026,10,24,8).astimezone()
from src.analysis_agent import account_refresh_state as ars
ars.machine_root=lambda:root/'machine'
q=RefreshQueue(store=RefreshStore(root),accounts=lambda:['A','B'],clock=lambda:now.timestamp())
print(json.dumps(q.page_open(now=now,port=9095)))
'''


def test_two_actual_dashboard_processes_admit_only_one_window(tmp_path):
    from tests.profile_lease_fakes import fake_profile
    (tmp_path / 'ad_account_map.csv').write_text('account_name,ad_account_id,profile_id\n' +
        f'A,act_12345678,{fake_profile("machine-A")}\nB,act_22345678,{fake_profile("machine-B")}\n')
    env={**os.environ, 'ACL_AUTO_REFRESH':'1', 'ACL_CONFIG_DIR':str(tmp_path)}
    procs=[subprocess.Popen([sys.executable,'-c',_WINDOW_CHILD,str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    results=[]
    try:
        for proc in procs:
            out,err=proc.communicate(timeout=20)
            assert proc.returncode==0,err
            results.append(json.loads(out))
    finally:
        for proc in procs:
            if proc.poll() is None:proc.kill();proc.wait()
    assert sorted(r['sweep'] for r in results)==['already_done','started']
    state=RefreshStore(tmp_path).read()
    assert len(state['entries'])==2 and state['windows_done']=={'2026-10-24':['08:00']}


_READ_CHILD = r'''
import json,sys,time
from pathlib import Path
from src.fb_ads_rpa import profile_lease
from src.analysis_agent.account_refresh_state import read_session
root=Path(sys.argv[1]);label=sys.argv[2]
profile_lease._lease_root=lambda:root/'leases'
from src.analysis_agent import account_refresh_state as ars
ars.machine_root=lambda:root/'machine'
with profile_lease.profile_lock('11111111-1111-1111-1111-'+('111111111111' if label=='A' else '222222222222')):
 with read_session(priority=label=='B'):
  with (root/'events.jsonl').open('a') as f:f.write(json.dumps(['start',label])+'\n')
  time.sleep(.1)
  with (root/'events.jsonl').open('a') as f:f.write(json.dumps(['end',label])+'\n')
'''


def test_command_and_schedule_read_sessions_share_one_machine_lease(tmp_path):
    procs=[subprocess.Popen([sys.executable,'-c',_READ_CHILD,str(tmp_path),a],
        cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env={**os.environ,'ACL_CONFIG_DIR':str(tmp_path/a)}) for a in ['A','B']]
    try:
        for proc in procs:
            out,err=proc.communicate(timeout=20)
            assert proc.returncode==0,out+err
    finally:
        for proc in procs:
            if proc.poll() is None:proc.kill();proc.wait()
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    first=events[0][1]; second='B' if first=='A' else 'A'
    assert events==[['start',first],['end',first],['start',second],['end',second]]
