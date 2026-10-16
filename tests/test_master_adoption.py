"""Adoption must not mutate live tasks or authorize a partial/failed CI result."""
import base64
import copy
import json
import shutil
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts import acl_adoption

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts import acl_pr_check


@pytest.fixture
def prepared(tmp_path):
    (tmp_path / '.workflow/legacy').mkdir(parents=True)
    (tmp_path / 'tracker').mkdir()
    for name in ['.workflow/adoption.json', '.workflow/legacy/tracker-config.json', 'tracker/config.json']:
        (tmp_path / name).write_text((ROOT / name).read_text())
    path = tmp_path / '.workflow/adoption.json'
    state = json.loads(path.read_text())
    state.update(state='prepared', evidence={})
    path.write_text(json.dumps(state))
    return tmp_path


@pytest.mark.parametrize('args', [['work', 'ACL-1248'], ['set', 'ACL-1248', '--status', 'merged'],
                                ['record-review'], ['stage'], ['merged', 'ACL-1248'], ['init']])
def test_prepared_contract_cannot_mutate_live_tracker(prepared, args):
    with pytest.raises(ValueError, match='not active'):
        acl_adoption.guard_tracker(args, prepared)


def test_active_flag_without_cutover_evidence_is_not_enough(prepared):
    path = prepared / '.workflow/adoption.json'
    state = json.loads(path.read_text()); state['state'] = 'active'
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='evidence'):
        acl_adoption.guard_tracker(['work', 'ACL-1248'], prepared)


