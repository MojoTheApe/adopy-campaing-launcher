"""Versioned, opt-in CI coverage from native Actions producer jobs.

Portable export: stdlib only. Coverage is advisory: existing required checks,
independent reviews, runtime acceptance and release gates remain authoritative.
The first protocol supports reviewed static container jobs, not uploaded manifests
or run-level artifact-to-job inference. Unsupported contexts require fresh jobs.
"""
import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from urllib.parse import quote

VERSION = 1
MARKER = 'TRACKER_CI_CONTEXT_V1='
SHA = re.compile(r'[a-f0-9]{40}\Z')
DIGEST_IMAGE = re.compile(r'[^\s]+@sha256:[a-f0-9]{64}\Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Ambiguous duplicate JSON key')
        result[key] = value
    return result


def git_path(value):
    return (isinstance(value, str) and value and not value.startswith(('/', '-'))
            and not any(x in value.split('/') for x in ('', '.', '..'))
            and not any(c in value for c in '\n\r\\'))


def blobs(value, *, nonempty=False):
    if (not isinstance(value, dict) or nonempty and not value
            or any(not git_path(k) or not isinstance(v, str) or not SHA.fullmatch(v) for k, v in value.items())):
        raise ValueError('CI context needs explicit repository paths and immutable Git blob SHAs')


