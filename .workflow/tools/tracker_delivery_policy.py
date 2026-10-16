"""Portable, read-only admission rules for the opt-in Master Repo contract.

Copied beside the standalone PR checker by export-template. No write operations,
model invocation, installation or deployment takes place in this module.
"""
import base64
import datetime
import hashlib
import json
import re
from urllib.parse import quote

COMPLETIONS = ('reviewed-merge', 'verified-production', 'verified-stable-package')
SHA = re.compile(r'[0-9a-f]{40}\Z')


def valid_branch(value):
    return (isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', value))
            and not any(x in value for x in ('..', '//', '@{'))
            and not value.endswith(('/', '.', '.lock')))


def policy(config):
    value = config.get('delivery_policy')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'schema_version', 'staging', 'completion', 'required_checks', 'delivery_target'}:
        raise ValueError('Invalid delivery_policy fields')
    stage = value['staging']
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('Unsupported delivery policy version')
    if not isinstance(stage, dict) or set(stage) != {'enabled', 'branch', 'target'} or type(stage['enabled']) is not bool:
        raise ValueError('Staging needs enabled, branch and target')
    main = config['project']['base_branch']
    if not valid_branch(main) or not valid_branch(stage['branch']) or main == stage['branch']:
        raise ValueError('Main and stage must be distinct valid branches')
    if value['completion'] not in COMPLETIONS:
        raise ValueError('Unknown delivery completion policy')
    for target in (stage['target'], value['delivery_target']):
        if not isinstance(target, str) or not target.strip():
            raise ValueError('Environment targets must be explicit identities')
    checks = value['required_checks']
    if (not isinstance(checks, list) or not checks or any(not isinstance(x, str) or not x.strip() for x in checks)
            or len(set(checks)) != len(checks)):
        raise ValueError('Delivery policy needs unique required check names')
    return value


def fingerprint(config):
    # Capacity is enforced by execution-state equality and unique lane ownership;
    # changing it must not revoke already verified delivery or adoption history.
    return hashlib.sha256(json.dumps(policy(config), sort_keys=True).encode()).hexdigest()


def execution_policy(config):
    value = config.get('execution_policy')
    if 'execution_policy' not in config:
        return None
    fields = {'schema_version', 'implementation_limit', 'review_limit'}
    version = value.get('schema_version') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != fields | ({'batch_limit'} if version in (2, 3) else set())
            or type(version) is not int or version not in (1, 2, 3)
            or type(value['implementation_limit']) is not int or not 1 <= value['implementation_limit'] <= 16
            or type(value['review_limit']) is not int or not 1 <= value['review_limit'] <= (5 if version == 3 else 1)):
        raise ValueError('Execution policy needs schema_version 1..3, implementation_limit 1..16; review_limit is 1 (v1/v2) or 1..5 (v3)')
    if not policy(config):
        raise ValueError('Parallel execution requires the independent-review delivery policy')
    if version in (2, 3) and (type(value['batch_limit']) is not int or not 1 <= value['batch_limit'] <= (5 if version == 3 else 16)):
        raise ValueError('Approved batches require batch_limit 1..16 (v2) or 1..5 (v3)')
    return value


def batch_mode(config):
    return bool(execution_policy(config) and execution_policy(config)['schema_version'] in (2, 3))


def review_lanes(section):
    """Current source lanes and the separately bounded integration lane."""
    if not section:
        return []
    return list(section.get('source_reviews', {}).values()) + ([section['review']] if section.get('review') else [])


def review_lane(section, *, pr=None, claim=None, issue=None):
    matches = [lane for lane in review_lanes(section)
               if (pr is None or lane.get('pr') == pr)
               and (claim is None or lane.get('id') == claim)
               and (issue is None or issue in lane.get('issues', []))]
    if len(matches) > 1:
        raise ValueError('Conflicting review owners; inspect shared state')
    return matches[0] if matches else None


