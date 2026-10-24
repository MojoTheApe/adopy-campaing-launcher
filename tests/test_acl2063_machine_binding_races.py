"""The actual session, not a later/current map, identifies a relayed result."""
from contextlib import contextmanager
import pytest
from types import SimpleNamespace

from src.analysis_agent import account_spend_api as api, campaign_inventory as ci
from src.analysis_agent.account_refresh_machine import MachineStore, binding
from tests.profile_lease_fakes import fake_profile


def world(monkeypatch):
    profile = fake_profile('binding-race')
    entry = SimpleNamespace(ad_account_id='act_12345678', profile_id=profile, folder_id='', multilogin_instance='A')
    monkeypatch.setattr(ci._as, '_entry_for', lambda a: entry)
    monkeypatch.setattr(ci, 'backend_ready', lambda: True)
    return entry, profile


def test_act_change_before_session_resolution_refuses_before_external_open(monkeypatch):
    entry, profile = world(monkeypatch)
    old = binding('A')
    original = api._session
    opened = []
    @contextmanager
    def change_before_resolve(*a, **kw):
        entry.ad_account_id = 'act_22345678'
        with original(*a, **kw) as session:
            yield session
    monkeypatch.setattr(api, '_session', change_before_resolve)
    monkeypatch.setattr(api, '_warm_registry', lambda: opened.append('warm-check'))
    monkeypatch.setattr(api, '_open_gated', lambda *a: opened.append('open'))
    monkeypatch.setattr(ci, 'read_into', lambda *a, **k: opened.append('read'))
    result = ci.run_read('A', trigger='schedule')
    assert opened == []
    assert result.state == 'failed'
    peer = MachineStore().result(old)
    assert peer['payload']['state'] == 'failed' and peer['payload']['campaigns'] == []
    assert MachineStore().result(binding('A')) is None


@pytest.mark.parametrize('entrypoint', ['cold', 'inline'])
def test_map_change_after_actual_read_keeps_outcome_only_for_actual_old_target(monkeypatch, entrypoint):
    entry, profile = world(monkeypatch)
    old = binding('A')
    @contextmanager
    def opened(*a, **k):
        yield SimpleNamespace(act='act_12345678', profile_id=profile,
                              client=SimpleNamespace(calls=0, stopped=None), notes={})
    monkeypatch.setattr(api, '_open_session', opened)
    def read(snap, client, act, **kw):
        assert act == 'act_12345678'
        snap.campaigns = [{'id': 'old-target-only'}]
        snap.steps = {s: 'ok' for s in ci.STRUCTURE_STEPS}
        entry.ad_account_id = 'act_22345678'
    monkeypatch.setattr(ci, 'read_into', read)
    if entrypoint == 'cold':
        result = ci.run_read('A', trigger='schedule')
    else:
        from src.fb_ads_rpa.profile_lease import profile_lock
        with profile_lock(profile), opened() as session:
            result = ci.read_in_session('A', session, trigger='action')
    assert result.error_code == 'account_map_changed'
    assert MachineStore().result(old)['payload']['campaigns'] == [{'id': 'old-target-only'}]
    assert MachineStore().result(binding('A')) is None
    assert ci.get_snapshot('A') is None


from tests.test_acl2063_machine_queue import dashboards, NOW


@pytest.mark.parametrize('latest_failed', [False, True])
def test_consumer_map_change_after_prefetch_never_relabels_either_snapshot(dashboards, monkeypatch, latest_failed):
    select, new, reads = dashboards
    first = new('one')
    first.page_open(now=NOW, port=9090); first._drain_once()
    if latest_failed:
        first.record_result(ci.Snapshot(account='A', state='failed', error='synthetic failure'), trigger='action')
    select('two'); second = new('two')
    entry = SimpleNamespace(ad_account_id='act_12345678', profile_id=fake_profile('machine-A'))
    monkeypatch.setattr(ci._as, '_entry_for', lambda a: entry)
    original = second._machine.results
    def change_after_prefetch(keys):
        result = original(keys)
        entry.ad_account_id = 'act_22345678'
        return result
    monkeypatch.setattr(second._machine, 'results', change_after_prefetch)
    second.state()
    assert ci.get_snapshot('A') is None and ci.get_attempt('A') is None
    assert 'A' not in ci._load_all(), 'neither prior good nor latest failed payload may be relabelled'


def test_resolved_foreign_session_cannot_read_or_relay_under_old_key(monkeypatch):
    entry, profile = world(monkeypatch)
    old = binding('A')
    original = api._session
    @contextmanager
    def changed(*a, **kw):
        entry.ad_account_id = 'act_22345678'
        with original(*a, **kw) as active:
            yield active
    @contextmanager
    def resolved(*a, **kw):
        yield SimpleNamespace(act=entry.ad_account_id, profile_id=profile,
                              client=SimpleNamespace(calls=0, stopped=None), notes={})
    monkeypatch.setattr(api, '_session', changed)
    monkeypatch.setattr(api, '_open_session', resolved)
    calls = []
    def read(snap, client, act, **kw):
        calls.append(act)
        snap.campaigns = [{'id': 'foreign-act-only'}]
        snap.steps = {s: 'ok' for s in ci.STRUCTURE_STEPS}
    monkeypatch.setattr(ci, 'read_into', read)
    ci.run_read('A', trigger='schedule')
    assert calls == [], 'an actual session of another account must be refused before Graph reads'
    assert MachineStore().result(old)['payload']['campaigns'] == []
