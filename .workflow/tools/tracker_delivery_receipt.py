#!/usr/bin/env python3
"""Resolve an authorized batch and record native verified delivery with a repo token.

Exported beside the portable admission helpers. This module cannot merge, deploy,
approve tasks or create authorization. Project-owned trusted workflows perform
delivery and emit the receipt only after their actual acceptance checks.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

try:
    from . import delivery_policy as rules
except ImportError:
    import tracker_delivery_policy as rules

MARKER = 'TRACKER_DELIVERY_RECEIPT '
WORKFLOW = re.compile(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml\Z')


class PendingDelivery(ValueError):
    """The authorized coordinator has not completed its main handoff yet."""


def github(path, method='GET', payload=None):
    command = ['gh', 'api', '--method', method, path]
    if payload is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, encoding='utf-8', capture_output=True, timeout=90)
    if (result.returncode and path.endswith('/logs')
            and 'pass --allow-escape-sequences' in result.stderr):
        # Recent gh versions reject colored Actions logs even when captured.
        # Opt in only for privately captured logs; never print terminal escapes.
        # Older gh versions need no new flag and retain their compatible path.
        result = subprocess.run(command + ['--allow-escape-sequences'], text=True,
                                encoding='utf-8', capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError('GitHub delivery request failed; inspect state before retrying')
    if path.endswith('/logs'):
        if len(result.stdout.encode()) > 8 * 1024 * 1024:
            raise ValueError('Native verification log exceeds proof bound')
        return result.stdout
    return json.loads(result.stdout) if result.stdout.strip() else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def configuration(request, repo, revision):
    root = 'repos/' + repo
    row = request(root + '/contents/tracker/config.json?ref=' + quote(revision, safe=''))
    from_json = rules.direct_module().unique_object
    config = json.loads(rules.file_bytes(request, root, row),object_pairs_hook=from_json)
    modern=rules.execution_policy(config)
    if config.get('project', {}).get('repository') != repo or not (rules.batch_mode(config) or modern and modern['schema_version']==4):
        raise ValueError('Use the configured official batch or modern direct project')
    return config


def state(request, repo):
    root = 'repos/' + repo
    row = request(root + '/contents/.tracker-state.json?ref=tracker-state')
    shared = json.loads(rules.file_bytes(request, root, row),object_pairs_hook=rules.direct_module().unique_object)
    rules.state_schema(shared)
    if shared.get('maintenance'):
        raise PendingDelivery('Delivery continuation is paused by owned maintenance; resume after cutover')
    return shared, row['sha']


def official_run(request, repo, run_id, revision):
    if type(run_id) is not int or run_id < 1:
        raise ValueError('A real native workflow run is required')
    run = request(f'repos/{repo}/actions/runs/{run_id}')
    if (run.get('head_sha') != revision or run.get('head_branch') != 'main'
            or run.get('event') not in ('push', 'workflow_dispatch', 'workflow_run')
            or run.get('head_repository', {}).get('full_name', '').lower() != repo.lower()
            or not WORKFLOW.fullmatch(run.get('path', ''))):
        raise ValueError('Native run must belong to this official main revision and trusted workflow')
    return run


def authorization(config, batch):
    auth = batch.get('delivery_request')
    if not auth:
        return None
    if (auth.get('schema_version') != 1 or type(auth.get('schema_version')) is not int
            or auth.get('state') != 'requested'
            or auth.get('batch_id') != batch.get('id') or auth.get('issues') != batch.get('issues')
            or auth.get('target') != rules.policy(config)['delivery_target']
            or rules.reconciled_request_policy(auth, batch) != rules.fingerprint(config)
            or not WORKFLOW.fullmatch(auth.get('workflow', ''))
            or not isinstance(auth.get('job'), str) or not auth['job'].strip()
            or not isinstance(auth.get('requested_at'), str)
            or not auth.get('actor') or not auth.get('session')):
        raise ValueError('Durable delivery authorization changed or belongs to another policy/target')
    return auth


def task_context(request, repo, config, shared, batch, merge_sha):
    """Validate against the exact snapshot that will supply the final CAS SHA."""
    section = rules.execution_state(shared, config)
    issues, members = batch.get('issues', []), batch.get('members', [])
    if (batch.get('state') != 'merged' or not issues or len(issues) != len(set(issues))
            or [m.get('issue') for m in members] != issues):
        raise ValueError('The unchanged completed batch must still own every delivery member')
    tasks = shared.get('delivery_workflow', {}).get('tasks', {})
    already_verified = True
    for number in issues:
        item = request(f'repos/{repo}/issues/{number}')
        labels = {x['name'] for x in item.get('labels', [])}
        row = tasks.get(str(number), {})
        effective, reconciled_scope = rules.reconciled_policy(row)
        member = next(m for m in members if m['issue'] == number)
        execution = section.get('tasks', {}).get(str(number), {})
        statuses = [s for s in labels if s.startswith('status:')]
        merged = ['status:' + (s if isinstance(s, str) else s['id']) for s in config['workflow']
                  if (s if isinstance(s, str) else s['phase']) == 'merged']
        if (statuses != merged or {'flag:archived', 'flag:blocked'} & labels or row.get('process')
                or item.get('state_reason') == 'not_planned' or 'pull_request' in item
                or effective != rules.fingerprint(config)
                or not rules.scope_matches(item, reconciled_scope or member.get('scope'), config)
                or execution.get('state') != 'merged' or execution.get('batch_id') != batch['id']
                or {s.get('merge_sha') for s in row.get('sources', [])} != {merge_sha}):
            raise PendingDelivery('Every frozen task needs its exact reviewed main merge receipt first')
        rules.require_completion_scope(item, config, row)
        verification = row.get('verification') or {}
        already_verified = already_verified and (row.get('state') == 'verified'
            and verification.get('result') == 'passed'
            and verification.get('target') == rules.policy(config)['delivery_target']
            and merge_sha in verification.get('merge_shas', []))
    return tasks, already_verified


SELECTIONS = {
    'task_context_policy': ('task_context', 'task_context', 'WORKFLOW.md'),
    'completion_policy': ('completion', 'completion_policy', 'DELIVERY.md'),
    'ci_retry_policy': ('ci_retry', 'ci_retry', 'VERIFICATION.md'),
}


def selection_config(config):
    """Validate the actual selections before comparing JSON identities/types."""
    if (not isinstance(config, dict) or type(config.get('schema_version')) is not int
            or config['schema_version'] not in (2, 3)):
        raise ValueError('Selection delivery needs an explicit valid project config')
    rules.execution_policy(config)
    rules.completion_policy(config)
    rules.context_module().policy(config)
    rules.retry_module().policy(config)
    rules.process_module().policy(config)
    if 'knowledge_capture_policy' in config:
        raise ValueError('Selection package continuation does not adopt modern capture')
    if 'ci_evidence_policy' in config:
        try:
            from .ci_evidence import policy
        except ImportError:
            from tracker_ci_evidence import policy
        policy(config)


def selection_delta(before, after, key):
    """A comparison, never an operative config derived by dropping a flag."""
    if key in before or key not in after:
        raise ValueError('Select exactly one previously absent policy')
    other = {name: value for name, value in after.items() if name != key}
    if digest(other) != digest(before):
        raise ValueError('Selection delivery cannot change existing configuration or policy')


def selection_descriptor(before, after, prior, selected, key):
    mirror = SELECTIONS[key][1]
    for value, config in ((before, prior), (after, selected)):
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 2 or value.get('profile') != 'package'
                or value.get('completion') != 'verified-stable-package'
                or not isinstance(value.get('project'), dict)
                or value.get('project', {}).get('repository') != 'https://github.com/' + config['project']['repository']
                or not isinstance(value.get('workflow'), dict)
                or value['workflow'].get('staging') is not False
                or value['workflow'].get('main_branch') != 'main'
                or digest(value['workflow'].get('execution')) != digest(rules.execution_policy(config))
                or digest(value['workflow'].get('required_checks')) != digest(rules.policy(config)['required_checks'])):
            raise ValueError('Selection descriptor must retain the actual package/execution/check contract')
        for field, (_, name, _) in SELECTIONS.items():
            if ((field in config) != (name in value['workflow'])
                    or digest(config.get(field)) != digest(value['workflow'].get(name))):
                raise ValueError('Selection descriptor/config mirrors differ')
    selection_delta(before['workflow'], after['workflow'], mirror)
    old, new = dict(before), dict(after)
    old.pop('workflow'); new.pop('workflow')
    if digest(old) != digest(new):
        raise ValueError('Selection delivery cannot change the descriptor outside its one policy')


def selection_tree(request, repo, baseline, merge_sha, prior, selected, key):
    """Complete immutable diff; docs remain independently reviewed source text."""
    process = rules.process_module()
    root = 'repos/' + repo
    before = process.entries(request, root, rules.tree(request, root, baseline))
    after = process.entries(request, root, rules.tree(request, root, merge_sha))
    document = 'docs/repository/' + SELECTIONS[key][2]
    allowed = {'delivery.json', 'tracker/config.json', 'docs/repository/TRACKER.md',
               document, '.workflow/template-base.json'}
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    if changed != allowed:
        raise ValueError('Selection delivery needs its exact generated governance-only diff; runtime/pin/helper changes stop')
    raw = {}
    for path in allowed:
        for name, files in (('before', before), ('after', after)):
            row = files.get(path)
            if not row or row['type'] != 'blob' or row['mode'] != '100644':
                raise ValueError('Selection governance files must remain existing regular non-executable files')
            raw[name, path] = process.blob(request, root, row['sha'])
    unique = rules.direct_module().unique_object
    def parsed(name, path):
        value = json.loads(raw[name, path], object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError('Selection governance JSON must be an unambiguous object')
        return value
    if (digest(parsed('before', 'tracker/config.json')) != digest(prior)
            or digest(parsed('after', 'tracker/config.json')) != digest(selected)):
        raise ValueError('Selection configs differ from the actual immutable Git trees')
    descriptors = [parsed(name, 'delivery.json') for name in ('before', 'after')]
    selection_descriptor(*descriptors, prior, selected, key)
    bases = [parsed(name, '.workflow/template-base.json') for name in ('before', 'after')]
    for value, descriptor in zip(bases, descriptors):
        if (set(value) != {'schema_version', 'standard_revision', 'inputs', 'files'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or not isinstance(descriptor.get('standard'), dict)
                or not isinstance(value['standard_revision'], str)
                or value['standard_revision'] != descriptor.get('standard', {}).get('revision')
                or not rules.SHA.fullmatch(value['standard_revision'])
                or not isinstance(value['inputs'], dict) or not isinstance(value['files'], dict)
                or any(not isinstance(name, str) or not isinstance(text, str) for name, text in value['files'].items())):
            raise ValueError('Selection delivery needs the actual retained pristine template ancestry')
    old, new = bases
    if digest({k: v for k, v in old.items() if k not in ('inputs', 'files')}) != digest({k: v for k, v in new.items() if k not in ('inputs', 'files')}):
        raise ValueError('Selection cannot repin its pristine template')
    selection_delta(old['inputs'], new['inputs'], SELECTIONS[key][1])
    if digest(new['inputs'][SELECTIONS[key][1]]) != digest(selected[key]):
        raise ValueError('Pristine selection input differs from the official selected policy')
    files_before, files_after = old['files'], new['files']
    if files_before.keys() != files_after.keys():
        raise ValueError('Selection delivery cannot add or remove pristine template files')
    expected = allowed - {'.workflow/template-base.json'}
    if (not expected <= files_before.keys()
            or {path for path in files_before if files_before[path] != files_after[path]} != expected):
        raise ValueError('Pristine template changed outside exact selection consequences')
    configs = [json.loads(files['tracker/config.json'], object_pairs_hook=unique) for files in (files_before, files_after)]
    for config in configs:
        selection_config(config)
    selection_delta(*configs, key)
    if digest(configs[1][key]) != digest(selected[key]):
        raise ValueError('Pristine config selection differs from the actual policy')
    selection_descriptor(*(json.loads(files['delivery.json'], object_pairs_hook=unique) for files in (files_before, files_after)), *configs, key)
    # Exact source/combined review binds these full text blobs. The portable
    # reader enforces their fixed non-runtime paths, not a fictitious renderer.
    if any(not raw[name, path].strip() for name in ('before', 'after')
           for path in ('docs/repository/TRACKER.md', document)):
        raise ValueError('Selection documentation must retain its real reviewed guidance')


def selection_delivery_context(request, repo, shared, config, current, merge_sha):
    """Derive one selection package's operative config from its genuine B/M.

    No state field, caller-selected baseline, authority flag or ordinary reader
    fallback is introduced. Existing receipt fields bind the immutable merge.
    """
    value = rules.execution_policy(config)
    if not value or value['schema_version'] not in (2, 3):
        return None
    missing = [key for key, (layer, _, _) in SELECTIONS.items() if key in config and layer not in shared]
    if not missing:
        return None
    selection_config(config)
    if len(missing) != 1 or current != merge_sha:
        raise ValueError('Selection delivery needs one new policy and exact unchanged official selection main')
    key = missing[0]
    if (rules.policy(config)['staging']['enabled'] or rules.policy(config)['completion'] != 'verified-stable-package'
            or config['project']['base_branch'] != 'main' or rules.retry_module().pending(shared)):
        raise ValueError('Selection delivery requires noStage package and no unresolved retry effects')
    root = 'repos/' + repo
    matches = []
    for batch in shared.get('execution', {}).get('batches', {}).values():
        if batch.get('state') == 'merged' and batch.get('delivery_request'):
            for number in batch.get('main_prs', []):
                pr = request(root + f'/pulls/{number}')
                if pr.get('merge_commit_sha') == merge_sha:
                    matches.append((batch, pr))
    if len(matches) != 1:
        raise ValueError('Selection delivery needs one actual completed authorized batch')
    batch, pr = matches[0]
    baseline = batch.get('main_sha')
    if (not isinstance(baseline, str) or not rules.SHA.fullmatch(baseline)
            or baseline == merge_sha or len(batch.get('issues', [])) != 1
            or not rules.direct_module().integer(batch['issues'][0])
            or not rules.direct_module().integer(pr.get('number'))
            or batch.get('main_prs') != [pr.get('number')]):
        raise ValueError('Selection delivery needs its genuine one-member preselection baseline')
    prior = configuration(request, repo, baseline)
    selection_config(prior)
    selection_delta(prior, config, key)
    section = rules.execution_state(shared, prior)
    frozen = [row.get('batch') for row in section['history']
              if row.get('event') == 'batch-frozen' and row.get('batch', {}).get('id') == batch.get('id')]
    binding = ('id', 'actor', 'session', 'issues', 'members', 'main_sha', 'branch', 'intake', 'started_at', 'delivery_request')
    if (len(frozen) != 1 or any(name not in batch or name not in frozen[0]
            or digest(batch[name]) != digest(frozen[0][name]) for name in binding)):
        raise ValueError('Selection batch/request no longer matches its genuine pre-main freeze')
    try:
        from . import batch_policy
    except ImportError:
        import tracker_batch_policy as batch_policy
    admission = batch_policy.admit(request, prior, pr, batch['issues'], shared)
    retained = shared.get('delivery_workflow', {}).get('main_batches', {}).get(str(pr['number']), {})
    expected = dict(admission, pr=pr['number'], source_sha=merge_sha, issues=batch['issues'])
    if (set(retained) != set(expected) | {'recorded_at'}
            or not rules.direct_module().timestamp(retained.get('recorded_at'))
            or digest({name: retained[name] for name in expected}) != digest(expected)
            or admission.get('main_sha') != baseline):
        raise ValueError('Selection delivery needs its exact admitted main/base/receipt binding')
    if section.get('integration') or section.get('review'):
        raise PendingDelivery('Finish the genuine source/integration handoff before selection delivery')
    for member in batch['members']:
        rules.checked(request, root, member['head_sha'], rules.policy(prior)['required_checks'])
        if rules.tree(request, root, member['head_sha']) != rules.tree(request, root, merge_sha):
            raise ValueError('Selection main must retain the exact independently reviewed single source tree')
    others = dict(shared, execution=dict(section, batches={name: row for name, row in section['batches'].items() if row is not batch}))
    if rules.pending_delivery(others, prior):
        raise PendingDelivery('Finish every other authorized package before this selection delivery')
    authorization(prior, batch)
    selection_tree(request, repo, baseline, merge_sha, prior, config, key)
    return prior


def resolve(repo, merge_sha, ci_run, *, request=github, include_delivered=False):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or not rules.SHA.fullmatch(merge_sha):
        raise ValueError('Supply official repository and exact main merge SHA')
    source_config = configuration(request, repo, merge_sha)
    run = official_run(request, repo, ci_run, merge_sha)
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        raise ValueError('Main CI has not succeeded on the exact delivery revision')
    root = 'repos/' + repo
    current = request(root + '/git/ref/heads/main')['object']['sha']
    config = configuration(request, repo, current)
    original, active = rules.policy(source_config), rules.policy(config)
    if ({k: v for k, v in original.items() if k != 'staging'}
            != {k: v for k, v in active.items() if k != 'staging'}
            or original['staging']['branch'] != active['staging']['branch']
            or rules.execution_policy(source_config) != rules.execution_policy(config)):
        raise ValueError('Current delivery target, checks, completion or execution differs from the authorized source')
    if request(root + '/compare/' + merge_sha + '...' + current).get('status') not in ('ahead', 'identical'):
        raise ValueError('Delivery version is no longer included in main')
    shared, _ = state(request, repo)
    selected = selection_delivery_context(request, repo, shared, config, current, merge_sha)
    if selected is not None:
        config = selected
    section = rules.execution_state(shared, config)
    if section['policy']['schema_version']==4:
        direct_result=resolve_direct(request,repo,config,shared,merge_sha,ci_run,include_delivered)
        if direct_result is not None:return direct_result
    matches = []
    for batch in section.get('batches', {}).values():
        if batch.get('state') != 'merged' or not batch.get('delivery_request'):
            continue
        for number in batch.get('main_prs', []):
            pr = request(root + f'/pulls/{number}')
            if pr.get('merge_commit_sha') == merge_sha:
                matches.append((batch, pr))
    if len(matches) > 1:
        raise ValueError('Ambiguous authorized main batch; inspect retained evidence')
    if not matches:
        held = section.get('integration') or {}
        if held.get('delivery_request'):
            heads = [m.get('head_sha') for m in held.get('members', [])]
            if heads and all(request(root + '/compare/' + h + '...' + merge_sha).get('status')
                             in ('ahead', 'identical') for h in heads):
                raise PendingDelivery('Record the admitted main merge and finish its Coordinator handoff first')
        return {'eligible': False, 'reason': 'No completed batch authorized delivery for this main revision'}
    batch, pr = matches[0]
    auth = authorization(config, batch)
    issues = batch.get('issues', [])
    members = batch.get('members', [])
    if (not issues or len(issues) != len(set(issues)) or any(type(n) is not int or n < 1 for n in issues)
            or [m.get('issue') for m in members] != issues
            or not pr.get('merged') or pr.get('base', {}).get('ref') != 'main'
            or pr.get('base', {}).get('repo', {}).get('full_name', '').lower() != repo.lower()
            or re.findall(r'^Tracker batch:\s*([a-f0-9]{32})\s*$', pr.get('body') or '', re.M) != [batch['id']]):
        raise ValueError('Delivery requires the actual admitted full-member main PR')
    _, already_verified = task_context(request, repo, config, shared, batch, merge_sha)
    if already_verified and not include_delivered:
        return {'eligible': False, 'reason': 'The exact authorized batch delivery is already verified'}
    tree = request(root + '/git/commits/' + merge_sha)['tree']['sha']
    if selected is not None and request(root + '/git/ref/heads/main')['object']['sha'] != current:
        raise ValueError('Official selection main changed while resolving the exact package')
    return {'eligible': True, 'schema_version': 1, 'repository': repo, 'batch_id': batch['id'],
            'issues': issues, 'main_pr': pr['number'], 'merge_sha': merge_sha, 'tree_sha': tree,
            'policy': rules.fingerprint(config), 'target': auth['target'],
            'workflow': auth['workflow'], 'job': auth['job'], 'requested_at': auth['requested_at'],
            'authorization_sha256': digest(auth),
            'ci_run': ci_run}


def authorization_direct(config,archive):
    direct=rules.direct_module();auth=archive.get('delivery_request')
    if not auth:return None
    direct.direct_authorization(auth,archive)
    selected=direct.subject(archive.get('subject'));direct.task_number(selected)
    expected=dict(schema_version=2,kind='direct',state='requested',subject=selected,
        reservation_sha256=direct.digest(archive['id']),source_pr=archive['source_pr'],source_head_sha=archive['source_head_sha'],
        scope_sha256=archive['scope_sha256'],policy=rules.fingerprint(config),route_policy_sha256=direct.digest(direct.bundle(config)),
        target=rules.policy(config)['delivery_target'])
    if (type(auth.get('schema_version')) is not int or any(auth.get(k)!=v for k,v in expected.items())
            or not isinstance(auth.get('workflow'),str) or not WORKFLOW.fullmatch(auth['workflow'])
            or not direct.text(auth.get('job'),200) or not direct.timestamp(auth.get('requested_at'))):
        raise ValueError('Exact typed direct delivery authorization changed')
    return auth


def direct_context(request,repo,config,shared,archive,merge_sha):
    direct=rules.direct_module();section=rules.execution_state(shared,config)
    number=direct.task_number(archive['subject']);journal=archive.get('finalization',{})
    if archive.get('state')!='completed' or journal.get('phase')!='released' or journal.get('merge_sha')!=merge_sha:
        raise PendingDelivery('Complete the genuine direct receipt/status/source-owner release first')
    issue=request(f'repos/{repo}/issues/{number}');labels={r['name'] for r in issue.get('labels',[])}
    merged={'status:'+(s if isinstance(s,str) else s['id']) for s in config['workflow'] if (s if isinstance(s,str) else s['phase'])=='merged'}
    tasks=shared.get('delivery_workflow',{}).get('tasks',{});row=tasks.get(str(number),{})
    if ({s for s in labels if s.startswith('status:')}!=merged or {'flag:blocked','flag:archived'}&labels
            or 'pull_request' in issue or issue.get('state_reason')=='not_planned' or row.get('process')
            or section['tasks'].get(str(number),{}).get('state')!='merged'
            or not rules.scope_matches(issue,row.get('scope'),config)
            or direct.digest(row.get('scope'))!=archive['scope_sha256']
            or rules.reconciled_policy(row)[0]!=rules.fingerprint(config)
            or row.get('sources')!=[dict(pr=journal['main_pr'],merge_sha=merge_sha,tree_sha=rules.tree(request,'repos/'+repo,merge_sha))]):
        raise PendingDelivery('Exact real task scope/main receipt/status changed before direct delivery')
    rules.require_completion_scope(issue,config,row)
    verification=row.get('verification') or {}
    verified=(row.get('state')=='verified' and verification.get('result')=='passed'
              and verification.get('target')==rules.policy(config)['delivery_target'] and verification.get('merge_shas')==[merge_sha])
    return tasks,number,verified


def resolve_direct(request,repo,config,shared,merge_sha,ci_run,include_delivered):
    section=rules.execution_state(shared,config);matches={}
    for row in section['completed'].values():
        if row.get('kind')=='direct' and row.get('delivery_request') and row.get('finalization',{}).get('merge_sha')==merge_sha:
            matches[row['id']]=row
    if len(matches)>1:raise ValueError('Ambiguous direct main delivery source')
    if not matches:
        held=section.get('integration') or {}
        if held.get('kind')=='direct' and held.get('delivery_request') and (held.get('finalization') or {}).get('merge_sha')==merge_sha:
            raise PendingDelivery('Finish retained direct main receipt/status/source owner first')
        return None
    archive=next(iter(matches.values()));auth=authorization_direct(config,archive)
    _,_,verified=direct_context(request,repo,config,shared,archive,merge_sha)
    pr=request(f'repos/{repo}/pulls/{archive["finalization"]["main_pr"]}')
    if (not pr.get('merged') or pr.get('merge_commit_sha')!=merge_sha or pr['base']['ref']!=config['project']['base_branch']
            or rules.direct_module().linkage(pr.get('body'),config)!=archive['subject']):
        raise ValueError('Direct delivery needs the actual admitted typed main PR')
    if verified and not include_delivered:return dict(eligible=False,reason='The exact authorized direct delivery is already verified')
    return dict(eligible=True,schema_version=2,kind='direct',repository=repo,subject=copy.deepcopy(archive['subject']),
        reservation_sha256=rules.direct_module().digest(archive['id']),main_pr=pr['number'],merge_sha=merge_sha,
        tree_sha=rules.tree(request,'repos/'+repo,merge_sha),policy=rules.fingerprint(config),target=auth['target'],
        workflow=auth['workflow'],job=auth['job'],requested_at=auth['requested_at'],authorization_sha256=digest(auth),ci_run=ci_run)


def verification_job(request, repo, run_id, name, attempt):
    """Find the latest actual verifier, retaining success across downstream reruns."""
    # A partial rerun increments run_attempt without rerunning successful upstream
    # jobs. Inspect the native attempts instead of requiring the latest attempt
    # to contain the verifier or trusting an optional job.run_attempt property.
    if type(attempt) is not int or not 1 <= attempt <= 100:
        raise ValueError('Native verification attempt is invalid or exceeds proof bound')
    for actual_attempt in range(attempt, 0, -1):
        found = []
        for page in range(1, 101):
            rows = request(f'repos/{repo}/actions/runs/{run_id}/attempts/{actual_attempt}/jobs?per_page=100&page={page}')
            jobs = rows.get('jobs')
            if not isinstance(jobs, list):
                raise ValueError('Native verification attempt membership is unavailable')
            found.extend(j for j in jobs if j.get('name') == name)
            if len(jobs) < 100:
                break
        else:
            raise ValueError('Native verification job pagination exceeds proof bound')
        if found:
            if len(found) != 1:
                raise ValueError('Native verification job identity is ambiguous')
            return found[0], actual_attempt
    raise ValueError('Verification job is absent from every native run attempt')


def verified_native(request, document, receipt, run_id, job_id):
    repo = document['repository']
    run = official_run(request, repo, run_id, document['merge_sha'])
    if run.get('path') != document['workflow'] or type(job_id) is not int or job_id < 1:
        raise ValueError('Verification workflow differs from the authorized project producer')
    job = request(f'repos/{repo}/actions/jobs/{job_id}')
    if (job.get('run_id') != run_id or job.get('name') != document['job']
            or job.get('head_sha') != document['merge_sha']
            or job.get('status') != 'completed' or job.get('conclusion') != 'success'):
        raise ValueError('The exact authorized verification job has not passed')
    latest, _ = verification_job(request, repo, run_id, document['job'], run.get('run_attempt', 1))
    if latest.get('id') != job_id:
        raise ValueError('A later native verification job supersedes this report')
    log = request(f'repos/{repo}/actions/jobs/{job_id}/logs')
    if not isinstance(log, str) or len(log.encode()) > 8 * 1024 * 1024:
        raise ValueError('Native verification log is unavailable or oversized')
    proofs = []
    for line in log.splitlines():
        if MARKER in line:
            try:
                value = json.loads(line.split(MARKER, 1)[1])
            except (ValueError, TypeError):
                continue
            if value == receipt:
                proofs.append(value)
    if len(proofs) != 1:
        raise ValueError('Receipt must match one actual successful native acceptance report')


def record(document, receipt, run_id, job_id, *, apply=False, request=github):
    if not document.get('eligible'):
        raise ValueError('There is no authorized delivery request')
    actual = resolve(document['repository'], document['merge_sha'], document['ci_run'], request=request,
                     include_delivered=True)
    if (receipt.get('kind') != 'delivery-verification' or receipt.get('result') != 'passed'
            or receipt.get('target') != document['target'] or receipt.get('merge_shas') != [document['merge_sha']]
            or not isinstance(receipt.get('checks'), list) or not receipt['checks']
            or any(not isinstance(x, str) or not x.strip() for x in receipt['checks'])
            or not isinstance(receipt.get('report'), str) or not receipt['report'].strip()):
        raise ValueError('A passed exact-target/revision delivery verification is required')
    repo = document['repository']
    shared, sha = state(request, repo)
    current = request('repos/' + repo + '/git/ref/heads/main')['object']['sha']
    config = configuration(request, repo, current)
    selected = selection_delivery_context(request, repo, shared, config, current, document['merge_sha'])
    if selected is not None:
        config = selected
    is_direct=document.get('kind')=='direct'
    if is_direct:
        direct=rules.direct_module()
        batch=shared.get('execution',{}).get('completed',{}).get(str(document['main_pr']),{})
        if (document.get('schema_version')!=2 or type(document.get('schema_version')) is not int
                or batch.get('kind')!='direct' or direct.digest(batch.get('id'))!=document.get('reservation_sha256')):
            raise ValueError('Exact retained typed direct delivery archive required')
        auth=authorization_direct(config,batch)
    else:
        batch = shared.get('execution', {}).get('batches', {}).get(document['batch_id'], {})
        auth = authorization(config, batch)
    if not auth or digest(auth) != actual.get('authorization_sha256'):
        raise ValueError('Authorization changed before receipt write')
    if actual != document:
        if is_direct:raise ValueError('Typed direct delivery request is stale; resolve the actual unchanged authorization again')
        # A retained successful native report can predate a staging-only audit.
        # Accept only an actual earlier prefix of that SAME immutable request's
        # validated reconciliation chain; all task/source/producer fields match.
        ignored = {'policy', 'authorization_sha256'}
        same = ({k: v for k, v in actual.items() if k not in ignored}
                == {k: v for k, v in document.items() if k not in ignored})
        prefixes = []
        for length in range(len(auth.get('policy_reconciliations', [])) + 1):
            previous = copy.deepcopy(auth)
            previous.pop('policy_reconciliations', None)
            if length:
                previous['policy_reconciliations'] = auth['policy_reconciliations'][:length]
            prefixes.append(digest(previous) == document.get('authorization_sha256')
                            and rules.reconciled_request_policy(previous, batch) == document.get('policy'))
        if not same or not any(prefixes):
            raise ValueError('Delivery request is stale or changed; resolve the same authorized batch again')
        document = actual
    if rules.fingerprint(config) != document['policy']:
        raise ValueError('Delivery policy changed before receipt write')
    if is_direct:
        tasks,number,_=direct_context(request,repo,config,shared,batch,document['merge_sha']);numbers=[number]
    else:
        if document['main_pr'] not in batch.get('main_prs', []) or batch.get('issues') != document['issues']:
            raise ValueError('Main handoff changed before receipt write')
        tasks, _ = task_context(request, repo, config, shared, batch, document['merge_sha']);numbers=document['issues']
    native = {'run': run_id, 'job': job_id, 'workflow': document['workflow'],
              'authorization_sha256': document['authorization_sha256']}
    if all(tasks[str(n)].get('state') == 'verified'
           and tasks[str(n)].get('verification') == receipt
           and tasks[str(n)].get('native_delivery') == native for n in numbers):
        return {'applied': False, 'already_recorded': True, 'issues': numbers}
    verified_native(request, document, receipt, run_id, job_id)
    if all(tasks[str(n)].get('state') == 'verified'
           and tasks[str(n)].get('verification') == receipt
           and {k: v for k, v in tasks[str(n)].get('native_delivery', {}).items() if k != 'job'}
               == {k: v for k, v in native.items() if k != 'job'}
           for n in numbers):
        # A successful uncertain CAS may be followed by a downstream-only rerun.
        # GitHub can clone upstream success under a new job ID. Fresh native proof
        # above validates the clone; retain the originally recorded job/evidence.
        return {'applied': False, 'already_recorded': True, 'issues': numbers}
    if is_direct:
        if request('repos/'+repo+'/git/ref/heads/main')['object']['sha']!=current:
            raise ValueError('Official main changed during native delivery verification; retain the pending authorization')
        direct_context(request,repo,config,shared,batch,document['merge_sha'])
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for number in numbers:
        row = tasks[str(number)]
        if row.get('state') == 'verified':
            raise ValueError('Verified delivery already exists; preserve it instead of replacing evidence')
        row.setdefault('history', []).append({k: copy.deepcopy(v) for k, v in row.items() if k != 'history'})
        rules.retain_scope(row)
        row.pop('policy_reconciliations', None)
        row.update(policy=document['policy'], state='verified', verification=copy.deepcopy(receipt),
                   verified_at=at, native_delivery=native)
    if apply:
        root = 'repos/' + repo
        if selected is not None:
            task_context(request, repo, config, shared, batch, document['merge_sha'])
            if request(root + '/git/ref/heads/main')['object']['sha'] != current:
                raise ValueError('Official selection main changed during native delivery verification')
        rules.execution_state(shared,config)
        # CAS failure/uncertain response is never retried with a stale body.
        request(root + '/contents/.tracker-state.json', 'PUT', {
            'message': 'Record verified authorized delivery [skip ci]', 'branch': 'tracker-state', 'sha': sha,
            'content': base64.b64encode(json.dumps(shared, indent=2).encode()).decode()})
    return {'applied': apply, 'issues': numbers, 'result': 'passed'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('resolve')
    p.add_argument('--repository', required=True)
    p.add_argument('--merge-sha', required=True)
    p.add_argument('--ci-run', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = commands.add_parser('record')
    p.add_argument('--request', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--verification-run', type=int, required=True)
    p.add_argument('--verification-job', type=int, required=True)
    p.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'resolve':
        value = resolve(args.repository, args.merge_sha, args.ci_run)
        args.output.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    else:
        value = record(json.loads(args.request.read_text()), json.loads(args.evidence.read_text()),
                       args.verification_run, args.verification_job, apply=args.apply)
    print(json.dumps(value))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PendingDelivery as error:
        print(str(error))
        raise SystemExit(75)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print('Delivery continuation refused: ' + str(error))
        raise SystemExit(1)
