"""Opt-in task requirements and exact-bound human assessments.

Read-only functions are exported beside admission. Mutations import the owned
engine lazily; no semantic conclusion is inferred from paths, labels or time.
"""
import copy
import hashlib
import json
import re
import uuid
from urllib.parse import quote
try:
    from . import delivery_policy as rules
except ImportError:
    import tracker_delivery_policy as rules

START = '<!-- tracker-journal:start -->'
END = '<!-- tracker-journal:end -->'


def policy(config):
    if 'task_context_policy' not in config:
        return None
    value = config['task_context_policy']
    if (not isinstance(value, dict) or set(value) != {'schema_version'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1):
        raise ValueError('Invalid task_context_policy')
    if not rules.batch_mode(config):
        raise ValueError('Task context requires explicit execution schema 2 or 3')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def requirements(body):
    body = body or ''
    if START not in body and END not in body:
        return body.rstrip()
    if body.count(START) != 1 or body.count(END) != 1:
        raise ValueError('Use one paired terminal tracker journal')
    lines = body.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == START]
    ends = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == END]
    if (len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]
            or ''.join(lines[ends[0] + 1:]).strip()):
        raise ValueError('Tracker journal markers must be exact lines at the end of the Issue')
    journal = ''.join(lines[starts[0] + 1:ends[0]])
    if re.search(r'(?mi)^\s*[-*+]\s+\[[ x]\]|\bneeds(?:\*\*)?\s*:|^\s*Blocked\s*:', journal):
        raise ValueError('Keep criteria, dependencies and blockers outside the tracker journal')
    return ''.join(lines[:starts[0]]).rstrip()


def scope_matches(issue, expected, config):
    if isinstance(expected, str) and expected.startswith('v2:'):
        return bool(policy(config) and rules.task_scope(issue, config) == expected)
    # Historical proof retains its original, conservative whole-body contract.
    return rules.task_scope(issue) == expected


def baseline(api, config):
    return api.request(api.root + '/git/ref/heads/' + quote(config['project']['base_branch'], safe=''))['object']['sha']


def read(api, config, store):
    from . import execution
    if not policy(config):
        raise ValueError('Adopt task_context_policy explicitly before using task context commands')
    return execution.read(store, config)


def adopt(api, config, store, args):
    """Held, idle, official-config activation; never rehash historical receipts."""
    from . import execution, maintenance
    value = policy(config)
    if not value:
        raise ValueError('Commit the explicit task context policy first')
    state, sha = store.read(optional=True)
    rules.completion_adoption_order(state, config)
    if state.get('task_context', {}).get('policy') == value:
        rules.execution_state(state, config)
        return {'already_adopted': True, 'applied': False}
    maintenance.owned(state, args)
    if maintenance.busy(state):
        raise ValueError('Drain actual owners, pending operations, intake and stage before adoption')
    previous = dict(config)
    previous.pop('task_context_policy', None)
    rules.execution_state(state, previous)
    ref = baseline(api, config)
    row = api.request(api.root + '/contents/tracker/config.json?ref=' + ref)
    if json.loads(rules.file_bytes(api.request, api.root, row)) != config:
        raise ValueError('Task context adoption requires the exact official current-main config')
    for item in api.issues():
        try:
            requirements(item.get('body'))
        except ValueError as error:
            raise ValueError(f'#{item["number"]}: inspect incompatible journal before adoption: {error}') from None
    text = maintenance.report(args)
    state['task_context'] = {'policy': copy.deepcopy(value), 'base_schema_version': 2,
                             'readiness': {}, 'history': [{'event': 'adopted', 'main_sha': ref, 'report': text}]}
    if state.get('completion'):
        state['completion']['base_schema_version'] = 4
    else:
        state['maintenance']['base_schema_version'] = 4
    execution.audit(state['execution'], 'task-context-adopted', main_sha=ref)
    rules.execution_state(state, config)
    if args.apply:
        store.write(state, sha)
    return {'applied': args.apply, 'policy': value, 'instruction': 'Old evidence keeps its original scope semantics; resume owned maintenance after compatible publication.'}


def readiness_identity(api, config, number, actor, session):
    return dict(kind='task-readiness', repository=config['project']['repository'], issue=number,
                scope=rules.task_scope(api.issue(number), config), main_sha=baseline(api, config),
                actor=actor, session=session)


def assessment(document, identity, fields, verdicts):
    if (not isinstance(document, dict) or any(document.get(k) != v for k, v in identity.items())
            or document.get('verdict') not in verdicts
            or any(not isinstance(document.get(k), str) or not document[k].strip()
                   or len(document[k].encode()) > 32768 for k in (*fields, 'report'))):
        raise ValueError('Assessment is missing, stale or incomplete for the actual scope, baseline and owner')


