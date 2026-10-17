"""Portable, opt-in instruction-only proof from complete immutable Git objects."""
import base64
import hashlib
import json
import re
import subprocess
from urllib.parse import quote
try:
    from . import delivery_policy as rules
except ImportError:
    import tracker_delivery_policy as rules


def policy(config):
    if 'process_instructions' not in config:
        return None
    value = config['process_instructions']
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'required_checks'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1):
        raise ValueError('Invalid process_instructions policy')
    checks = value['required_checks']
    if (not isinstance(checks, list) or not checks or any(not isinstance(x, str) or not x.strip() or x != x.strip() for x in checks)
            or len(checks) != len(set(checks)) or not {'descriptor', 'tracker-link'} <= set(checks)):
        raise ValueError('Process instructions need unique actual checks including descriptor and tracker-link')
    if not rules.policy(config):
        raise ValueError('Process instructions require the independent-review delivery policy')
    return value


def fingerprint(config):
    return hashlib.sha256(json.dumps(policy(config), sort_keys=True).encode()).hexdigest()


def scope(issue, config=None):
    if config and 'task_context_policy' in config:
        return rules.task_scope(issue, config)
    labels = sorted(x['name'] for x in issue['labels'] if not x['name'].startswith(('status:', 'priority:', 'release:')))
    return hashlib.sha256(json.dumps({k: issue.get(k) for k in ('number', 'title', 'body')}
                                   | {'labels': labels}, sort_keys=True).encode()).hexdigest()


def safe_path(path):
    return (isinstance(path, str) and bool(path) and not path.startswith('/') and '\\' not in path
            and not any(ord(c) < 32 or ord(c) == 127 for c in path)
            and all(part not in ('', '.', '..') for part in path.split('/')))


def instruction_path(path):
    return safe_path(path) and (path in ('AGENTS.md', 'CLAUDE.md')
                               or path.startswith('docs/repository/') and path.endswith('.md'))


def entries(request, root, tree_sha):
    value = request(root + '/git/trees/' + tree_sha + '?recursive=1')
    if (not isinstance(value, dict) or value.get('sha') != tree_sha or value.get('truncated') is not False
            or not isinstance(value.get('tree'), list)):
        raise ValueError('Process diff needs a complete immutable Git tree; missing/truncated trees cannot qualify')
    result, seen = {}, set()
    for row in value['tree']:
        if not isinstance(row, dict):
            raise ValueError('Invalid Git tree entry')
        path = row.get('path')
        if (not safe_path(path) or path in seen or not isinstance(row.get('mode'), str) or row.get('type') not in ('tree', 'blob', 'commit')
                or not rules.SHA.fullmatch(row.get('sha', ''))):
            raise ValueError('Unsafe or incomplete Git tree entry')
        seen.add(path)
        if row['type'] == 'tree':
            if row.get('mode') != '040000':
                raise ValueError('Invalid Git directory mode')
            continue
        result[path] = {k: row[k] for k in ('sha', 'mode', 'type')}
    return result


def blob(request, root, sha):
    value = request(root + '/git/blobs/' + sha)
    if (not isinstance(value, dict) or value.get('sha') != sha or value.get('encoding') != 'base64'
            or type(value.get('size')) is not int or not 0 <= value['size'] <= 1024 * 1024
            or not isinstance(value.get('content'), str)):
        raise ValueError('Process diff needs complete bounded Git blobs')
    try:
        data = base64.b64decode(''.join(value['content'].split()), validate=True)
        digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if len(data) != value['size'] or digest != sha or b'\0' in data:
            raise ValueError()
        return data.decode('utf-8')
    except (ValueError, UnicodeError):
        raise ValueError('Missing, binary or truncated process diff content') from None


