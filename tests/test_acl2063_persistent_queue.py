"""Полная очередь использует общий файл; медленный профиль не запирает HTTP-статус."""
import copy
import hashlib
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
import json,os,sys,time
from pathlib import Path
from datetime import datetime
from src.analysis_agent.account_refresh import RefreshQueue
from src.analysis_agent.account_refresh_state import RefreshStore
root=Path(sys.argv[1]); label,schedule=sys.argv[2:]; now=datetime(2026,10,24,8).astimezone()
from src.analysis_agent import account_refresh_state as ars
ars.machine_root=lambda:root/'machine'
q=RefreshQueue(store=RefreshStore(root),accounts=lambda:['A','B'],clock=lambda:now.timestamp())
admissions=[]; admit=q._machine.admit; barriers=root/'barriers'
def wait(name):
    deadline=time.monotonic()+10
    while not (barriers/name).exists():
        if time.monotonic()>=deadline:raise TimeoutError('Dashboard barrier: '+name)
        time.sleep(.01)
def controlled_admit(window,keys,owner):
    if label=='second':wait('first-admitted')
    started=admit(window,keys,owner)
    admissions.append(dict(started=started,window=window,keys=keys,owner=owner))
    (barriers/(label+'-admitted')).touch()
    if label=='first':wait('second-admitted' if schedule=='normal' else 'second-finished')
    elif schedule=='normal':wait('first-finished')
    return started
q._machine.admit=controlled_admit
result=q.page_open(now=now,port=9095)
(barriers/(label+'-finished')).touch()
print(json.dumps(dict(label=label,pid=os.getpid(),machine_root=str(q._machine.root.resolve()),
                      admissions=admissions,result=result)))