def assess(api, config, store, number, args):
    from . import execution
    state, sha, section = read(api, config, store)
    actor, session = execution.credentials(args, 'Worker')
    identity = readiness_identity(api, config, number, actor, session)
    if not args.evidence_file:
        return {'identity': identity, 'required_assessments': ['code', 'decisions', 'related_work', 'dependencies', 'report'],
                'verdicts': ['ready', 'needs-clarification', 'superseded'], 'applied': False}
    document = json.loads(args.evidence_file.read_text(encoding='utf-8'))
    assessment(document, identity, ('code', 'decisions', 'related_work', 'dependencies'),
               ('ready', 'needs-clarification', 'superseded'))
    # An assessment grants no branch/edit ownership. A prospective replacement
    # must be able to assess before the stopped-owner takeover, in its own slot.
    if readiness_identity(api, config, number, actor, session) != identity:
        raise ValueError('Task context changed during assessment')
    if args.apply:
        context = state['task_context']
        context['readiness'].setdefault(str(number), {})[session] = copy.deepcopy(document)
        context['history'].append({'event': 'assessed', 'evidence': copy.deepcopy(document)})
        store.write(state, sha)
    return {'applied': args.apply, 'issue': number, 'verdict': document['verdict']}


def require_ready(api, config, state, number, fields):
    if not policy(config):
        return
    identity = readiness_identity(api, config, number, fields['actor'], fields['session'])
    document = state.get('task_context', {}).get('readiness', {}).get(str(number), {}).get(fields['session'])
    assessment(document, identity, ('code', 'decisions', 'related_work', 'dependencies'), ('ready',))


def approval_digest(approval):
    return digest({k: v for k, v in approval.items() if k != 'compatibility'})


def compatible(request, config, approval, main_sha):
    """Validate retained current compatibility; original review is never rewritten."""
    if not policy(config):
        return False
    root = 'repos/' + config['project']['repository']
    previous, previous_digest = approval['main_sha'], None
    for row in approval.get('compatibility', []):
        identity = dict(kind='source-compatibility', repository=config['project']['repository'],
                        issue=approval['issue'], pr=approval['pr'], head_sha=approval['head_sha'],
                        tree_sha=rules.tree(request, root, approval['head_sha']), scope=approval['scope'],
                        policy=rules.fingerprint(config), task_context_policy=policy(config),
                        approval_sha256=approval_digest(approval), original_main_sha=approval['main_sha'],
                        previous_main_sha=previous, previous_receipt_sha256=previous_digest)
        assessment(row, identity, ('main_changes', 'semantic_dependencies', 'checks'), ('compatible',))
        if (not rules.SHA.fullmatch(row.get('main_sha', '')) or row['main_sha'] == previous
                or request(root + '/compare/' + previous + '...' + row['main_sha']).get('status') != 'ahead'
                or row.get('reviewer') == approval['review'].get('implementer')
                or not re.fullmatch(r'Reviewer-[A-Za-z0-9]{4,32}', row.get('reviewer', ''))
                or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}', row.get('review_session', ''))
                or not re.fullmatch(r'[a-f0-9]{32}', row.get('execution_lane', ''))):
            raise ValueError('Invalid independent forward-main compatibility proof')
        previous, previous_digest = row['main_sha'], digest(row)
    return bool(previous_digest and previous == main_sha)


def source_context(api, config, section, number):
    from . import batch_policy
    approval = section['approvals'].get(str(number))
    if not approval or section['tasks'].get(str(number), {}).get('state') != 'approved':
        raise ValueError('Compatibility requires an unchanged Approved source')
    batch = section.get('integration')
    if batch and number in batch['issues']:
        raise ValueError('Frozen batch members require the normal held-batch recovery')
    # Original evidence remains valid against its original immutable baseline.
    pr = batch_policy.source(api.request, config, approval, approval['main_sha'], frozen=True)
    current = baseline(api, config)
    previous = (approval.get('compatibility') or [{}])[-1].get('main_sha', approval['main_sha'])
    if current == previous or api.request(api.root + '/compare/' + previous + '...' + current).get('status') != 'ahead':
        raise ValueError('Compatibility requires a forward main change; divergence needs author recovery')
    if rules.tree(api.request, api.root, pr['base']['sha']) != rules.tree(api.request, api.root, current):
        raise ValueError('Source target base must reflect current main')
    return approval, current, previous