def dependencies(body, repo):
    marker = r'\bneeds(?:\*\*)?\s*:\s*(?:\*\*)?\s*`?'
    pattern = marker + r'(?:#(\d+)|([a-z][a-z0-9]*-\d+[a-z]?)|https://github\.com/' + re.escape(repo) + r'/issues/(\d+))'
    local = ['#' + (a or c) if (a or c) else b.lower() for a,b,c in re.findall(pattern, body, re.I)]
    external = [url for url in re.findall(marker + r'(https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)\b', body, re.I)
                if not url.lower().startswith('https://github.com/' + repo.lower() + '/issues/')]
    return list(dict.fromkeys(local + external))


def accepted_scope_digest(issue):
    labels = sorted(x['name'] if isinstance(x, dict) else x for x in issue.get('labels', []))
    fields = {k: issue.get(k) for k in ('number', 'title', 'body', 'state', 'state_reason', 'created_at', 'closed_at')}
    fields['labels'] = [x for x in labels if not x.startswith(('release:', 'priority:'))]
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def task_scope(issue):
    labels = sorted(x['name'] if isinstance(x, dict) else x for x in issue.get('labels', [])
                    if not (x['name'] if isinstance(x, dict) else x).startswith(('status:', 'priority:', 'release:')))
    return hashlib.sha256(json.dumps({k: issue.get(k) for k in ('number', 'title', 'body')}
                                   | {'labels': labels}, sort_keys=True).encode()).hexdigest()


def receipt_digest(receipt):
    original = {k: v for k, v in receipt.items() if k not in ('history', 'policy_reconciliations')}
    return hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()


def reconciled_policy(receipt):
    """An explicit staging-only audit can carry old facts, never create success."""
    current, scope = receipt.get('policy'), None
    for row in receipt.get('policy_reconciliations', []):
        if (not isinstance(row, dict) or row.get('from_policy') != current
                or row.get('receipt_sha256') != receipt_digest(receipt)
                or row.get('sources') != receipt.get('sources', [])
                or any(not isinstance(row.get(k), str) or not re.fullmatch(r'[0-9a-f]{64}', row[k])
                       for k in ('from_policy', 'to_policy', 'scope'))
                or not row.get('report') or not row.get('previous_revision')):
            return receipt.get('policy'), None
        current, scope = row['to_policy'], row['scope']
    return current, scope


def reconciled_request_policy(request, batch):
    """Carry an existing authorization only through its audited unchanged batch."""
    current = request.get('policy')
    expected = dict(batch_id=batch.get('id'), issues=batch.get('issues'), target=request.get('target'),
                    main_prs=batch.get('main_prs'), source_heads=[m.get('head_sha') for m in batch.get('members', [])])
    for row in request.get('policy_reconciliations', []):
        if (not isinstance(row, dict) or row.get('from_policy') != current
                or row.get('request_sha256') != receipt_digest(request)
                or any(row.get(k) != v for k, v in expected.items())
                or any(not isinstance(row.get(k), str) or not re.fullmatch(r'[0-9a-f]{64}', row[k])
                       for k in ('from_policy', 'to_policy'))
                or not row.get('report') or not row.get('previous_revision')):
            return request.get('policy')
        current = row['to_policy']
    return current


def pending_delivery(shared, config):
    """Keep the target's main stable until its explicitly requested delivery ends."""
    completion = policy(config)
    if not completion:
        return False
    for batch in (shared.get('execution') or {}).get('batches', {}).values():
        request = batch.get('delivery_request', {})
        if (batch.get('state') != 'merged' or request.get('state') != 'requested'
                or request.get('target') != completion['delivery_target']):
            continue
        expected = set(batch.get('main_prs', []))
        for n in batch['issues']:
            receipt = shared.get('delivery_workflow', {}).get('tasks', {}).get(str(n), {})
            matching = [summary(r) for r in [receipt, *reversed(receipt.get('history', []))]
                        if expected and expected <= {s.get('pr') for s in r.get('sources', [])}
                        and summary(r).get('policy') == fingerprint(config)]
            row = next(iter(matching), {})
            if not row.get('admitted') or completion['completion'] != 'reviewed-merge' and not row.get('verified'):
                return True
    return False