def tree_proof(request, config, base_sha, head_sha):
    root = 'repos/' + config['project']['repository']
    base_tree, head_tree = rules.tree(request, root, base_sha), rules.tree(request, root, head_sha)
    before, after = entries(request, root, base_tree), entries(request, root, head_tree)
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    if not changed or any(not instruction_path(path) for path in changed):
        return None
    for path in changed:
        if any(row and (row['type'] != 'blob' or row['mode'] != '100644') for row in (before.get(path), after.get(path))):
            raise ValueError('Process instructions require normal regular files on both sides of the diff')
    # A caller's temporary config cannot activate the exception. The reviewed
    # base already contains the exact project-owned opt-in, and its config is not
    # part of the allowed instruction diff.
    stored = before.get('tracker/config.json')
    if not stored or stored['type'] != 'blob' or stored['mode'] != '100644':
        raise ValueError('Process opt-in must already exist in the authoritative base config')
    try:
        authoritative = json.loads(blob(request, root, stored['sha']))
    except (TypeError, json.JSONDecodeError):
        raise ValueError('Invalid authoritative process configuration') from None
    if (policy(authoritative) != policy(config) or authoritative.get('project', {}).get('repository') != config['project']['repository']
            or authoritative.get('project', {}).get('base_branch') != config['project']['base_branch']):
        raise ValueError('Process opt-in does not match the authoritative base configuration')
    descriptor = before.get('delivery.json')
    if not descriptor or descriptor['type'] != 'blob' or descriptor['mode'] != '100644':
        raise ValueError('Process opt-in must already exist in the base workflow descriptor')
    try:
        mirrored = json.loads(blob(request, root, descriptor['sha'])).get('workflow', {}).get('process_instructions')
    except (TypeError, AttributeError, json.JSONDecodeError):
        raise ValueError('Invalid authoritative workflow descriptor') from None
    if mirrored != policy(config):
        raise ValueError('Process opt-in differs from the authoritative workflow descriptor')
    manifest = []
    for path in changed:
        old, new = before.get(path), after.get(path)
        for row in (old, new):
            if row:
                blob(request, root, row['sha'])
        manifest.append({'path': path, 'before': old, 'after': new})
    return {'base_sha': base_sha, 'head_sha': head_sha, 'tree_sha': head_tree, 'files': manifest,
            'diff_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}


def classify(request, config, pr):
    if not policy(config):
        return None
    main = config['project']['base_branch']
    stage = (rules.policy(config) or {}).get('staging', {}).get('branch')
    if pr['base']['ref'] != main or pr['head']['ref'] in (main, stage):
        return None
    if any((pr[side].get('repo') or {}).get('full_name', '').lower() != config['project']['repository'].lower()
           for side in ('base', 'head')):
        return None
    root = 'repos/' + config['project']['repository']
    base = rules.merge_base(request, root, pr) if pr.get('merged') else pr['base']['sha']
    proof = tree_proof(request, config, base, pr['head']['sha'])
    if proof and request(root + '/compare/' + base + '...' + pr['head']['sha']).get('status') not in ('ahead', 'identical'):
        raise ValueError('Process source must include its exact reviewed main baseline')
    if proof and pr.get('merged') and rules.pr_tree(request, root, pr) != proof['tree_sha']:
        raise ValueError('Actual process merge contains content outside the reviewed source')
    return proof


def classify_local(config, repository, base_sha, head_sha):
    """Use the same predicate in CI without network or a shared-engine token."""
    if not policy(config):
        return None
    if not all(isinstance(x, str) and rules.SHA.fullmatch(x) for x in (base_sha, head_sha)):
        raise ValueError('Local process classification needs exact immutable revisions')
    def git(*args):
        result = subprocess.run(['git', '-C', str(repository), *args], capture_output=True, check=False)
        if result.returncode:
            raise ValueError('Cannot establish complete local Git process proof')
        return result.stdout
    git('merge-base', '--is-ancestor', base_sha, head_sha)
    root = 'repos/' + config['project']['repository']
    def request(path):
        suffix = path.removeprefix(root)
        if suffix.startswith('/git/commits/'):
            sha = suffix.rsplit('/', 1)[1]
            if git('cat-file', '-t', sha).strip() != b'commit':
                raise ValueError('Expected a local Git commit')
            lines = git('cat-file', '-p', sha).decode().splitlines()
            return {'tree': {'sha': next(x[5:] for x in lines if x.startswith('tree '))}}
        if suffix.startswith('/git/trees/'):
            sha = suffix.split('/')[3].split('?')[0]
            rows = []
            for entry in git('ls-tree', '-r', '-z', '--full-tree', sha).split(b'\0'):
                if not entry:
                    continue
                metadata, path = entry.split(b'\t', 1)
                mode, kind, oid = metadata.decode().split()
                rows.append({'path': path.decode('utf-8'), 'mode': mode, 'type': kind, 'sha': oid})
            return {'sha': sha, 'truncated': False, 'tree': rows}
        if suffix.startswith('/git/blobs/'):
            sha = suffix.rsplit('/', 1)[1]
            if git('cat-file', '-t', sha).strip() != b'blob':
                raise ValueError('Expected a local Git blob')
            data = git('cat-file', 'blob', sha)
            return {'sha': sha, 'size': len(data), 'encoding': 'base64', 'content': base64.b64encode(data).decode()}
        raise ValueError('Unsupported local process proof request')
    try:
        return tree_proof(request, config, base_sha, head_sha)
    except (UnicodeError, StopIteration):
        raise ValueError('Incomplete local process Git proof') from None


def idle(shared, config):
    section = rules.execution_state(shared, config)
    stage = shared.get('delivery_workflow', {}).get('stage', {})
    return not (rules.pending_delivery(shared, config) or stage and stage.get('state') != 'promoted' or section and
                (section.get('integration') or section.get('intake') and section['intake'].get('state') == 'running'))


def admit(request, config, pr, numbers, shared):
    proof = classify(request, config, pr)
    if not proof or len(numbers) != 1:
        raise ValueError('Process admission requires exactly one genuine instruction-only task')
    root = 'repos/' + config['project']['repository']
    section = rules.execution_state(shared, config)
    completed = section and section['completed'].get(str(pr['number'])) if pr.get('merged') else None
    lane = completed or rules.review_lane(section, pr=pr['number'])
    if completed and (completed.get('route') != 'process' or any(
            section['tasks'].get(str(n), {}).get('state') != 'merged'
            or section['tasks'][str(n)].get('pr') != completed.get('pr') for n in numbers)):
        raise ValueError('Old process merge cannot complete reopened work')
    if not completed:
        if not idle(shared, config):
            raise ValueError('Finish held stage/integration work before a process merge')
    review = shared.get('delivery_workflow', {}).get('reviews', {}).get(str(pr['number']), {})
    if (review.get('process_policy') != fingerprint(config) or review.get('process_proof') != proof
            or not rules.review_matches(review, config, pr, proof['tree_sha'], numbers)
            or review.get('base_sha') != proof['base_sha']):
        raise ValueError('Independent process review is missing or stale')
    if section and (not lane or lane.get('route') != 'process' or lane.get('pr') != pr['number']
                    or lane.get('state') != 'reviewing' or set(lane.get('issues', [])) != set(numbers)
                    or review.get('execution_lane') != lane['id'] or review.get('reviewer') != lane['actor']
                    or review.get('review_session') != lane['session']):
        raise ValueError('Process merge requires its genuine independent source review lane')
    if section is None:
        task = shared.get('delivery_workflow', {}).get('tasks', {}).get(str(numbers[0]), {})
        if not task.get('completed_at') and (shared.get('active') or {}).get('issue') != numbers[0]:
            raise ValueError('Legacy process work must retain its actual task slot through completion')
    for n in numbers:
        issue = request(root + f'/issues/{n}')
        if section is None and task.get('completed_at'):
            stages = {x if isinstance(x, str) else x['id']: x if isinstance(x, str) else x['phase'] for x in config['workflow']}
            actual = [stages.get(x['name'][7:]) for x in issue['labels'] if x['name'].startswith('status:')]
            if actual != ['merged']:
                raise ValueError('Old process merge cannot complete reopened legacy work')
        if not rules.scope_matches(issue, review.get('issue_scopes', {}).get(str(n)), config):
            raise ValueError('Process task scope changed after independent review')
    current = request(root + '/git/ref/heads/' + quote(config['project']['base_branch'], safe=''))['object']['sha']
    if pr.get('merged'):
        if request(root + '/compare/' + pr['merge_commit_sha'] + '...' + current).get('status') not in ('ahead', 'identical'):
            raise ValueError('Actual process merge is no longer included in current main')
    else:
        if current != proof['base_sha']:
            raise ValueError('Main changed after process review')
    rules.dependencies_ready(request, config, numbers, shared)
    page = 1
    while True:
        commits = request(root + f'/pulls/{pr["number"]}/commits?per_page=100&page={page}')
        for commit in commits:
            rules.reject_closing(commit.get('commit', {}).get('message', ''))
        if len(commits) < 100:
            break
        page += 1
        if page > 100:
            raise ValueError('Too many commits to verify safely')
    rules.checked(request, root, pr['head']['sha'], policy(config)['required_checks'])
    return {'route': 'process', 'tree_sha': proof['tree_sha'], 'main_sha': proof['base_sha'],
            'policy': rules.fingerprint(config), 'process_policy': fingerprint(config)}


def stage_base_compatible(request, config, stage_sha, main_sha, shared):
    """Allow only an idle stage gap composed of retained verified process merges.

    This neither moves a ref nor permits held work to change its frozen baseline.
    The next ordinary candidate still includes exact current main, full source
    approvals, independent combined review and normal stage/delivery checks.
    """
    if not policy(config):
        return False
    root = 'repos/' + config['project']['repository']
    proof = tree_proof(request, config, stage_sha, main_sha)
    if not proof or request(root + '/compare/' + stage_sha + '...' + main_sha).get('status') not in ('ahead', 'identical'):
        return False
    stage_tree = rules.tree(request, root, stage_sha)
    cursor = main_sha
    for _ in range(64):
        if rules.tree(request, root, cursor) == stage_tree:
            return request(root + '/compare/' + stage_sha + '...' + cursor).get('status') in ('ahead', 'identical')
        matches = [(row, source) for row in shared.get('delivery_workflow', {}).get('tasks', {}).values()
                   if row.get('process') and row.get('state') == 'verified'
                   for source in row.get('sources', []) if source.get('merge_sha') == cursor]
        if len(matches) != 1:
            return False
        row, source = matches[0]
        pr = request(root + f'/pulls/{source["pr"]}')
        review = shared.get('delivery_workflow', {}).get('reviews', {}).get(str(source['pr']), {})
        if (row.get('policy') != rules.fingerprint(config) or not pr.get('merged')
                or pr.get('merge_commit_sha') != cursor or source.get('tree_sha') != rules.pr_tree(request, root, pr)
                or row.get('verification', {}).get('route') != 'process'):
            return False
        admission = admit(request, config, pr, review.get('issues', []), shared)
        if row.get('verification') != admission:
            return False
        cursor = admission['main_sha']
    raise ValueError('Stage process reconciliation exceeds bounded retained history')
