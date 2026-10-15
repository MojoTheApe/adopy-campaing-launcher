"""Portable read-only schema-2 approval and frozen-batch admission.

Exported beside the PR checker. Independent review is an attestation; GitHub
permissions and protection remain authoritative, as for the v1 contract.
"""
import hashlib
import json
import re
from urllib.parse import quote
try:
    from . import delivery_policy as rules, pr_checks
except ImportError:
    import tracker_delivery_policy as rules
    import tracker_pr_check as pr_checks


def scope(issue):
    labels = sorted(x['name'] for x in issue['labels']
                    if not x['name'].startswith(('status:', 'priority:', 'release:')))
    # Status writes, scheduling and release rollover do not edit accepted scope.
    return hashlib.sha256(json.dumps({k: issue.get(k) for k in ('number', 'title', 'body')}
                                   | {'labels': labels}, sort_keys=True).encode()).hexdigest()


def source(request, config, approval, baseline, *, phases=('approved',), pending=False, completed=False, frozen=False, included_in=None):
    root = 'repos/' + config['project']['repository']
    number = approval['issue']
    issue = request(root + f'/issues/{number}')
    labels = {x['name'] for x in issue['labels']}
    mapping = {s if isinstance(s, str) else s['id']: s if isinstance(s, str) else s['phase'] for s in config['workflow']}
    actual = {mapping.get(x[7:]) for x in labels if x.startswith('status:')}
    for prefix, allowed in (('module:', {m['id'] for m in config['modules']}),
                            ('type:', {'feature', 'bug', 'improvement', 'research', 'operations'}),
                            ('priority:', {'high', 'medium', 'low'})):
        selected = [x[len(prefix):] for x in labels if x.startswith(prefix)]
        if len(selected) != 1 or selected[0] not in allowed:
            raise ValueError(f'#{number}: approved task needs valid classification')
    if (not actual or not actual <= set(phases) or len(actual) != 1 and not pending
            or 'flag:blocked' in labels or 'flag:archived' in labels
            or issue.get('state_reason') == 'not_planned' or 'pull_request' in issue
            or issue.get('state') == 'closed' and not completed
            or scope(issue) != approval.get('scope')):
        raise ValueError(f'#{number}: approved scope/status changed; return to its author and review again')
    pr = request(root + f'/pulls/{approval["pr"]}')
    review = approval.get('review', {})
    included_merge = bool(pr.get('merged') and included_in)
    if (pr.get('merged') and not included_merge or pr.get('draft')
            or pr.get('state') != 'open' and not (completed or included_merge)
            or rules.route(config, pr, request) != 'approval'
            or pr_checks.linked_numbers(pr.get('body'), config) != [number]
            or pr['head']['ref'] != approval.get('branch')
            or pr['head']['sha'] != approval.get('head_sha')
            or not frozen and pr['base']['sha'] != approval.get('base_sha')
            or approval.get('main_sha') != baseline
            or approval.get('policy') != rules.fingerprint(config)):
        raise ValueError(f'#{number}: source approval is stale for its PR, main or policy')
    # GitHub auto-marks main-targeted sources merged when promotion contains their
    # tips. Only post-main proof (or a completed batch's archived stage proof)
    # may accept that state. Its merge_commit_sha is not the batch admission
    # evidence. Bind inclusion to the frozen head instead.
    if included_in:
        comparison = request(root + '/compare/' + approval['head_sha'] + '...' + included_in)
        if comparison.get('status') not in ('ahead', 'identical'):
            raise ValueError(f'#{number}: approved source head is not included in the batch main merge')
    rules.naming(config, pr, [number], request)
    content = rules.tree(request, root, pr['head']['sha'])
    # A PR's live base follows main after promotion. Frozen member proof retains
    # its reviewed baseline; a later intake must revalidate the new live base.
    frozen = dict(pr, base=dict(pr['base'], sha=approval['base_sha']))
    if (rules.tree(request, root, approval['base_sha']) != rules.tree(request, root, baseline)
            or not rules.review_matches(review, config, frozen, content, [number])
            or review.get('main_sha') != baseline):
        raise ValueError(f'#{number}: independent source review does not match the frozen baseline')
    if not completed:
        rules.checked(request, root, pr['head']['sha'], rules.policy(config)['required_checks'])
    return pr


def composition(request, root, head, baseline, sources):
    """Candidate consists solely of the baseline, approved tips and merge commits.

    Review still inspects conflict resolution and the resulting combined tree.
    Ordinary extra implementation commits on the integration branch are rejected.
    """
    stops = {baseline, *sources}
    found, seen, pending = set(), set(), [head]
    while pending:
        sha = pending.pop()
        if sha in stops:
            found.add(sha)
            continue
        if sha in seen:
            continue
        seen.add(sha)
        if len(seen) > 64:
            raise ValueError('Candidate merge graph exceeds the bounded batch composition')
        commit = request(root + '/git/commits/' + sha)
        parents = [p['sha'] for p in commit.get('parents', [])]
        if len(parents) != 2:
            raise ValueError('Integration branch contains work outside the approved source tips')
        rules.reject_closing(commit.get('message', ''))
        pending.extend(parents)
    if found != stops:
        raise ValueError('Candidate must include the exact main baseline and every approved source tip')


def batch_id(pr):
    lines = re.findall(r'^Tracker batch:\s*([a-f0-9]{32})\s*$', pr.get('body') or '', re.M)
    if len(lines) != 1:
        raise ValueError('Batch PR needs one Tracker batch: <ID> line')
    return lines[0]


def dependencies_ready(request, config, numbers, shared):
    return rules.dependencies_ready(request, config, numbers, shared)