def policy(config):
    value = config.get('ci_evidence_policy')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'profiles'}
            or type(value['schema_version']) is not int or value['schema_version'] not in (1, 2)
            or not isinstance(value['profiles'], dict) or not value['profiles']):
        raise ValueError('Invalid ci_evidence_policy version/fields')
    for key, p in value['profiles'].items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', key):
            raise ValueError('CI profile names must be stable identifiers')
        if (not isinstance(p, dict) or set(p) != {'workflow_path', 'workflow_blob_sha', 'producer_files', 'jobs', 'metadata_mode', 'max_age_seconds'}
                or not git_path(p['workflow_path']) or not p['workflow_path'].startswith('.github/workflows/')
                or not isinstance(p['workflow_blob_sha'], str) or not SHA.fullmatch(p['workflow_blob_sha'])
                or p['metadata_mode'] not in ('commit', 'tree') or type(p['max_age_seconds']) is not int
                or not 1 <= p['max_age_seconds'] <= 30 * 86400 or not isinstance(p['jobs'], dict) or not p['jobs']):
            raise ValueError('CI profile needs immutable workflow, producers and explicit metadata mode')
        blobs(p['producer_files'], nonempty=True)
        ids = []
        for name, job in p['jobs'].items():
            if not isinstance(name, str) or not name.strip() or not isinstance(job, dict):
                raise ValueError('CI jobs need unique full native job names')
            if job == {'mode': 'fresh'}:
                continue
            if (set(job) != {'mode', 'workflow_job', 'runner_labels', 'producer_step', 'producer_path', 'verification_steps', 'context'}
                    or job['mode'] != 'fixed' or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', job['workflow_job'])
                    or not isinstance(job['runner_labels'], list) or not job['runner_labels']
                    or any(not isinstance(x, str) or not x or x.endswith('-latest') or x == 'self-hosted' for x in job['runner_labels'])
                    or len(set(job['runner_labels'])) != len(job['runner_labels'])
                    or not isinstance(job['producer_step'], str) or not job['producer_step']
                    or job['producer_path'] not in p['producer_files']
                    or not isinstance(job['verification_steps'], list) or not job['verification_steps']
                    or any(not isinstance(x, str) or not x for x in job['verification_steps'])
                    or len(set(job['verification_steps'])) != len(job['verification_steps'])
                    or job['producer_step'] in job['verification_steps']):
                raise ValueError('CI fixed jobs need static producer, exact native job/steps and stable runner labels')
            ids.append(job['workflow_job'])
            c = job['context']
            if (not isinstance(c, dict) or set(c) != {'container', 'host_independent', 'toolchains', 'dependencies', 'build_inputs', 'fixtures'}
                    or not isinstance(c['container'], str) or not DIGEST_IMAGE.fullmatch(c['container'])
                    or c['host_independent'] is not True
                    or not isinstance(c['toolchains'], dict) or not c['toolchains']
                    or not set(c['toolchains']) <= {'python3', 'node', 'npm'}
                    or any(not isinstance(v, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', v) for v in c['toolchains'].values())):
                raise ValueError('CI reuse needs reviewed host-independent immutable container and exact toolchains')
            blobs(c['dependencies'], nonempty=True); blobs(c['build_inputs']); blobs(c['fixtures'])
        if len(set(ids)) != len(ids):
            raise ValueError('CI workflow job aliases/matrices are unsupported; declare every real job separately')
    return value


def profile(config, key):
    value = policy(config)
    if not value or key not in value['profiles']:
        raise ValueError('CI evidence profile is not explicitly adopted')
    return value['profiles'][key]


def contents(request, root, path, ref):
    row = request(root + '/contents/' + quote(path, safe='/') + '?ref=' + quote(ref, safe=''))
    try:
        from .delivery_policy import file_bytes
    except ImportError:
        from tracker_delivery_policy import file_bytes
    return row, file_bytes(request, root, row)


def adopted(request, config, key):
    root = 'repos/' + config['project']['repository']
    main = request(root + '/git/ref/heads/' + quote(config['project']['base_branch'], safe=''))['object']['sha']
    _, raw = contents(request, root, 'tracker/config.json', main)
    current = json.loads(raw)
    if (current.get('project') != config.get('project') or policy(current) != policy(config)
            or any(current.get(k) != config.get(k) for k in ('delivery_policy', 'execution_policy', 'process_instructions', 'task_context_policy'))):
        raise ValueError('CI policy must match the actual immutable current-main adoption')
    return profile(current, key)


def command(key, name, job):
    return ' '.join(shlex.quote(x) for x in ('python3', '-I', job['producer_path'], 'emit-context', '--profile', key, '--job', name))


def trusted_workflow(request, root, ref, p, key, protocol=1):
    row, raw = contents(request, root, p['workflow_path'], ref)
    if row.get('sha') != p['workflow_blob_sha']:
        raise ValueError('CI producer workflow is not the reviewed immutable workflow')
    # JSON is a deliberately narrow, unambiguous YAML subset. Dynamic/reusable
    # YAML/matrices remain fresh until their producer/context can be proved.
    try:
        workflow = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise ValueError('Native CI evidence v1 supports static JSON-formatted workflows only') from None
    if not isinstance(workflow, dict) or workflow.get('env') or workflow.get('defaults'):
        raise ValueError('CI producer cannot inherit mutable execution context')
    for name, job in p['jobs'].items():
        if job['mode'] == 'fresh':
            continue
        actual = workflow.get('jobs', {}).get(job['workflow_job'], {})
        steps = actual.get('steps', [])
        labels = actual.get('runs-on')
        labels = [labels] if isinstance(labels, str) else labels
        if (actual.get('name', job['workflow_job']) != name or actual.get('container') != job['context']['container']
                or labels != job['runner_labels'] or actual.get('strategy') or actual.get('uses')
                or any(actual.get(x) for x in ('env', 'defaults', 'continue-on-error', 'services', 'environment'))
                or protocol == 1 and actual.get('if') or len(steps) < 3):
            raise ValueError('CI native job does not match the fixed reviewed profile')
        checkout, producer = steps[:2]
        # Only the immutable official checkout runs before context collection.
        # No repository script, dependency installation, expression or hook runs
        # before the trusted producer observes HEAD/context.
        if (set(checkout) - {'uses', 'name'} or not re.fullmatch(r'actions/checkout@[a-f0-9]{40}', checkout.get('uses', ''))
                or producer != {'name': job['producer_step'], 'run': command(key, name, job)}):
            raise ValueError('CI producer must run immediately after immutable default checkout')
        test_names = [s.get('name') for s in steps[2:]]
        if (test_names != job['verification_steps'] or any(set(s) - {'name', 'run'} or not s.get('run') for s in steps[2:])):
            raise ValueError('CI profile must enumerate every unconditional verification step')
    return workflow


def stamp(value):
    if not isinstance(value, str):
        raise ValueError('CI proof needs native timestamps')
    try:
        result = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None: raise ValueError('Missing timestamp timezone')
        return result
    except ValueError:
        raise ValueError('Invalid CI timestamp') from None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def native_context(log, step):
    if not isinstance(log, str) or len(log.encode()) > 8 * 1024 * 1024:
        raise ValueError('CI native job log missing or too large')
    found = []
    start, end = stamp(step['started_at']), stamp(step['completed_at'])
    for line in log.splitlines():
        match = re.fullmatch(r'(\d{4}-\d\d-\d\dT[^ ]+Z) ' + MARKER + r'(\{.*\})', line)
        if match and start <= stamp(match[1]) < end:
            found.append(json.loads(match[2], object_pairs_hook=unique_object))
    if len(found) != 1:
        raise ValueError('CI proof needs exactly one context from its actual producer step/attempt')
    return found[0]


def tested(request, root, run, source_pr, retained=None):
    if source_pr is not None:
        pr = request(root + f'/pulls/{source_pr}')
        historical = bool(retained and pr.get('state') == 'closed' and pr.get('merged'))
        if (run.get('event') != 'pull_request' or not historical and (pr.get('state') != 'open' or pr.get('merged')) or pr.get('draft')
                or pr['head']['sha'] != run['head_sha'] or pr['head']['repo']['full_name'] != run['repository']['full_name']
                or source_pr not in [r['number'] for r in run.get('pull_requests', [])]
                    and not (historical and run.get('pull_requests') == [])):
            raise ValueError('CI source proof needs the actual unchanged open official PR')
        sha = retained['tested_sha'] if historical else pr.get('merge_commit_sha')
        commit = request(root + '/git/commits/' + str(sha))
        base = retained['source_binding']['base_sha'] if historical else pr['base']['sha']
        if [p['sha'] for p in commit.get('parents', [])] != [base, pr['head']['sha']]:
            raise ValueError('CI synthetic checkout does not contain the exact current PR base/head')
        if historical:
            merged = request(root + '/git/commits/' + str(pr.get('merge_commit_sha')))
            if (not merged.get('parents') or merged['parents'][0]['sha'] != base
                    or merged['tree']['sha'] != commit['tree']['sha']):
                raise ValueError('CI retained source differs from its actual admitted merge')
        return sha, commit['tree']['sha']
    if run.get('event') not in ('push', 'workflow_dispatch'):
        raise ValueError('CI PR runs need explicit current source PR binding')
    sha = run['head_sha']
    return sha, request(root + '/git/commits/' + sha)['tree']['sha']


def collect(request, read_log, config, key, run_id, source_pr=None, *, retained=None, _job_gate=False):
    p = adopted(request, config, key)
    root = 'repos/' + config['project']['repository']
    run = request(root + f'/actions/runs/{int(run_id)}')
    if (run.get('repository', {}).get('full_name') != config['project']['repository']
            or run.get('head_repository', {}).get('full_name') != config['project']['repository']
            or run.get('path') != p['workflow_path']
            or not _job_gate and (run.get('status') != 'completed' or run.get('conclusion') != 'success')
            or _job_gate and (run.get('status') not in ('in_progress', 'completed')
                or run.get('status') == 'completed' and run.get('conclusion') != 'success')
            or type(run.get('run_attempt')) is not int):
        raise ValueError('CI proof needs a successful official run of the reviewed workflow')
    if retained and policy(config)['schema_version'] != 2:
        raise ValueError('Historical CI revalidation requires explicitly adopted protocol 2')
    checkout, tree = tested(request, root, run, source_pr, retained)
    trusted_workflow(request, root, checkout, p, key, policy(config)['schema_version'])
    producer_blobs = {}
    for path, expected in p['producer_files'].items():
        row, raw = contents(request, root, path, checkout)
        if row.get('sha') != expected:
            raise ValueError('CI producer code changed from its reviewed immutable blobs')
        direct_paths = {j['producer_path'] for j in p['jobs'].values() if j['mode'] == 'fixed'}
        if path in direct_paths and raw != Path(__file__).read_bytes():
            raise ValueError('CI native v1 requires the direct maintained self-contained producer; wrappers/custom import closure are unsupported')
        producer_blobs[path] = expected
    rows = request(root + f'/actions/runs/{int(run_id)}/attempts/{run["run_attempt"]}/jobs?per_page=100')['jobs']
    # Bound pagination without accepting a partial job matrix.
    if len(rows) >= 100 or len({j['name'] for j in rows}) != len(rows):
        raise ValueError('CI native job matrix incomplete or ambiguous')
    successful = []
    for name, job in p['jobs'].items():
        if job['mode'] == 'fresh':
            continue
        actual = [r for r in rows if r['name'] == name]
        if len(actual) != 1:
            continue
        actual = actual[0]
        if (actual.get('run_id') != int(run_id) or actual.get('run_attempt') != run['run_attempt']
                or actual.get('head_sha') != run['head_sha'] or actual.get('status') != 'completed'
                or actual.get('conclusion') != 'success' or actual.get('labels') != job['runner_labels']):
            continue
        completed = stamp(actual.get('completed_at'))
        age = (utc_now() - completed).total_seconds()
        if age < -60 or age > p['max_age_seconds']:
            continue
        expected_steps = [job['producer_step'], *job['verification_steps']]
        steps = actual.get('steps', [])
        found = [s for s in steps if s.get('name') in expected_steps]
        if ([s['name'] for s in found] != expected_steps
                or any(s.get('status') != 'completed' or s.get('conclusion') != 'success' for s in found)):
            continue
        check = request(actual['check_run_url'].replace('https://api.github.com/', ''))
        if (check.get('app', {}).get('slug') != 'github-actions' or check.get('name') != name
                or check.get('head_sha') != run['head_sha'] or check.get('status') != 'completed'
                or check.get('conclusion') != 'success' or check.get('details_url') != actual.get('html_url')):
            continue
        context = native_context(read_log(actual['id']), found[0])
        expected = {'schema_version': VERSION, 'repository': config['project']['repository'], 'profile': key,
                    'job': name, 'run_id': int(run_id), 'run_attempt': run['run_attempt'],
                    'workflow_sha': checkout, 'checkout_sha': checkout, 'tree_sha': tree,
                    'context': job['context']}
        if policy(config)['schema_version'] == 2:
            # GitHub empties a merged run's PR list. The maintained producer
            # binds its original actual PR ref inside the native step log.
            expected['native_ref'] = (f'refs/pull/{source_pr}/merge' if source_pr is not None
                                      else 'refs/heads/' + str(run.get('head_branch')))
        if (context != expected or any(type(context.get(k)) is not int for k in ('schema_version', 'run_id', 'run_attempt'))):
            raise ValueError('CI producer context/checkout/attempt does not match independent native proof')
        for group in ('dependencies', 'build_inputs', 'fixtures'):
            for path, expected_sha in job['context'][group].items():
                row, _ = contents(request, root, path, checkout)
                if row.get('sha') != expected_sha:
                    raise ValueError('CI resolved inputs differ from reviewed fixed context')
        successful.append({'name': name, 'id': actual['id'], 'run_id': int(run_id), 'attempt': run['run_attempt'],
                           'url': actual['html_url'], 'check_run_url': actual['check_run_url'],
                           'completed_at': actual['completed_at'], 'context': context})
    source_binding = None
    if source_pr is not None:
        try:
            from . import batch_policy, pr_checks, delivery_policy as rules
        except ImportError:
            import tracker_batch_policy as batch_policy
            import tracker_pr_check as pr_checks
            import tracker_delivery_policy as rules
        pr = request(root + f'/pulls/{source_pr}')
        source_binding = {'head_sha': pr['head']['sha'], 'base_sha': pr['base']['sha'], 'branch': pr['head']['ref'],
                          'scope': {str(n): batch_policy.scope(request(root + f'/issues/{n}'), config)
                                    for n in pr_checks.linked_numbers(pr.get('body'), config)},
                          'delivery_policy': rules.fingerprint(config), 'execution_policy': rules.execution_policy(config)}
        if retained and pr.get('merged'):
            source_binding['base_sha'] = retained['source_binding']['base_sha']
    return {'schema_version': VERSION, 'repository': config['project']['repository'], 'profile': key,
            'policy': digest(policy(config)), 'profile_digest': digest(p), 'tested_sha': checkout, 'tree_sha': tree,
            'source_pr': source_pr, 'source_binding': source_binding, 'run_id': int(run_id), 'attempt': run['run_attempt'], 'producer_blobs': producer_blobs,
            'jobs': successful, 'provenance': {'kind': 'native-actions-job-log', 'workflow': run['path'], 'run_url': run['html_url']},
            'artifacts': [], 'bounds': 'No run-level artifact job/attempt inference; no package/runtime acceptance.'}


def save(api, config, store, args):
    state, sha = store.read(optional=True)
    proof = collect(api.request, api.action_job_log, config, args.profile, args.run, args.source_pr)
    key = digest(proof)
    records = state.setdefault('ci_evidence', {'schema_version': VERSION, 'records': {}})
    if records.get('schema_version') != VERSION or not isinstance(records.get('records'), dict):
        raise ValueError('Unsupported persisted CI evidence protocol')
    if key in records['records']:
        if records['records'][key] != proof:
            raise ValueError('Conflicting retained CI evidence identity')
        return {'applied': False, 'evidence_id': key, 'proof': proof, 'private': True}
    records['records'][key] = proof
    if args.apply:
        store.write(state, sha)
    return {'applied': args.apply, 'evidence_id': key, 'proof': proof, 'private': True}


def coverage(request, read_log, config, state, key, number, expected_head, context='source'):
    p = adopted(request, config, key)
    root = 'repos/' + config['project']['repository']
    pr = request(root + f'/pulls/{int(number)}')
    main = context == 'main'
    if (pr['head']['repo']['full_name'] != config['project']['repository']
            or pr.get('base', {}).get('repo', {}).get('full_name', config['project']['repository']) != config['project']['repository']):
        raise ValueError('CI coverage needs official source and base repositories')
    if main:
        if (not pr.get('merged') or pr.get('state') != 'closed' or pr.get('merge_commit_sha') != expected_head
                or pr['base']['ref'] != config['project']['base_branch']):
            raise ValueError('Main coverage requires the actual merged PR and exact merge commit')
        comparison = request(root + '/compare/' + expected_head + '...' + quote(config['project']['base_branch'], safe=''))
        if comparison.get('status') not in ('ahead', 'identical'):
            raise ValueError('Main coverage candidate is not included in actual main')
    elif (pr['head']['sha'] != expected_head or pr.get('state') != 'open' or pr.get('merged') or pr.get('draft')):
        raise ValueError('CI coverage needs the actual current ready PR/head')
    result = {'schema_version': VERSION, 'head_sha': expected_head, 'covered': {}, 'missing': list(p['jobs']),
              'required_checks_unchanged': True, 'runtime_acceptance_unchanged': True, 'advisory': True, 'rejected': []}
    if context not in ('source', 'integration', 'main') or main and policy(config)['schema_version'] != 2:
        result['reason'] = 'This context requires actual fresh checks under the adopted protocol'
        return result
    try:
        from . import delivery_policy as rules, batch_policy, pr_checks
    except ImportError:
        import tracker_delivery_policy as rules
        import tracker_batch_policy as batch_policy
        import tracker_pr_check as pr_checks
    allowed_prs = {int(number)}
    if context == 'integration':
        section = rules.execution_state(state, config)
        batch = (section or {}).get('integration')
        if not batch:
            raise ValueError('CI reuse needs the actual frozen integration batch')
        numbers = pr_checks.linked_numbers(pr.get('body'), config)
        batch_policy.validate_candidate(request, config, pr, numbers, batch, state)
        allowed_prs = {m['pr'] for m in batch['members']}
        if policy(config)['schema_version'] == 2: allowed_prs.add(int(number))
    target_sha = expected_head
    if not main and policy(config)['schema_version'] == 2:
        target_sha = pr.get('merge_commit_sha')
        synthetic = request(root + '/git/commits/' + str(target_sha))
        if [x['sha'] for x in synthetic.get('parents', [])] != [pr['base']['sha'], expected_head]:
            raise ValueError('Coverage needs the actual current synthetic candidate')
    target_tree = rules.tree(request, root, target_sha)
    retained = state.get('ci_evidence', {})
    if retained and (retained.get('schema_version') != VERSION or not isinstance(retained.get('records'), dict)):
        raise ValueError('Unsupported retained CI evidence protocol')
    for identity, record in retained.get('records', {}).items():
        if (not isinstance(record, dict) or digest(record) != identity or record.get('profile') != key
                or record.get('policy') != digest(policy(config))
                or not main and record.get('source_pr') not in allowed_prs):
            continue
        if (record.get('tree_sha') != target_tree
                or p['metadata_mode'] == 'commit' and record.get('tested_sha') != target_sha):
            continue
        # Stored JSON is a reference, never producer trust. Revalidate all actual
        # native jobs/logs, the current PR and immutable context on every read.
        try:
            if main:
                if not record.get('source_pr'): continue
                source = request(root + f'/pulls/{record["source_pr"]}')
                if record['source_pr'] != int(number):
                    stage = config.get('delivery_policy', {}).get('staging', {})
                    if (not stage.get('enabled') or source['base']['ref'] != stage['branch']
                            or not source.get('merged') or source.get('merge_commit_sha') != pr['head']['sha']):
                        continue
                source_numbers = pr_checks.linked_numbers(source.get('body'), config)
                if set(source_numbers) != set(pr_checks.linked_numbers(pr.get('body'), config)): continue
            fresh = collect(request, read_log, config, key, record['run_id'], record['source_pr'],
                            retained=record if main else None)
        except (ValueError, KeyError, TypeError):
            result['rejected'].append({'evidence_id': identity, 'reason': 'Native producer/context proof no longer valid; run missing jobs'})
            continue
        if fresh != record:
            continue
        for job in record['jobs']:
            name = job['name']
            spec = p['jobs'][name]
            matched = True
            for group in ('dependencies', 'build_inputs', 'fixtures'):
                for path, expected in spec['context'][group].items():
                    row, _ = contents(request, root, path, target_sha)
                    matched &= row.get('sha') == expected
            # Entire tree equality binds workflow/helper/code; context is derived
            # from the adopted profile/target, never copied from old evidence.
            if matched:
                result['covered'].setdefault(name, []).append({k: job[k] for k in ('id', 'run_id', 'attempt', 'url')})
    result['missing'] = [name for name in p['jobs'] if name not in result['covered']]
    return result


def plan(request, read_log, config, state, key, number, expected_head, context='source'):
    """Read-only native proof plan; callers execute all named missing jobs."""
    if context == 'main' and not number:
        root = 'repos/' + config['project']['repository']
        rows = request(root + '/commits/' + expected_head + '/pulls?per_page=100')
        matches = [row for row in rows if row.get('merge_commit_sha') == expected_head and row.get('merged_at')
                   and row.get('base', {}).get('ref') == config['project']['base_branch']
                   and row.get('head', {}).get('repo', {}).get('full_name') == config['project']['repository']]
        if len(rows) >= 100 or len(matches) != 1:
            p = adopted(request, config, key)
            result = {'schema_version': VERSION, 'head_sha': expected_head, 'covered': {}, 'missing': list(p['jobs']),
                      'required_checks_unchanged': True, 'runtime_acceptance_unchanged': True, 'advisory': True,
                      'rejected': [], 'context': context, 'target_pr': None,
                      'reason': 'No unique actual merged PR; all checks must run fresh'}
            result['missing_json'] = json.dumps(result['missing'], separators=(',', ':'))
            return result
        number = matches[0]['number']
    result = coverage(request, read_log, config, state, key, number, expected_head, context)
    result['context'] = context
    result['target_pr'] = number
    result['missing_json'] = json.dumps(result['missing'], separators=(',', ':'))
    return result


def gate(request, read_log, config, state, key, number, expected_head, run_id, context='source'):
    """Recompute coverage and verify the current native run after all jobs.

    This is a CI aggregate, never admission, independent review or delivery.
    Explicit adoption must make its exact name required; skipped product names
    cannot remain required checks expecting success. Never reinterpret skips in
    the existing admission checker.
    """
    if policy(config)['schema_version'] != 2:
        raise ValueError('The maintained CI gate requires explicitly adopted protocol 2')
    if context not in ('source', 'integration', 'main', 'promotion'):
        raise ValueError('CI gate supports only source, integration, main or promotion candidate runs')
    result = plan(request, read_log, config, state, key, number, expected_head, context)
    number = result['target_pr']
    p = profile(config, key); root = 'repos/' + config['project']['repository']
    run = request(root + f'/actions/runs/{int(run_id)}')
    if (run.get('id') != int(run_id) or run.get('head_sha') != expected_head
            or run.get('repository', {}).get('full_name') != config['project']['repository']
            or run.get('head_repository', {}).get('full_name') != config['project']['repository']
            or run.get('path') != p['workflow_path']
            or context == 'main' and (run.get('event') != 'push' or run.get('head_branch') != config['project']['base_branch'])
            or context in ('source', 'integration', 'promotion') and (run.get('event') != 'pull_request'
                or int(number) not in [r['number'] for r in run.get('pull_requests', [])])):
        raise ValueError('CI gate must inspect its actual official candidate run')
    proof = collect(request, read_log, config, key, run_id,
                    int(number) if run.get('event') == 'pull_request' else None, _job_gate=True)
    proven = {j['name'] for j in proof['jobs']}
    rows = request(root + f'/actions/runs/{int(run_id)}/attempts/{run["run_attempt"]}/jobs?per_page=100')['jobs']
    if len(rows) >= 100 or len({j['name'] for j in rows}) != len(rows):
        raise ValueError('CI gate requires a complete unambiguous native job list')
    failures = []
    fresh = []
    for name, spec in p['jobs'].items():
        actual = [row for row in rows if row.get('name') == name]
        if len(actual) != 1:
            failures.append(name); continue
        actual = actual[0]
        if (actual.get('run_id') != int(run_id) or actual.get('run_attempt') != run['run_attempt']
                or actual.get('head_sha') != expected_head or actual.get('status') != 'completed'):
            failures.append(name); continue
        if actual.get('conclusion') == 'skipped' and name in result['covered']:
            continue
        if (actual.get('conclusion') != 'success'
                or any(s.get('conclusion') in ('failure', 'cancelled', 'timed_out', 'action_required') for s in actual.get('steps', []))
                or spec['mode'] == 'fixed' and name not in proven):
            failures.append(name); continue
        check = request(actual['check_run_url'].replace('https://api.github.com/', ''))
        if (check.get('app', {}).get('slug') != 'github-actions' or check.get('name') != name
                or check.get('head_sha') != expected_head or check.get('status') != 'completed'
                or check.get('conclusion') != 'success' or check.get('details_url') != actual.get('html_url')):
            failures.append(name); continue
        fresh.append(name)
    if failures:
        raise ValueError('CI gate has missing/failed/cancelled/unproved jobs: '+', '.join(failures))
    result.update(passed=True, fresh=fresh, native_run_id=int(run_id), native_attempt=run['run_attempt'])
    return result


def emit(config, key, name, run=subprocess.run, environ=None):
    env = os.environ if environ is None else environ
    p = profile(config, key); job = p['jobs'].get(name)
    if not job or job['mode'] != 'fixed':
        raise ValueError('CI context emission requires the explicit fixed job profile')
    def output(argv):
        if argv[:2] == ['git', 'rev-parse']:
            # Actions container checkouts can be owned by the host runner. Scope
            # this read-only invocation to its exact working directory; never
            # persist config or trust a wildcard/another repository.
            argv = ['git', '-c', 'safe.directory=' + str(Path.cwd()), *argv[1:]]
        result = run(argv, text=True, capture_output=True, check=True)
        return result.stdout.strip()
    repository = config['project']['repository']
    if env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != repository:
        raise ValueError('CI context producer requires its actual official Actions environment')
    for field in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        if not re.fullmatch(r'[1-9][0-9]*', env.get(field, '')):
            raise ValueError('CI context producer needs actual native run/attempt')
    checkout = output(['git', 'rev-parse', 'HEAD']); tree = output(['git', 'rev-parse', 'HEAD^{tree}'])
    if not SHA.fullmatch(checkout) or not SHA.fullmatch(tree) or env.get('GITHUB_WORKFLOW_SHA') != checkout:
        raise ValueError('CI workflow/checkout must be the actual default immutable checkout')
    for path, expected in p['producer_files'].items():
        if output(['git', 'rev-parse', 'HEAD:' + path]) != expected:
            raise ValueError('CI producer blob does not match the reviewed profile')
    for tool, expected in job['context']['toolchains'].items():
        actual = output([tool, '--version'])
        if tool == 'python3': actual = actual.removeprefix('Python ')
        if actual != expected:
            raise ValueError('CI actual toolchain version differs from fixed context')
    for group in ('dependencies', 'build_inputs', 'fixtures'):
        for path, expected in job['context'][group].items():
            if output(['git', 'rev-parse', 'HEAD:' + path]) != expected:
                raise ValueError('CI actual resolved input differs from fixed context')
    result = {'schema_version': VERSION, 'repository': repository, 'profile': key, 'job': name,
              'run_id': int(env['GITHUB_RUN_ID']), 'run_attempt': int(env['GITHUB_RUN_ATTEMPT']),
              'workflow_sha': checkout, 'checkout_sha': checkout, 'tree_sha': tree, 'context': copy.deepcopy(job['context'])}
    if policy(config)['schema_version'] == 2:
        reference = env.get('GITHUB_REF', '')
        event = env.get('GITHUB_EVENT_NAME')
        if not (event == 'pull_request' and re.fullmatch(r'refs/pull/[1-9]\d*/merge', reference)
                or event in ('push', 'workflow_dispatch') and reference.startswith('refs/heads/') and reference[11:]):
            raise ValueError('CI protocol 2 needs its actual native event/ref')
        result['native_ref'] = reference
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('emit-context',))
    p.add_argument('--config', type=Path, default=Path('tracker/config.json'))
    p.add_argument('--profile', required=True); p.add_argument('--job', required=True)
    args = p.parse_args()
    try:
        result = emit(json.loads(args.config.read_text()), args.profile, args.job)
        print(MARKER + json.dumps(result, sort_keys=True, separators=(',', ':')), flush=True)
        # Native step boundaries have second precision. Keep collection before
        # the end boundary so a later untrusted test cannot supply its marker.
        time.sleep(2)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit('CI context unavailable: ' + str(error)) from None


if __name__ == '__main__':
    main()