def review_acquire(api, config, store, number, args):
    from . import execution
    from .sync import now
    state, sha, section = read(api, config, store)
    actor, session = execution.credentials(args, 'Reviewer')
    lane = rules.review_lane(section, issue=number)
    if lane:
        execution.owned(lane, args, 'Reviewer')
        if lane.get('route') != 'compatibility':
            raise ValueError('Another review operation owns this task')
        return {'review': lane, 'resumed': True}
    rules.admission_open(state)
    if section['policy']['schema_version'] == 3:
        if len(section['source_reviews']) >= section['policy']['review_limit']:
            raise ValueError('All source review slots are occupied')
    elif section.get('review'):
        raise ValueError('The review lane is occupied')
    execution.independent_slot(section, actor, session)
    approval, current, previous = source_context(api, config, section, number)
    if any(w['actor'] == actor or w['session'] == session for w in section['tasks'][str(number)]['implementers']):
        raise ValueError('Compatibility Reviewer must not have implemented this task')
    execution.register(section, actor, session, 'Reviewer')
    lane = dict(id=uuid.uuid4().hex, actor=actor, session=session,
                coordinator=execution.actor(args.coordinator, 'Coordinator'), pr=approval['pr'], issues=[number],
                state='reviewing', route='compatibility', main_sha=current, previous_main_sha=previous,
                approval_sha256=approval_digest(approval), started_at=now())
    execution.put_review(section, lane)
    execution.audit(section, 'compatibility-acquired', issue=number, lane=lane['id'])
    store.write(state, sha)
    return {'review': lane, 'instruction': 'Assess semantic compatibility with the main delta. No author edits or source CI on this new baseline are implied.'}


def compatibility_identity(api, config, approval, lane):
    previous = (approval.get('compatibility') or [None])[-1]
    return dict(kind='source-compatibility', repository=config['project']['repository'], issue=approval['issue'],
                pr=approval['pr'], head_sha=approval['head_sha'], tree_sha=rules.tree(api.request, api.root, approval['head_sha']),
                scope=approval['scope'], policy=rules.fingerprint(config), task_context_policy=policy(config),
                approval_sha256=approval_digest(approval), original_main_sha=approval['main_sha'],
                previous_main_sha=lane['previous_main_sha'], previous_receipt_sha256=digest(previous) if previous else None,
                main_sha=lane['main_sha'], reviewer=lane['actor'], review_session=lane['session'], execution_lane=lane['id'])


def review_record(api, config, store, number, args):
    from . import execution
    state, sha, section = read(api, config, store)
    lane = rules.review_lane(section, issue=number)
    document = json.loads(args.evidence_file.read_text(encoding='utf-8')) if args.evidence_file else None
    approval = section['approvals'].get(str(number))
    if not lane:
        actor, session = execution.credentials(args, 'Reviewer')
        rows = (approval or {}).get('compatibility', [])
        if (rows and rows[-1] == document and document.get('execution_lane') == args.claim
                and (document.get('reviewer'), document.get('review_session')) == (actor, session)):
            if not compatible(api.request, config, approval, baseline(api, config)):
                raise ValueError('Compatibility changed after the original write')
            source_context_after_write(api, config, approval)
            return {'already_applied': True, 'applied': False}
        raise ValueError('No owned compatibility review')
    execution.owned(lane, args, 'Reviewer')
    if lane.get('route') != 'compatibility':
        raise ValueError('This is not a compatibility review')
    approval, current, previous = source_context(api, config, section, number)
    if (lane['main_sha'] != current or lane['previous_main_sha'] != previous
            or lane['approval_sha256'] != approval_digest(approval)):
        raise ValueError('Compatibility baseline changed; release and review the new context')
    identity = compatibility_identity(api, config, approval, lane)
    if document is None:
        return {'identity': identity, 'applied': False,
                'required_assessments': ['main_changes', 'semantic_dependencies', 'checks', 'report'], 'verdict': 'compatible'}
    assessment(document, identity, ('main_changes', 'semantic_dependencies', 'checks'), ('compatible',))
    updated = copy.deepcopy(approval)
    updated.setdefault('compatibility', []).append(copy.deepcopy(document))
    if not compatible(api.request, config, updated, current):
        raise ValueError('Compatibility does not cover current main')
    # Refresh live Issue/PR/main immediately before the CAS; a changed state SHA
    # also rejects concurrent takeovers, returns and batch selection.
    again, head, prior = source_context(api, config, section, number)
    if (head, prior, approval_digest(again)) != (current, previous, approval_digest(approval)):
        raise ValueError('Source changed during compatibility assessment')
    if args.apply:
        section['approvals'][str(number)] = updated
        execution.audit(section, 'compatibility-recorded', issue=number, evidence=copy.deepcopy(document))
        execution.remove_review(section, lane)
        store.write(state, sha)
    return {'applied': args.apply, 'issue': number, 'state': 'approved'}


def source_context_after_write(api, config, approval):
    from . import batch_policy
    batch_policy.source(api.request, config, approval, baseline(api, config))


def review_release(api, config, store, number, args):
    from . import execution
    state, sha, section = read(api, config, store)
    lane = execution.owned(rules.review_lane(section, issue=number), args, 'Reviewer')
    if lane.get('route') != 'compatibility':
        raise ValueError('Use the normal source review return command')
    text = execution.report(args)
    execution.audit(section, 'compatibility-released', issue=number, review=copy.deepcopy(lane), report=text)
    execution.remove_review(section, lane)
    store.write(state, sha)
    return {'issue': number, 'state': 'approved', 'instruction': 'No new compatibility granted; use normal author return for necessary changes.'}
