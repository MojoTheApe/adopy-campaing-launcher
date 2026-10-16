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
    config = json.loads(rules.file_bytes(request, root, row))
    if config.get('project', {}).get('repository') != repo or not rules.batch_mode(config):
        raise ValueError('Use the configured official batch project')
    return config


def state(request, repo):
    root = 'repos/' + repo
    row = request(root + '/contents/.tracker-state.json?ref=tracker-state')
    shared = json.loads(rules.file_bytes(request, root, row))
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
                or (reconciled_scope or member.get('scope')) != rules.task_scope(item)
                or execution.get('state') != 'merged' or execution.get('batch_id') != batch['id']
                or {s.get('merge_sha') for s in row.get('sources', [])} != {merge_sha}):
            raise PendingDelivery('Every frozen task needs its exact reviewed main merge receipt first')
        verification = row.get('verification') or {}
        already_verified = already_verified and (row.get('state') == 'verified'
            and verification.get('result') == 'passed'
            and verification.get('target') == rules.policy(config)['delivery_target']
            and merge_sha in verification.get('merge_shas', []))
    return tasks, already_verified


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
    section = rules.execution_state(shared, config)
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
    return {'eligible': True, 'schema_version': 1, 'repository': repo, 'batch_id': batch['id'],
            'issues': issues, 'main_pr': pr['number'], 'merge_sha': merge_sha, 'tree_sha': tree,
            'policy': rules.fingerprint(config), 'target': auth['target'],
            'workflow': auth['workflow'], 'job': auth['job'], 'requested_at': auth['requested_at'],
            'authorization_sha256': digest(auth),
            'ci_run': ci_run}


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
    batch = shared.get('execution', {}).get('batches', {}).get(document['batch_id'], {})
    auth = authorization(config, batch)
    if not auth or digest(auth) != actual.get('authorization_sha256'):
        raise ValueError('Authorization changed before receipt write')
    if actual != document:
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
    if document['main_pr'] not in batch.get('main_prs', []) or batch.get('issues') != document['issues']:
        raise ValueError('Main handoff changed before receipt write')
    tasks, _ = task_context(request, repo, config, shared, batch, document['merge_sha'])
    native = {'run': run_id, 'job': job_id, 'workflow': document['workflow'],
              'authorization_sha256': document['authorization_sha256']}
    if all(tasks[str(n)].get('state') == 'verified'
           and tasks[str(n)].get('verification') == receipt
           and tasks[str(n)].get('native_delivery') == native for n in document['issues']):
        return {'applied': False, 'already_recorded': True, 'issues': document['issues']}
    verified_native(request, document, receipt, run_id, job_id)
    if all(tasks[str(n)].get('state') == 'verified'
           and tasks[str(n)].get('verification') == receipt
           and {k: v for k, v in tasks[str(n)].get('native_delivery', {}).items() if k != 'job'}
               == {k: v for k, v in native.items() if k != 'job'}
           for n in document['issues']):
        # A successful uncertain CAS may be followed by a downstream-only rerun.
        # GitHub can clone upstream success under a new job ID. Fresh native proof
        # above validates the clone; retain the originally recorded job/evidence.
        return {'applied': False, 'already_recorded': True, 'issues': document['issues']}
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for number in document['issues']:
        row = tasks[str(number)]
        if row.get('state') == 'verified':
            raise ValueError('Verified delivery already exists; preserve it instead of replacing evidence')
        row.setdefault('history', []).append({k: copy.deepcopy(v) for k, v in row.items() if k != 'history'})
        row.pop('policy_reconciliations', None)
        row.update(policy=document['policy'], state='verified', verification=copy.deepcopy(receipt),
                   verified_at=at, native_delivery=native)
    if apply:
        root = 'repos/' + repo
        # CAS failure/uncertain response is never retried with a stale body.
        request(root + '/contents/.tracker-state.json', 'PUT', {
            'message': 'Record verified authorized delivery [skip ci]', 'branch': 'tracker-state', 'sha': sha,
            'content': base64.b64encode(json.dumps(shared, indent=2).encode()).decode()})
    return {'applied': apply, 'issues': document['issues'], 'result': 'passed'}


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