'''


def _dashboard_window_observation(root, schedule):
    from tests.profile_lease_fakes import fake_profile
    from src.analysis_agent.account_refresh_machine import binding_values
    root.mkdir()
    (root / 'barriers').mkdir()
    (root / 'ad_account_map.csv').write_text('account_name,ad_account_id,profile_id\n' +
        f'A,act_12345678,{fake_profile("machine-A")}\nB,act_22345678,{fake_profile("machine-B")}\n')
    env={**os.environ, 'ACL_AUTO_REFRESH':'1', 'ACL_CONFIG_DIR':str(root)}
    procs=[subprocess.Popen([sys.executable,'-c',_WINDOW_CHILD,str(root),label,schedule],
        cwd=Path(__file__).resolve().parents[1],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for label in ('first','second')]
    results=[]
    try:
        for proc in procs:
            out,err=proc.communicate(timeout=20)
            assert proc.returncode==0,err
            results.append(json.loads(out))
    finally:
        for proc in procs:
            if proc.poll() is None:proc.kill();proc.wait()
    observation=dict(root=str(root.resolve()),schedule=schedule,results=results,
        state=RefreshStore(root).read(),machine=json.loads((root/'machine/state.json').read_text()),
        bindings={'A':binding_values('act_12345678',fake_profile('machine-A')),
                  'B':binding_values('act_22345678',fake_profile('machine-B'))})
    print('Real dashboard window: '+json.dumps(observation,sort_keys=True))
    return observation


@pytest.fixture(scope='module')
def dashboard_window_observations(tmp_path_factory):
    root=tmp_path_factory.mktemp('dashboard_window_orders')
    return {schedule:_dashboard_window_observation(root/schedule,schedule)
            for schedule in ('normal','reverse')}


def _assert_one_dashboard_window(observation):
    root=Path(observation['root']); results=observation['results']
    state,machine,bindings=observation['state'],observation['machine'],observation['bindings']
    owner=hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    tag='2026-10-24/08:00'
    assert observation['schedule'] in ('normal','reverse')
    assert set(bindings)=={'A','B'} and len(set(bindings.values()))==2
    assert len(results)==2 and {r['label'] for r in results}=={'first','second'}
    assert len({r['pid'] for r in results})==2
    by_label={r['label']:r for r in results}
    for label,row in by_label.items():
        assert row['machine_root']==str((root/'machine').resolve())
        assert len(row['admissions'])==1
        admission=row['admissions'][0]
        assert admission['started'] is (label=='first')
        assert admission['owner']==owner and admission['window']==tag
        assert len(admission['keys'])==2 and set(admission['keys'])==set(bindings.values())
    expected={'first':('started',2),'second':('already_done',0)} if observation['schedule']=='normal' else {
        'first':('already_done',0),'second':('joined',2)}
    assert {label:row['result'] for label,row in by_label.items()}=={
        label:dict(sweep=sweep,queued=queued,window='08:00') for label,(sweep,queued) in expected.items()}
    assert state['windows_done']=={'2026-10-24':['08:00']}
    assert state['machine_members']=={tag:bindings} and state['machine_bindings']==bindings
    assert set(state['entries'])=={'A','B'} and state['seq']==2
    assert sorted(v[1] for v in state['entries'].values())==[1,2]
    assert all(len(v)==4 and v[2]==f'schedule:{tag}' and v[3] is False for v in state['entries'].values())
    assert state['running'] is None
    assert state['sweep']=={'window':tag,'done':0,'total':2,'_pending':['A','B']}
    assert set(machine['windows'])=={tag} and set(machine['windows'][tag])==set(bindings.values())
    assert all(member=={'owners':[owner],'done':None} for member in machine['windows'][tag].values())
    assert set(machine['requests'])==set(bindings.values())
    assert all(set(request['owners'])=={owner} for request in machine['requests'].values())
    assert all(request['owners'][owner]['trigger']==f'schedule:{tag}' and
               request['owners'][owner]['force'] is False for request in machine['requests'].values())
    assert machine['running'] is None and machine['results']=={}


@pytest.mark.parametrize('schedule',['normal','reverse'])
def test_two_actual_dashboard_processes_admit_only_one_window(dashboard_window_observations,schedule):
    observation=dashboard_window_observations[schedule]
    _assert_one_dashboard_window(observation)


@pytest.mark.parametrize('case',[
    'double-admission','missing-binding','duplicate-binding','missing-entry','duplicate-entry',
    'wrong-machine-domain','wrong-owner-domain','duplicate-owner','second-window',
    'missing-window-done','duplicate-window-done','missing-request','second-start',
])
def test_dashboard_window_invariants_reject_invalid_observations(dashboard_window_observations,case):
    # Corrupt a real reverse-schedule observation; valid results alone cannot hide
    # duplicate/missing persisted work or a different isolation domain.
    observation=copy.deepcopy(dashboard_window_observations['reverse'])
    first,second=observation['results']
    tag='2026-10-24/08:00'
    key=observation['bindings']['B']
    if case=='double-admission':second['admissions'][0]['started']=True
    elif case=='missing-binding':del observation['machine']['windows'][tag][key]
    elif case=='duplicate-binding':second['admissions'][0]['keys']=[key,key]
    elif case=='missing-entry':del observation['state']['entries']['B']
    elif case=='duplicate-entry':observation['state']['entries']['duplicate-A']=observation['state']['entries']['A']
    elif case=='wrong-machine-domain':second['machine_root']=str(Path(observation['root'])/'other-machine')
    elif case=='wrong-owner-domain':first['admissions'][0]['owner']='0'*64
    elif case=='duplicate-owner':observation['machine']['windows'][tag][key]['owners']*=2
    elif case=='second-window':observation['machine']['windows']['2026-10-24/13:00']={}
    elif case=='missing-window-done':observation['state']['windows_done']={}
    elif case=='duplicate-window-done':observation['state']['windows_done']['2026-10-24'].append('08:00')
    elif case=='missing-request':del observation['machine']['requests'][key]
    elif case=='second-start':second['result']['sweep']='started'
    with pytest.raises(AssertionError):
        _assert_one_dashboard_window(observation)


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