def execution_state(state, config):
    value = execution_policy(config)
    if not value:
        if state_schema(state) == 2:
            raise ValueError('Shared execution state requires the adopted execution policy; restore the pinned config')
        return None
    section = state.get('execution')
    if (state_schema(state) != 2 or not isinstance(section, dict)
            or section.get('policy') != value):
        raise ValueError('Run execution migrate while the repository is idle to adopt this execution policy')
    if value['schema_version'] in (2, 3) and 'delivery_policy' not in section and not policy(config)['staging']['enabled']:
        raise ValueError('Unmarked historical batch state supported Stage only; use inspected delivery-policy migrate before disabling Stage')
    if section.get('delivery_policy', fingerprint(config)) != fingerprint(config):
        raise ValueError('Delivery policy changed; use the inspected idle delivery-policy migrate transition')
    for key in ('claims', 'tasks', 'actors', 'completed'):
        if not isinstance(section.get(key), dict):
            raise ValueError('Invalid execution state: ' + key)
    if not isinstance(section.get('review_queue'), list) or not isinstance(section.get('history'), list):
        raise ValueError('Invalid execution queue/history')
    if section.get('review') is not None and not isinstance(section['review'], dict):
        raise ValueError('Invalid review lane')
    if state.get('active') or len(section['claims']) > value['implementation_limit']:
        raise ValueError('Conflicting execution owners; inspect shared state')
    if value['schema_version'] in (2, 3):
        if not isinstance(section.get('approvals'), dict) or not isinstance(section.get('batches'), dict):
            raise ValueError('Invalid approval/batch state')
        for key in ('integration', 'intake'):
            if section.get(key) is not None and not isinstance(section[key], dict):
                raise ValueError('Invalid batch ownership: ' + key)
    if value['schema_version'] == 3:
        lanes = section.get('source_reviews')
        if not isinstance(lanes, dict) or len(lanes) > value['review_limit']:
            raise ValueError('Invalid source review capacity')
        seen = {key: set() for key in ('id', 'actor', 'session', 'pr', 'issue')}
        for key, lane in lanes.items():
            if (not isinstance(lane, dict) or key != str(lane.get('pr')) or lane.get('batch_id')
                    or len(lane.get('issues', [])) != 1):
                raise ValueError('Invalid source review lane')
        if section.get('review') and not section['review'].get('batch_id'):
            raise ValueError('Schema 3 reserves review for the single integration lane')
        for lane in review_lanes(section):
            for key in seen:
                values = lane.get('issues', []) if key == 'issue' else [lane.get(key)]
                if any(v is None or v in seen[key] for v in values):
                    raise ValueError('Conflicting review owners; each lane needs a distinct run and task')
                seen[key].update(values)
    elif section.get('source_reviews'):
        raise ValueError('Source review pools require explicit execution schema 3')
    return section


def file_bytes(request, root, row):
    """Decode a contents-API file row; files over 1 MB come back without inline content."""
    if not isinstance(row, dict) or row.get("type", "file") != "file":
        raise ValueError("Expected a repository file")
    content, encoding = row.get("content"), row.get("encoding")
    if content and encoding in ("base64", None):
        return base64.b64decode(content, validate=False)
    # GitHub returns encoding "none" and empty content for 1–100 MB files; the Git blob API still serves them.
    sha = row.get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("Cannot establish repository file revision")
    blob = request(root + "/git/blobs/" + sha)
    if not isinstance(blob, dict) or blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
        raise ValueError("Unexpected repository blob response")
    data = base64.b64decode(blob["content"], validate=False)
    if isinstance(row.get("size"), int) and len(data) != row["size"]:
        raise ValueError("Repository blob size does not match its file")
    return data


def read_full_state(request, repo):
    root = 'repos/' + repo
    row = request(root + '/contents/.tracker-state.json?ref=tracker-state')
    state = json.loads(file_bytes(request, root, row))
    if state_schema(state) not in (1, 2):
        raise ValueError('Invalid tracker state')
    return state