def validate_candidate(request, config, pr, numbers, batch, shared, *, completed=False):
    root = 'repos/' + config['project']['repository']
    if (batch_id(pr) != batch.get('id') or set(numbers) != set(batch.get('issues', []))
            or not numbers or len(numbers) > rules.execution_policy(config)['batch_limit']):
        raise ValueError('Candidate does not match the frozen task set or configured batch limit')
    direction = rules.route(config, pr, request)
    if direction == 'stage' and pr['head']['ref'] != batch.get('branch'):
        raise ValueError('Candidate must use the reserved integration branch')
    if direction == 'stage' and not completed and batch.get('state') == 'fixing':
        raise ValueError('Refresh returned member approvals before reviewing a rebuilt batch')
    baseline = batch['main_sha']
    if not completed:
        current = request(root + '/git/ref/heads/' + quote(config['project']['base_branch'], safe=''))['object']['sha']
        if direction == 'stage' and current != baseline:
            recovered = (pr.get('merged') and rules.tree(request, root, current) == rules.pr_tree(request, root, pr)
                         and request(root + '/compare/' + pr['merge_commit_sha'] + '...' + current).get('status') in ('ahead', 'identical'))
            if not recovered:
                raise ValueError('Main changed; refresh member approvals and rebuild the held batch')
    for member in batch['members']:
        source(request, config, member, baseline, phases=('approved', 'stage', 'merged'),
               pending=bool(pr.get('merged')), completed=completed, frozen=True,
               included_in=pr['merge_commit_sha'] if pr.get('merged') and (direction == 'promotion' or completed) else None)
    if not completed:
        dependencies_ready(request, config, numbers, shared)
    if direction == 'stage':
        composition(request, root, pr['head']['sha'], baseline, [m['head_sha'] for m in batch['members']])
        stage = shared.get('delivery_workflow', {}).get('stage', {})
        if stage and stage.get('state') != 'promoted' and stage.get('batch_id') != batch['id']:
            raise ValueError('Another candidate owns stage')
        if not stage or stage.get('state') == 'promoted':
            base = rules.merge_base(request, root, pr) if pr.get('merged') else pr['base']['sha']
            if (rules.tree(request, root, base) != rules.tree(request, root, baseline)
                    and not ('process_instructions' in config and rules.process_module().stage_base_compatible(request, config, base, baseline, shared))):
                raise ValueError('Stage must start with current main or an exactly receipted instruction-only gap')
    return direction


def admit(request, config, pr, numbers, shared):
    direction = rules.route(config, pr, request)
    if direction == 'approval':
        raise ValueError('Source PRs are approved independently; merge the frozen batch into stage')
    section = rules.execution_state(shared, config)
    batch = section.get('integration')
    completed = section['batches'].get(batch_id(pr)) if pr.get('merged') else None
    archived = bool(completed and completed.get('state') == 'merged')
    if archived:
        batch = completed
        if any(section['tasks'].get(str(n), {}).get('state') != 'merged'
               or section['tasks'][str(n)].get('batch_id') != batch['id'] for n in batch['issues']):
            raise ValueError('Old batch cannot complete a newer execution of a task')
    if not batch:
        raise ValueError('No Coordinator owns this batch')
    validate_candidate(request, config, pr, numbers, batch, shared, completed=archived)
    root = 'repos/' + config['project']['repository']
    content = rules.pr_tree(request, root, pr)
    lane = batch.get('review') if archived else section.get('review')
    reviewed = shared.get('delivery_workflow', {}).get('reviews', {}).get(str(batch.get('pr')), {})
    if (not lane or lane.get('batch_id') != batch['id'] or lane.get('state') != 'reviewing'
            or reviewed.get('execution_lane') != lane.get('id') or reviewed.get('reviewer') != lane.get('actor')
            or reviewed.get('review_session') != lane.get('session')
            or reviewed.get('batch_id') != batch['id']
            or reviewed.get('source_heads') != [m['head_sha'] for m in batch['members']]):
        raise ValueError('Combined content requires the current independent integration review')
    if direction == 'stage':
        if batch.get('pr') != pr['number'] or not rules.review_matches(reviewed, config, pr, content, numbers):
            raise ValueError('Combined candidate review is missing or stale')
        if pr.get('merged') and rules.merge_base(request, root, pr) != reviewed['base_sha']:
            raise ValueError('Actual stage merge differs from the reviewed base')
    else:
        receipt = shared.get('delivery_workflow', {}).get('promotions', {}).get(str(pr['number']), {}) if pr.get('merged') else {}
        receipt = receipt or shared.get('delivery_workflow', {}).get('stage', {})
        if (receipt.get('state') != 'passed' or receipt.get('batch_id') != batch['id']
                or receipt.get('tree_sha') != content or receipt.get('policy') != rules.fingerprint(config)
                or set(receipt.get('issues', [])) != set(numbers) or reviewed.get('tree_sha') != content):
            raise ValueError('Promotion needs a passing verification of the exact reviewed batch tree')
        baseline = rules.merge_base(request, root, pr) if pr.get('merged') else pr['base']['sha']
        if baseline != batch['main_sha'] or rules.tree(request, root, baseline) != receipt.get('main_tree'):
            raise ValueError('Main changed after staging; rebuild and verify the held batch')
    rules.checked(request, root, pr['head']['sha'], rules.policy(config)['required_checks'])
    for page in range(1, 101):
        rows = request(root + f'/pulls/{pr["number"]}/commits?per_page=100&page={page}')
        for row in rows:
            rules.reject_closing(row.get('commit', {}).get('message', ''))
        if len(rows) < 100:
            break
    else:
        raise ValueError('Too many commits to verify safely')
    return {'tree_sha': content, 'policy': rules.fingerprint(config), 'route': direction,
            'main_sha': batch['main_sha'] if direction == 'stage' else None, 'batch_id': batch['id']}