@pytest.mark.parametrize('client', ['scripts/task_tracker.py', '.workflow/tools/task_tracker.py'])
def test_both_client_entrypoints_stop_before_loading_engine(client, prepared):
    for name in ('scripts/task_tracker.py', 'scripts/acl_adoption.py', '.workflow/tools/task_tracker.py'):
        target = prepared / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    result = subprocess.run([sys.executable, str(prepared / client), 'set', 'ACL-1248', '--status', 'merged'],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'not active' in result.stderr


@pytest.mark.parametrize('arguments', [
    ['-m', 'tracker', 'set', '248', '--status', 'review'],
    ['scripts/acl_mark_merged.py', '248', '--pr', '999', '--apply', '--reviewed-complete'],
    ['scripts/acl_tracker.py', '--output', 'output/legacy-test']])
def test_retained_legacy_commands_cannot_bypass_new_policy(arguments):
    result = subprocess.run([sys.executable, *arguments], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'historical only' in result.stderr or 'retired' in result.stderr


@pytest.fixture
def bootstrap(prepared):
    legacy = json.loads((prepared / '.workflow/legacy/tracker-config.json').read_text())
    pr = {'number': 999, 'state': 'open', 'draft': True, 'title': '[ACL-1248] Adopt workflow',
          'body': 'Tracker issues: ACL-1248',
          'base': {'ref': 'master', 'sha': 'a' * 40, 'repo': {'full_name': acl_adoption.REPOSITORY}},
          'head': {'ref': 'codex/acl-1248-master-adoption', 'repo': {'full_name': acl_adoption.REPOSITORY}}}
    calls = []

    def request(path):
        calls.append(path)
        if '/pulls/' in path: return pr
        if '/contents/' in path:
            assert path.endswith('?ref=' + 'a' * 40)
            return {'content': base64.b64encode(json.dumps(legacy).encode()).decode()}
        if path.endswith('/issues/248'):
            return {'number': 248, 'state': 'open', 'labels': [{'name': x} for x in
                    ['status:in_progress', 'module:platform', 'priority:medium', 'type:improvement']]}
        raise AssertionError(path)

    return pr, legacy, request, calls


def test_bootstrap_checks_real_base_policy_without_claiming_admission(prepared, bootstrap):
    _, _, request, _ = bootstrap
    result = acl_pr_check.check(request, 999, prepared)
    assert result['issues'] == [248]
    assert 'linkage only' in result['policy']
    assert 'admission' not in result


@pytest.mark.parametrize('change', ['different-task', 'foreign-head', 'stage-base', 'changed-config'])
def test_bootstrap_exception_cannot_authorize_other_work(prepared, bootstrap, change):
    pr, legacy, request, _ = bootstrap
    if change == 'different-task': pr['body'] = 'Tracker issues: ACL-1240'
    if change == 'foreign-head': pr['head']['repo']['full_name'] = 'other/repo'
    if change == 'stage-base': pr['base']['ref'] = 'stage'
    if change == 'changed-config': legacy['project']['base_branch'] = 'unexpected'
    with pytest.raises(ValueError): acl_pr_check.check(request, 999, prepared)


def test_newest_failed_ci_is_not_hidden_by_older_success(prepared):
    def request(path):
        if '/pulls/' in path: return {'state': 'closed'}
        if '/branches/' in path: return {'commit': {'sha': 'b' * 40}}
        assert '/actions/runs?' in path
        rows = []
        for name in ['ci', 'windows-installer']:
            base = {'id': 1, 'path': f'.github/workflows/{name}.yml', 'head_branch': 'master',
                    'event': 'push', 'head_sha': 'b' * 40, 'status': 'completed', 'conclusion': 'success'}
            rows.append(base)
            if name == 'ci': rows.append(dict(base, id=2, conclusion='failure'))
        return {'workflow_runs': rows}
    problems = acl_adoption.remote_blockers(request, prepared)
    assert len(problems) == 1
    assert 'ci.yml' in problems[0]


def test_existing_ids_and_modules_are_preserved():
    old = json.loads((ROOT / '.workflow/legacy/tracker-config.json').read_text())
    new = json.loads((ROOT / 'tracker/config.json').read_text())
    assert new['project'] == dict(old['project'], base_branch='main')
    assert new['modules'] == old['modules']
    assert new['workflow'] == old['workflow'][:-1] + ['approved', 'merged']
    assert new['execution_policy'] == {'schema_version':3, 'implementation_limit':5, 'review_limit':5, 'batch_limit':5}


def test_five_review_policy_keeps_descriptor_engine_and_delivery_contract_consistent():
    from scripts import tracker_delivery_policy as rules
    config = json.loads((ROOT / 'tracker/config.json').read_text())
    descriptor = json.loads((ROOT / 'delivery.json').read_text())
    lock = json.loads((ROOT / 'tracker/engine.json').read_text())
    assert descriptor['workflow']['execution'] == rules.execution_policy(config)
    assert lock['revision'] == descriptor['tracker']['revision']
    assert 'ci_evidence_policy' not in config  # Exported support cannot opt ACL into the native V1 protocol.
    action = yaml.safe_load((ROOT / '.github/workflows/delivery-check.yml').read_text())
    steps = action['jobs']['descriptor']['steps']
    assert any(step.get('uses') == 'MojoTheApe/master-repo@' + descriptor['standard']['revision'] for step in steps)
    assert not descriptor['workflow']['staging'] and not rules.policy(config)['staging']['enabled']
    assert descriptor['workflow']['stage_branch'] == rules.policy(config)['staging']['branch'] == 'stage'
    assert 'stage' not in config['workflow']


def test_policy_candidate_cannot_select_its_own_new_admission_route(monkeypatch):
    candidate = json.loads((ROOT / 'tracker/config.json').read_text())
    base = copy.deepcopy(candidate)
    base['delivery_policy']['staging']['enabled'] = True
    base['workflow'].insert(-1, 'stage')
    repo = candidate['project']['repository']
    pr = {'base': {'sha': 'a' * 40, 'ref': 'stage'}}
    def request(path):
        if '/pulls/' in path: return pr
        assert path == f'repos/{repo}/contents/tracker/config.json?ref=' + 'a' * 40
        return {'content': base64.b64encode(json.dumps(base).encode()).decode(), 'encoding': 'base64'}
    monkeypatch.setattr(acl_pr_check.shared, 'check', lambda request, config, number: config)
    selected = acl_pr_check.check(request, 930)
    assert selected == base and selected != candidate


@pytest.mark.parametrize('workflow,success_fields', [
    ('ci.yml', ['LINUX_TESTS', 'WINDOWS_TESTS']), ('windows-installer.yml', ['INPUTS', 'INSTALLER'])])
@pytest.mark.skipif(sys.platform == 'win32', reason='Ubuntu job shell contract is tested in the Ubuntu tooling job')  # linux-ok: Linux-only
def test_ci_admission_runs_fail_closed(workflow, success_fields):
    import os
    data = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    script = data['jobs']['admission']['steps'][0]['run']
    # ACL-1649: ci.yml decides Linux and Windows separately (LINUX output next to WINDOWS).
    flags = {'LINUX': 'true', 'PROCESS_INSTRUCTIONS': 'success'} if workflow == 'ci.yml' else {}
    off = {'LINUX': 'false', 'PROCESS_INSTRUCTIONS': 'success'} if workflow == 'ci.yml' else {}
    success = {'SCOPE': 'success', 'WINDOWS': 'true', **flags, **{k: 'success' for k in success_fields}}
    metadata = {'SCOPE': 'success', 'WINDOWS': 'false', **off, **{k: 'skipped' for k in success_fields}}
    cases = [(success, True), (metadata, True), (dict(success, SCOPE='failure'), False),
             (dict(success, WINDOWS=''), False)]
    if workflow == 'ci.yml':
        only_linux = dict(success, WINDOWS='false', WINDOWS_TESTS='skipped')   # PR into main: 3.11 only
        cases += [(only_linux, True), (dict(success, LINUX=''), False), (dict(success, LINUX='garbage'), False),
                  (dict(metadata, LINUX_TESTS='success'), False), (dict(metadata, LINUX_TESTS='failure'), False),
                  (dict(only_linux, WINDOWS_TESTS='failure'), False), (dict(only_linux, LINUX_TESTS='failure'), False)]
        cases += [(dict(success, LINUX='false'), False)]        # Linux declared off but its job ran green: mismatch
    for field in success_fields:
        for failed in ['failure', 'cancelled', 'skipped']:
            cases.append((dict(success, **{field: failed}), False))
    for env, allowed in cases:
        result = subprocess.run(['bash', '-e', '-c', script], env=dict(os.environ, **env), capture_output=True)
        assert (result.returncode == 0) == allowed, env


def test_active_audit_reads_main_not_frozen_compatibility_master(prepared):
    path = prepared / '.workflow/adoption.json'
    state = json.loads(path.read_text())
    state.update(state='active', evidence={k: 'reviewed report' for k in acl_adoption.ACTIVATION_EVIDENCE})
    path.write_text(json.dumps(state))
    def request(path):
        if '/pulls/' in path: return {'state': 'closed'}
        if '/branches/' in path:
            assert path.endswith('/branches/main')
            return {'commit': {'sha': 'b' * 40}}
        assert '/actions/runs?' in path
        return {'workflow_runs': [
            {'id': i, 'path': f'.github/workflows/{name}.yml', 'head_branch': 'main',
             'event': 'push', 'head_sha': 'b' * 40, 'status': 'completed', 'conclusion': 'success'}
            for i, name in enumerate(('ci', 'windows-installer'))]}
    assert acl_adoption.remote_blockers(request, prepared) == []
    acl_adoption.guard_tracker(['work', 'ACL-1248'], prepared)
