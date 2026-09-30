"""Portable, read-only admission rules for the opt-in Master Repo contract.

Copied beside the standalone PR checker by export-template. No write operations,
model invocation, installation or deployment takes place in this module.
"""
import base64
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
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'implementation_limit', 'review_limit'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['implementation_limit']) is not int or not 1 <= value['implementation_limit'] <= 16
            or type(value['review_limit']) is not int or value['review_limit'] != 1):
        raise ValueError('Execution policy needs schema_version 1, implementation_limit 1..16 and review_limit 1')
    if not policy(config):
        raise ValueError('Parallel execution requires the independent-review delivery policy')
    return value


def execution_state(state, config):
    value = execution_policy(config)
    if not value:
        if state.get('schema_version') == 2:
            raise ValueError('Shared execution state requires the adopted execution policy; restore the pinned config')
        return None
    section = state.get('execution')
    if (state.get('schema_version') != 2 or not isinstance(section, dict)
            or section.get('policy') != value):
        raise ValueError('Run execution migrate while the repository is idle to adopt this execution policy')
    for key in ('claims', 'tasks', 'actors', 'completed'):
        if not isinstance(section.get(key), dict):
            raise ValueError('Invalid execution state: ' + key)
    if not isinstance(section.get('review_queue'), list) or not isinstance(section.get('history'), list):
        raise ValueError('Invalid execution queue/history')
    if section.get('review') is not None and not isinstance(section['review'], dict):
        raise ValueError('Invalid review lane')
    if state.get('active') or len(section['claims']) > value['implementation_limit']:
        raise ValueError('Conflicting execution owners; inspect shared state')
    return section


def read_full_state(request, repo):
    row = request('repos/' + repo + '/contents/.tracker-state.json?ref=tracker-state')
    state = json.loads(base64.b64decode(row['content'], validate=False))
    if state.get('schema_version') not in (1, 2):
        raise ValueError('Invalid tracker state')
    return state


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


def route(config, pr):
    value = policy(config)
    main = config['project']['base_branch']
    if not value:
        if pr['base']['ref'] != main:
            raise ValueError('PR must target the configured project integration branch')
        return 'main'
    stage = value['staging']
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


def naming(config, pr, numbers):
    if not policy(config):
        return
    project = config['project']
    prefix = project['id_prefix']
    ids = [project.get('legacy_task_ids', {}).get(str(n), f"{prefix}-{n + project.get('id_offset', 0)}") for n in numbers]
    title = re.match(r'^\[([^]]+)\]\s+\S', pr.get('title', ''))
    if not title or set(x.strip() for x in title[1].split(',')) != set(ids):
        raise ValueError('PR title must start with all linked IDs: [ID, ID] Summary')
    prefix = '(?:work|codex)/' if execution_policy(config) else 'codex/'
    if route(config, pr) != 'promotion' and not any(
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
    if set(receipt) <= {'policy', 'state', 'admitted', 'verified', 'non_code'}:
        return receipt
    non_code = bool(receipt.get('non_code') and receipt.get('report'))
    admitted = bool(receipt.get('sources') or non_code)
    return {'policy': receipt.get('policy'), 'state': receipt.get('state', 'pending'),
            'admitted': admitted, 'non_code': non_code,
            'verified': admitted and receipt.get('state') == 'verified' and bool(receipt.get('verification') or non_code)}


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
    if not value:
        return None
    if pr.get('draft') or (pr.get('state') == 'closed' and not pr.get('merged')):
        raise ValueError('A draft or abandoned PR cannot be admitted')
    root = 'repos/' + config['project']['repository']
    content = pr_tree(request, root, pr)
    shared = read_full_state(request, config['project']['repository'])
    execution = execution_state(shared, config)
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
                if tree(request, root, receipt['base_sha']) != tree(request, root, main_ref['object']['sha']):
                    raise ValueError('Stage must start from the current main content')
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