def state_schema(state):
    """Validate the maintenance fence before exposing its retained state format.

    Older engines reject outer schema 3. No old writer can read a held fence and
    obtain a fresh SHA for a write that silently ignores maintenance.
    """
    version = state.get('schema_version')
    held = state.get('maintenance')
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError('Unsupported shared state protocol')
    if version != 3:
        if held is not None:
            raise ValueError('Maintenance requires the fenced shared state protocol')
        return version
    if (not isinstance(held, dict) or held.get('schema_version') != 1
            or type(held.get('schema_version')) is not int or held.get('state') != 'held'
            or type(held.get('base_schema_version')) is not int or held['base_schema_version'] not in (1, 2)
            or not isinstance(held.get('id'), str) or not re.fullmatch(r'[0-9a-f]{32}', held['id'])
            or not isinstance(held.get('actor'), str) or not re.fullmatch(r'Maintainer-[A-Za-z0-9]{4,32}', held['actor'])
            or not isinstance(held.get('session'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}', held['session'])
            or any(not isinstance(held.get(k), str) or not held[k].strip() for k in ('reserved_at', 'report'))):
        raise ValueError('Invalid held maintenance owner/protocol')
    if not isinstance(state.get('maintenance_history'), list):
        raise ValueError('Invalid maintenance history')
    try:
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|\+00:00)', held['reserved_at']):
            raise ValueError('Invalid timestamp')
        datetime.datetime.fromisoformat(held['reserved_at'].replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Invalid maintenance reservation timestamp') from None
    return held['base_schema_version']


def admission_open(state):
    state_schema(state)
    if state.get('maintenance'):
        raise ValueError('Maintenance holds new admission; resume an existing owner or inspect maintenance show')


def set_state_schema(state, version):
    state_schema(state)
    if state.get('maintenance'):
        state['maintenance']['base_schema_version'] = version
    else:
        state['schema_version'] = version


def read_state(request, repo):
    return read_full_state(request, repo).get('delivery_workflow', {})


def tree(request, root, sha):
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError('Expected an immutable Git revision')
    commit = request(root + '/git/commits/' + sha)
    result = commit.get('tree', {}).get('sha', '')
    if not SHA.fullmatch(result):
        raise ValueError('Cannot establish commit content')
    return result


def pr_tree(request, root, pr):
    return tree(request, root, pr['merge_commit_sha'] if pr.get('merged') else pr['head']['sha'])


def process_module():
    try:
        from . import process_instructions
    except ImportError:
        import tracker_process_instructions as process_instructions
    return process_instructions


def route(config, pr, request=None):
    # Only an authoritative Git proof can choose this exception. Ordinary callers
    # without a request retain their previous delivery route.
    if request and 'process_instructions' in config and process_module().classify(request, config, pr):
        return 'process'
    value = policy(config)
    main = config['project']['base_branch']
    if not value:
        if pr['base']['ref'] != main:
            raise ValueError('PR must target the configured project integration branch')
        return 'main'
    stage = value['staging']
    if batch_mode(config):
        local = pr['head'].get('repo', {}).get('full_name', '').lower() == config['project']['repository'].lower()
        if not local:
            raise ValueError('Batch work must use this repository branches')
        if pr['base']['ref'] == main and pr['head']['ref'] != stage['branch']:
            branch = re.fullmatch(r'codex/batch-([a-f0-9]{32})', pr['head']['ref'])
            if not stage['enabled'] and branch:
                ids = re.findall(r'^Tracker batch:\s*([a-f0-9]{32})\s*$', pr.get('body') or '', re.M)
                if ids != [branch[1]]:
                    raise ValueError('Direct-main batch needs its exact Tracker batch identity')
                return 'batch'
            return 'approval'
        if (pr['base']['ref'] == stage['branch'] and pr['head']['ref'] not in (main, stage['branch'])
                and not re.fullmatch(r'codex/batch-[a-f0-9]{32}', pr['head']['ref'])):
            # Preserve handed-off v1 source PRs. They become approval-only and
            # cannot enter stage individually under schema 2.
            return 'approval'
        if not stage['enabled']:
            raise ValueError('Staging is disabled; use approval-only sources and the reserved batch into main')
    if stage['enabled']:
        if pr['base']['ref'] == stage['branch']:
            if pr['head']['ref'] in (main, stage['branch']):
                raise ValueError('Stage admission requires a working branch')
            return 'stage'
        if pr['base']['ref'] == main and pr['head']['ref'] == stage['branch']:
            if pr['head'].get('repo', {}).get('full_name', '').lower() != config['project']['repository'].lower():
                raise ValueError('Promotion must use this repository stage branch')
            return 'promotion'
        raise ValueError('Staged projects require working branch -> stage -> main')
    if pr['base']['ref'] != main:
        raise ValueError('Staging is disabled; PR must target main')
    return 'main'


def naming(config, pr, numbers, request=None):
    direction = route(config, pr, request) if policy(config) or 'process_instructions' in config else None
    if not policy(config) and direction != 'process':
        return
    project = config['project']
    prefix = project['id_prefix']
    ids = [project.get('legacy_task_ids', {}).get(str(n), f"{prefix}-{n + project.get('id_offset', 0)}") for n in numbers]
    title = re.match(r'^\[([^]]+)\]\s+\S', pr.get('title', ''))
    if not title or set(x.strip() for x in title[1].split(',')) != set(ids):
        raise ValueError('PR title must start with all linked IDs: [ID, ID] Summary')
    if batch_mode(config) and direction in ('stage', 'batch'):
        if not re.fullmatch(r'codex/batch-[a-f0-9]{32}', pr['head']['ref']):
            raise ValueError('Stage accepts only a reserved codex/batch-<ID> candidate')
        reject_closing(pr.get('body', '') + '\n' + pr.get('title', ''))
        return
    prefix = '(?:work|codex)/' if execution_policy(config) else 'codex/'
    if direction != 'promotion' and not any(
            re.fullmatch(prefix + re.escape(i.lower()) + r'-[a-z0-9]+(?:-[a-z0-9]+)*', pr['head']['ref']) for i in ids):
        raise ValueError('Working branch must use the configured ticket naming convention')
    reject_closing(pr.get('body', '') + '\n' + pr.get('title', ''))


def reject_closing(text):
    if re.search(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s*:?[ \t]+(?:(?:[\w.-]+/[\w.-]+)?#[0-9]+|https://github\.com/[^\s]+/issues/[0-9]+)', text, re.I):
        raise ValueError('Use explicit tracker links, not automatic issue-closing keywords')


def merge_base(request, root, pr):
    commit = request(root + '/git/commits/' + pr['merge_commit_sha'])
    parents = commit.get('parents', [])
    if len(parents) not in (1, 2) or not SHA.fullmatch(parents[0].get('sha', '')):
        raise ValueError('Cannot verify the actual merge base; use a merge commit or squash')
    return parents[0]['sha']


def summary(receipt):
    """Minimal completion projection, excluding reports, environment and history."""
    if set(receipt) <= {'policy', 'state', 'admitted', 'verified', 'non_code', 'process', 'reconciled_scope'}:
        return receipt
    non_code = bool(receipt.get('non_code') and receipt.get('report'))
    admitted = bool(receipt.get('sources') or non_code)
    result = {'policy': receipt.get('policy'), 'state': receipt.get('state', 'pending'),
            'admitted': admitted, 'non_code': non_code,
            'verified': admitted and receipt.get('state') == 'verified' and bool(receipt.get('verification') or non_code)}
    if receipt.get('process'):
        proof = receipt.get('verification') or {}
        sources = receipt.get('sources', [])
        valid = (isinstance(proof, dict) and proof.get('route') == 'process'
                 and proof.get('policy') == receipt.get('policy')
                 and all(isinstance(proof.get(k), str) and SHA.fullmatch(proof[k]) for k in ('tree_sha', 'main_sha'))
                 and isinstance(proof.get('process_policy'), str) and re.fullmatch(r'[0-9a-f]{64}', proof['process_policy'])
                 and len(sources) == 1 and sources[0].get('tree_sha') == proof.get('tree_sha')
                 and type(sources[0].get('pr')) is int and sources[0]['pr'] > 0
                 and isinstance(sources[0].get('merge_sha'), str) and SHA.fullmatch(sources[0]['merge_sha']))
        result['verified'] = result['verified'] and bool(valid)
        if valid:
            result['process'] = True
    effective, scope = reconciled_policy(receipt)
    if scope:
        result.update(policy=effective, reconciled_scope=scope)
    return result


def checked(request, root, head, required):
    found = {}
    page = 1
    while True:
        result = request(root + f'/commits/{head}/check-runs?per_page=100&page={page}')
        rows = result.get('check_runs', [])
        for row in rows:
            if row.get('head_sha') == head and row.get('app', {}).get('slug') == 'github-actions':
                old = found.get(row['name'])
                if old is None or row['id'] > old['id']:
                    found[row['name']] = row
        if len(rows) < 100:
            break
        page += 1
        if page > 100:
            raise ValueError('Too many check runs to verify safely')
    for name in required:
        row = found.get(name, {})
        if row.get('status') != 'completed' or row.get('conclusion') != 'success':
            raise ValueError('Required check is not successful on this version: ' + name)


def review_matches(receipt, config, pr, content, numbers):
    if not isinstance(receipt, dict) or receipt.get('policy') != fingerprint(config):
        return False
    if (receipt.get('verdict') != 'approved' or receipt.get('head_sha') != pr['head']['sha']
            or receipt.get('tree_sha') != content or set(receipt.get('issues', [])) != set(numbers) or not receipt.get('report')
            or not receipt.get('reviewer') or receipt.get('reviewer') == receipt.get('implementer')):
        return False
    return pr.get('merged') or receipt.get('base_sha') == pr['base']['sha']


def admit(request, config, pr, numbers):
    value = policy(config)
    process = process_module() if 'process_instructions' in config else None
    if not value and not process:
        return None
    if pr.get('draft') or (pr.get('state') == 'closed' and not pr.get('merged')):
        raise ValueError('A draft or abandoned PR cannot be admitted')
    root = 'repos/' + config['project']['repository']
    content = pr_tree(request, root, pr)
    shared = read_full_state(request, config['project']['repository'])
    execution = execution_state(shared, config)
    if process and process.classify(request, config, pr):
        return process.admit(request, config, pr, numbers, shared)
    if not value:
        return None
    if batch_mode(config):
        try:
            from . import batch_policy
        except ImportError:
            import tracker_batch_policy as batch_policy
        return batch_policy.admit(request, config, pr, numbers, shared)
    state = shared.get('delivery_workflow', {})
    direction = route(config, pr)
    if execution is not None:
        lane = execution.get('review')
        if pr.get('merged'):
            completed = execution['completed'].get(str(pr['number']))
            if completed:
                if any(execution['tasks'].get(str(n), {}).get('state') != 'merged'
                       or execution['tasks'][str(n)].get('pr') != completed['pr'] for n in completed['issues']):
                    raise ValueError('This task has a newer execution; old merge evidence cannot complete its new scope')
                lane = completed
        if not lane or lane.get('state') != 'reviewing' or set(lane.get('issues', [])) != set(numbers):
            raise ValueError('This task set does not own the sequential review/integration lane')
        if direction != 'promotion' and lane.get('pr') != pr['number']:
            raise ValueError('Another PR owns the review/integration lane')
        reviewed = state.get('reviews', {}).get(str(lane['pr']), {})
        if (reviewed.get('execution_lane') != lane.get('id') or reviewed.get('reviewer') != lane.get('actor')
                or reviewed.get('review_session') != lane.get('session')):
            raise ValueError('Review must be recorded by the current independent lane owner')
    if direction == 'promotion':
        receipt = state.get('promotions', {}).get(str(pr['number']), {}) if pr.get('merged') else {}
        receipt = receipt or state.get('stage', {})
        if (receipt.get('state') != 'passed' or receipt.get('tree_sha') != content
                or receipt.get('policy') != fingerprint(config)
                or set(receipt.get('issues', [])) != set(numbers)):
            raise ValueError('Stage has no passing receipt for this exact content and task set')
        baseline = merge_base(request, root, pr) if pr.get('merged') else pr['base']['sha']
        if tree(request, root, baseline) != receipt.get('main_tree'):
            raise ValueError('Main changed after staging; rebase, review and stage again')
    else:
        receipt = state.get('reviews', {}).get(str(pr['number']))
        if not review_matches(receipt, config, pr, content, numbers):
            raise ValueError('Independent review is missing or stale for this content')
        if pr.get('merged') and merge_base(request, root, pr) != receipt.get('base_sha'):
            raise ValueError('Merged base differs from the reviewed base; reconcile with a new reviewed PR')
        if direction == 'stage':
            main_ref = request(root + '/git/ref/heads/' + quote(config['project']['base_branch'], safe=''))
            if receipt.get('main_sha') != main_ref['object']['sha']:
                raise ValueError('Main changed after review; reconcile and record a fresh review')
            active = state.get('stage', {})
            if active and active.get('state') != 'promoted' and set(active.get('issues', [])) != set(numbers):
                raise ValueError('Another task set owns stage; finish its promotion first')
            if not active or active.get('state') == 'promoted':
                if (tree(request, root, receipt['base_sha']) != tree(request, root, main_ref['object']['sha'])
                        and not (process and process.stage_base_compatible(request, config, receipt['base_sha'], main_ref['object']['sha'], shared))):
                    raise ValueError('Stage must start from current main or an exactly receipted instruction-only gap')
    page = 1
    while True:
        commits = request(root + f'/pulls/{pr["number"]}/commits?per_page=100&page={page}')
        for commit in commits:
            reject_closing(commit.get('commit', {}).get('message', ''))
        if len(commits) < 100:
            break
        page += 1
        if page > 100:
            raise ValueError('Too many commits to verify safely')
    checked(request, root, pr['head']['sha'], value['required_checks'])
    return {'tree_sha': content, 'policy': fingerprint(config), 'route': direction,
            'main_sha': main_ref['object']['sha'] if direction == 'stage' else None}


def dependencies_ready(request, config, numbers, shared):
    project = config['project']; root = 'repos/' + project['repository']
    stages = {s if isinstance(s, str) else s['id']: s if isinstance(s, str) else s['phase'] for s in config['workflow']}
    for number in numbers:
        issue = request(root + f'/issues/{number}')
        for identifier in dependencies(issue.get('body') or '', project['repository']):
            n = None
            if identifier.startswith('#'):
                n = int(identifier[1:])
            else:
                n = next((int(k) for k,v in project.get('legacy_task_ids', {}).items() if v.lower() == identifier.lower()), None)
                if n is None and re.fullmatch(re.escape(project['id_prefix']) + r'-[1-9]\d*', identifier, re.I):
                    n = int(identifier.split('-')[1]) - project.get('id_offset', 0)
            if not n or n <= 0:
                raise ValueError(f'#{number}: dependency cannot be verified: {identifier}')
            dependency = request(root + f'/issues/{n}')
            labels = {x['name'] for x in dependency['labels']}
            statuses = [stages.get(x[7:]) for x in labels if x.startswith('status:')]
            if ('pull_request' in dependency or statuses != ['merged'] or 'flag:archived' in labels
                    or 'flag:blocked' in labels or dependency.get('state_reason') == 'not_planned'):
                raise ValueError(f'#{number}: dependency is unfinished: {identifier}')
            if not policy(config):
                continue
            receipt = shared.get('delivery_workflow', {}).get('tasks', {}).get(str(n), {})
            historical = config.get('historical_completion', {})
            accepted = historical.get('tasks', {}).get(str(n), {})
            expected_id = project.get('legacy_task_ids', {}).get(str(n), f"{project['id_prefix']}-{n + project.get('id_offset', 0)}")
            if (not receipt and historical.get('policy') == fingerprint(config)
                    and accepted.get('id') == expected_id and accepted.get('issue_sha256') == accepted_scope_digest(dependency)):
                continue
            projected = summary(receipt)
            if (projected.get('policy') != fingerprint(config) or not projected.get('admitted')
                    or projected.get('reconciled_scope', task_scope(dependency)) != task_scope(dependency)
                    or policy(config)['completion'] != 'reviewed-merge'
                    and not projected.get('non_code') and not projected.get('verified')):
                raise ValueError(f'#{number}: dependency delivery is unfinished: {identifier}')
