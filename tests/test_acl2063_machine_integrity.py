"""Corrupt/unknown machine state refuses before profile transport or projection."""
import json

import pytest

from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_machine import MachineStore, CoordinationError, envelope, validate_envelope


def test_relay_only_contains_supported_structure_not_bi_or_credentials():
    snap = ci.Snapshot(account='private label', state='ok', steps={s: 'ok' for s in ci.STRUCTURE_STEPS},
                       campaigns=[{'id': '1', 'name': 'Campaign', 'spend': 99, 'token': 'synthetic-private',
                                   'adlabels': ['ACL'], 'acl_created': True}])
    payload = envelope(snap)
    assert payload['campaigns'] == [{'id': '1', 'name': 'Campaign', 'adlabels': ['ACL'], 'acl_created': True}]
    assert 'private label' not in json.dumps(payload)
    assert 'synthetic-private' not in json.dumps(payload)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(access_token='synthetic'),
    lambda p: p.update(state='ready'),
    lambda p: p.update(steps={}),
    lambda p: p.update(finished_at_utc='no timestamp'),
    lambda p: p['campaigns'].append({'id': '2', 'spend': 9}),
    lambda p: p['campaigns'].append({'id': '1'}),
    lambda p: p['campaigns'][0].update(daily_budget=float('nan')),
    lambda p: p['campaigns'][0].update(adlabels={'data': [{'name': 'ACL'}]}),
])
def test_malformed_relay_is_rejected(mutation):
    value = envelope(ci.Snapshot(account='A', state='ok', steps={s: 'ok' for s in ci.STRUCTURE_STEPS},
                                 campaigns=[{'id': '1'}]))
    mutation(value)
    with pytest.raises(CoordinationError):
        validate_envelope(value)


def test_missing_initialized_state_and_unknown_version_are_not_reset(tmp_path):
    store = MachineStore(tmp_path / 'machine')
    store.halt()
    original = store.path.read_bytes()
    store.path.unlink()
    with pytest.raises(CoordinationError, match='missing'):
        store.halt()
    assert not store.path.exists()
    raw = json.loads(original); raw['version'] = 2
    store.path.write_text(json.dumps(raw))
    with pytest.raises(CoordinationError, match='not supported'):
        store.halt()
    assert json.loads(store.path.read_text())['version'] == 2


def test_tampered_result_fails_closed_without_projection(dashboards):
    select, new, reads = dashboards
    from tests.test_acl2063_machine_queue import NOW
    first = new('one')
    first.page_open(now=NOW, port=9090); first._drain_once()
    raw = json.loads(first._machine.path.read_text())
    next(iter(raw['results'].values()))['payload']['campaigns'] = [{'id': 'injected'}]
    first._machine.path.write_text(json.dumps(raw))
    select('two'); second = new('two')
    with pytest.raises(CoordinationError, match='corrupt'):
        second.page_open(now=NOW, port=9095)
    assert ci.get_snapshot('A') is None and len(reads) == 1


from tests.test_acl2063_machine_queue import dashboards
