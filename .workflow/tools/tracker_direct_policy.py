"""Canonical portable structural contract for explicitly selected modern routes.

Only stdlib, no engine, workspace, API or consumer imports. A selected policy is
not adoption, a digest is not semantic approval, and a public subject is not an
ownership credential. Master uses this exact fixed snapshot for structure only.
"""
import datetime
import hashlib
import json
import re

HEX = re.compile(r'[0-9a-f]{64}\Z')
GIT = re.compile(r'[0-9a-f]{40}\Z')
OP = re.compile(r'[0-9a-f]{32}\Z')
REPO = re.compile(r'[\w.-]+/[\w.-]+\Z')
STATES = ('preparing', 'implementing', 'queued', 'reviewing', 'fixing', 'stage',
          'main-merged', 'finalizing', 'completed', 'stopped', 'cancelled-before-merge')
EVENTS = ('begun', 'source-bound', 'handoff-pending', 'stop-pending', 'handed-off',
          'review-bound', 'returned', 'taken-over', 'staged', 'main-observed',
          'receipt-recorded', 'completed', 'stopped', 'cancelled')


def digest(value):
    # Same byte convention as task_context.digest; file hashes stay raw bytes.
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def integer(value, lower=1, upper=9007199254740991):
    return type(value) is int and lower <= value <= upper


def text(value, limit=1024):
    return isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= limit


def hashed(value):
    return isinstance(value, str) and bool(HEX.fullmatch(value))


def git_sha(value):
    return isinstance(value, str) and bool(GIT.fullmatch(value))


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|\+00:00)', value):
        return False
    try:
        datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False


def repository(value):
    return isinstance(value, str) and bool(REPO.fullmatch(value))


def branch(value):
    return (isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', value))
            and not any(x in value for x in ('..', '//', '@{'))
            and not value.endswith(('/', '.', '.lock')))


def execution_policy(config):
    """Validate modern selection only. Older selections keep their old parser."""
    if 'execution_policy' not in config:
        return None
    value = config['execution_policy']
    fields = {'schema_version', 'route', 'implementation_limit', 'review_limit'}
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 4 or value.get('route') not in ('direct', 'batch')
            or set(value) != fields | ({'batch_limit'} if value.get('route') == 'batch' else set())
            or not integer(value.get('implementation_limit'), 1, 16)
            or not integer(value.get('review_limit'), 1, 5)
            or value['route'] == 'batch' and not integer(value.get('batch_limit'), 1, 5)):
        raise ValueError('Modern execution needs schema4, route direct|batch, implementation_limit1..16, review_limit1..5; only batch requires batch_limit1..5')
    return value


def context_policy(config):
    if 'task_context_policy' not in config:
        return None
    value = config['task_context_policy']
    if (not isinstance(value, dict) or set(value) != {'schema_version'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 2):
        raise ValueError('Modern task_context_policy must be exactly schema_version2')
    if not execution_policy(config):
        raise ValueError('Task context2 requires explicit execution4')
    completion = config.get('completion_policy')
    if (not isinstance(completion, dict) or set(completion) != {'schema_version'}
            or type(completion.get('schema_version')) is not int or completion['schema_version'] != 1):
        raise ValueError('Modern task context requires explicit completion_policy1')
    return value


def capture_policy(config):
    if 'knowledge_capture_policy' not in config:
        return None
    value = config['knowledge_capture_policy']
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'roots', 'scope'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or not text(value.get('scope'), 4096) or not isinstance(value.get('roots'), list)
            or not 1 <= len(value['roots']) <= 16 or any(not isinstance(p, str) for p in value['roots'])
            or len(set(value['roots'])) != len(value['roots'])):
        raise ValueError('Capture policy needs schema1, unique safe directory roots1..16 and explicit bounded approved scope')
    reserved = ('tracker', 'scripts', 'config', 'docs/repository', 'docs/runtime', 'skills', 'rules', 'tools')
    for path in value['roots']:
        if (not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_/-]*/', path)
                or any(x in ('', '.', '..') for x in path[:-1].split('/'))
                or any(path[:-1].lower().split('/')[:len(x.split('/'))] == x.split('/')
                       or x.split('/')[:len(path[:-1].split('/'))] == path[:-1].lower().split('/') for x in reserved)):
            raise ValueError('Capture roots must be normalized ordinary-note directories, excluding governance/runtime/configuration')
    if not execution_policy(config) or not context_policy(config):
        raise ValueError('Capture requires the explicit modern guarded bundle')
    delivery = config.get('delivery_policy')
    if not isinstance(delivery, dict) or delivery.get('completion') != 'reviewed-merge':
        raise ValueError('Capture v1 supports reviewed-merge only; package/production changes need a tracked task')
    return value


def bundle(config):
    value = execution_policy(config)
    if value is None:
        if 'knowledge_capture_policy' in config:
            raise ValueError('Capture requires explicit modern execution')
        return None
    context = context_policy(config)
    if not context:
        raise ValueError('Execution4 requires explicit task_context_policy2 and completion_policy1')
    if not isinstance(config.get('delivery_policy'), dict):
        raise ValueError('Modern routes require an explicit independent-review delivery policy')
    return {'execution_policy': value, 'task_context_policy': context,
            'completion_policy': config['completion_policy'],
            'knowledge_capture_policy': capture_policy(config)}


def subject(value):
    if (not isinstance(value, dict) or set(value) != {'kind', 'id'}
            or value.get('kind') not in ('task', 'capture')
            or value['kind'] == 'task' and not integer(value['id'])
            or value['kind'] == 'capture' and not (isinstance(value['id'], str) and OP.fullmatch(value['id']))):
        raise ValueError('Use one exact typed task/capture subject; aliases and claim IDs are not subjects')
    return value


def subject_key(value):
    value = subject(value)
    return value['kind'] + ':' + str(value['id'])


def parse_key(value):
    if not isinstance(value, str):
        raise ValueError('Invalid subject key')
    if re.fullmatch(r'task:[1-9][0-9]*', value):
        result = {'kind': 'task', 'id': int(value[5:])}
    elif re.fullmatch(r'capture:[0-9a-f]{32}', value):
        result = {'kind': 'capture', 'id': value[8:]}
    else:
        raise ValueError('Invalid canonical subject key')
    return subject(result)


def task_number(value):
    value = subject(value)
    if value['kind'] != 'task':
        raise ValueError('This command is task-only; capture has no Issue')
    return value['id']


def record(section, value):
    value = subject(value)
    collection = section['tasks'] if value['kind'] == 'task' else section['captures']['operations']
    row = collection.get(str(value['id']))
    if not isinstance(row, dict) or value['kind'] == 'capture' and row.get('id') != value['id']:
        raise ValueError('Subject has no matching durable operation/task record')
    return row


def subjects_for_lane(section, lane):
    if section['policy']['schema_version'] == 4 and not lane.get('batch_id'):
        if 'issues' in lane or 'issue' in lane:
            raise ValueError('Modern singleton has one subject, no parallel Issue alias')
        return [subject(lane.get('subject'))]
    numbers = lane.get('issues')
    if not isinstance(numbers, list) or not numbers or any(not integer(n) for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError('Batch/older source needs its nonempty actual Issue membership')
    return [{'kind': 'task', 'id': number} for number in numbers]


def source(value, *, ready=False):
    if (not isinstance(value, dict) or set(value) != {'branch', 'pr', 'base_sha', 'head_sha', 'tree_sha'}
            or not branch(value.get('branch')) or not git_sha(value.get('base_sha'))
            or value['pr'] is not None and not integer(value['pr'])
            or any(value[k] is not None and not git_sha(value[k]) for k in ('head_sha', 'tree_sha'))
            or ready and (not integer(value['pr']) or not git_sha(value['head_sha']) or not git_sha(value['tree_sha']))):
        raise ValueError('Invalid exact source binding')
    return value


def claim_digest(repo, value, owner, token):
    subject(value)
    if not repository(repo) or not isinstance(token, str) or not OP.fullmatch(token) or token == value['id']:
        raise ValueError('Supply the real distinct Worker claim token')
    fields = {k: owner[k] for k in ('actor', 'session', 'coordinator', 'branch', 'worktree_sha256')}
    return digest(dict(fields, repository=repo, subject=value, claim=token))


def handoff(value, repo, selected_subject):
    fields = {'schema_version', 'repository', 'subject', 'kind', 'actor', 'session', 'coordinator',
              'claim_sha256', 'worktree_sha256', 'source', 'scope_sha256', 'report_sha256', 'at'}
    if (not isinstance(value, dict) or set(value) != fields or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or value.get('repository') != repo
            or subject(value.get('subject')) != subject(selected_subject)
            or value.get('kind') not in ('handoff', 'stop')
            or not isinstance(value.get('actor'), str) or not re.fullmatch(r'Worker-[A-Za-z0-9]{4,32}', value['actor'])
            or not isinstance(value.get('session'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}', value['session'])
            or not isinstance(value.get('coordinator'), str) or not re.fullmatch(r'Coordinator-[A-Za-z0-9]{4,32}', value['coordinator'])
            or any(not hashed(value.get(k)) for k in ('claim_sha256', 'worktree_sha256', 'report_sha256'))
            or value['scope_sha256'] is not None and not hashed(value['scope_sha256'])
            or value['kind'] == 'handoff' and not hashed(value['scope_sha256'])
            or not timestamp(value.get('at'))):
        raise ValueError('Invalid retained non-authorizing source handoff')
    source(value['source'], ready=value['kind'] == 'handoff')
    return value


def scope(value, repo, selected_subject):
    fields = {'schema_version', 'repository', 'subject', 'base_sha', 'base_tree_sha',
              'head_sha', 'head_tree_sha', 'capture_policy_sha256', 'files_sha256'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('repository') != repo or subject(value.get('subject')) != selected_subject
            or any(not git_sha(value.get(k)) for k in ('base_sha', 'base_tree_sha', 'head_sha', 'head_tree_sha'))
            or any(not hashed(value.get(k)) for k in ('capture_policy_sha256', 'files_sha256'))):
        raise ValueError('Invalid complete capture scope')
    return value


def assessment(value, repo, selected_subject):
    fields = {'schema_version', 'kind', 'repository', 'subject', 'scope_sha256',
              'capture_policy_sha256', 'verdict', 'actor', 'session', 'report_sha256', 'at'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('kind') != 'capture-scope-assessment' or value.get('repository') != repo
            or subject(value.get('subject')) != selected_subject
            or value.get('verdict') not in ('within-mandate', 'needs-engineering', 'unknown')
            or not isinstance(value.get('actor'), str) or not re.fullmatch(r'Worker-[A-Za-z0-9]{4,32}', value['actor'])
            or not isinstance(value.get('session'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}', value['session'])
            or any(not hashed(value.get(k)) for k in ('scope_sha256', 'capture_policy_sha256', 'report_sha256'))
            or not timestamp(value.get('at'))):
        raise ValueError('Invalid operator capture scope assessment')
    return value


def pr_intent(value):
    if (not isinstance(value, dict) or set(value) != {'purpose', 'state', 'request_sha256', 'owner_claim_sha256', 'pr'}
            or value.get('purpose') not in ('source', 'promotion')
            or value.get('state') not in ('not-started', 'dispatching', 'unknown', 'observed')
            or any(value[k] is not None and not hashed(value[k]) for k in ('request_sha256', 'owner_claim_sha256'))
            or value['pr'] is not None and not integer(value['pr'])
            or value['state'] == 'not-started' and any(value[k] is not None for k in ('request_sha256', 'owner_claim_sha256', 'pr'))
            or value['state'] != 'not-started' and any(not hashed(value[k]) for k in ('request_sha256', 'owner_claim_sha256'))
            or value['state'] == 'observed' and not integer(value['pr'])
            or value['state'] in ('dispatching', 'unknown') and value['pr'] is not None):
        raise ValueError('Invalid durable capture PR intent')
    return value


def capture_receipt(value, repo, selected_subject):
    fields = {'schema_version', 'kind', 'repository', 'subject', 'source_pr', 'source_head_sha',
              'source_tree_sha', 'main_pr', 'merge_sha', 'tree_sha', 'scope_sha256',
              'capture_policy_sha256', 'route_policy_sha256', 'review_sha256',
              'compatibility_sha256', 'stage_sha256', 'recorded_at', 'result'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('kind') != 'capture-main-acceptance' or value.get('result') != 'accepted'
            or value.get('repository') != repo or subject(value.get('subject')) != selected_subject
            or not integer(value.get('source_pr')) or not integer(value.get('main_pr'))
            or any(not git_sha(value.get(k)) for k in ('source_head_sha', 'source_tree_sha', 'merge_sha', 'tree_sha'))
            or any(not hashed(value.get(k)) for k in ('scope_sha256', 'capture_policy_sha256', 'route_policy_sha256', 'review_sha256'))
            or any(value[k] is not None and not hashed(value[k]) for k in ('compatibility_sha256', 'stage_sha256'))
            or not timestamp(value.get('recorded_at'))):
        raise ValueError('Invalid canonical capture acceptance receipt')
    return value


def capture_record(value, repo, operation_id):
    fields = {'schema_version', 'id', 'repository', 'state', 'created_at', 'policy', 'source',
              'pr_intent', 'scope', 'assessment', 'implementers', 'review_sha256', 'handoff', 'receipt', 'history'}
    selected = subject({'kind': 'capture', 'id': operation_id})
    if (not isinstance(value, dict) or set(value) != fields
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('id') != operation_id or value.get('repository') != repo
            or value.get('state') not in STATES or not timestamp(value.get('created_at'))
            or not isinstance(value.get('policy'), dict) or set(value['policy']) != {'main_sha', 'capture_sha256', 'route_sha256'}
            or not git_sha(value['policy'].get('main_sha'))
            or any(not hashed(value['policy'].get(k)) for k in ('capture_sha256', 'route_sha256'))
            or not isinstance(value.get('implementers'), list) or not value['implementers']
            or not isinstance(value.get('history'), list) or not value['history']
            or value['review_sha256'] is not None and not hashed(value['review_sha256'])):
        raise ValueError('Invalid durable capture operation')
    for owner in value['implementers']:
        if (not isinstance(owner, dict) or set(owner) != {'actor', 'session'}
                or not isinstance(owner['actor'], str) or not re.fullmatch(r'Worker-[A-Za-z0-9]{4,32}', owner['actor'])
                or not isinstance(owner['session'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}', owner['session'])):
            raise ValueError('Invalid retained actual capture implementer')
    if len({(r['actor'],r['session']) for r in value['implementers']})!=len(value['implementers']):
        raise ValueError('Capture implementers are the retained distinct actual runs')
    source(value['source'])
    pr_intent(value['pr_intent'])
    if value['scope'] is not None:
        scope(value['scope'], repo, selected)
    if value['assessment'] is not None:
        assessment(value['assessment'], repo, selected)
        if value['scope'] is None or value['assessment']['scope_sha256'] != digest(value['scope']):
            raise ValueError('Capture assessment must bind its actual complete scope')
    if value['handoff'] is not None:
        handoff(value['handoff'], repo, selected)
    if value['receipt'] is not None:
        capture_receipt(value['receipt'], repo, selected)
    for event in value['history']:
        if (not isinstance(event, dict) or set(event) != {'event', 'at', 'actor', 'session', 'bindings'}
                or event['event'] not in EVENTS or not timestamp(event['at'])
                or not text(event['actor'], 100) or not text(event['session'], 100)
                or not isinstance(event['bindings'], dict)
                or set(event['bindings']) != {'source', 'pr_intent', 'scope', 'assessment', 'review_sha256', 'handoff', 'receipt_sha256'}):
            raise ValueError('Invalid capture transition history')
        binding = event['bindings']
        source(binding['source'])
        pr_intent(binding['pr_intent'])
        if binding['scope'] is not None:
            scope(binding['scope'], repo, selected)
        if binding['assessment'] is not None:
            assessment(binding['assessment'], repo, selected)
        if binding['handoff'] is not None:
            handoff(binding['handoff'], repo, selected)
        if any(binding[k] is not None and not hashed(binding[k]) for k in ('review_sha256', 'receipt_sha256')):
            raise ValueError('Invalid capture historical proof digest')
    if value['state'] == 'completed' and (not value['receipt'] or value['pr_intent']['state']!='observed'):
        raise ValueError('Completed capture must retain its exact receipt and positively reconciled native PR intent')
    if value['receipt']:
        receipt=value['receipt']
        if (value['scope'] is None or receipt['scope_sha256']!=digest(value['scope'])
                or receipt['source_pr']!=value['source']['pr'] or receipt['source_head_sha']!=value['source']['head_sha']
                or receipt['source_tree_sha']!=value['source']['tree_sha']
                or receipt['capture_policy_sha256']!=value['policy']['capture_sha256']
                or receipt['route_policy_sha256']!=value['policy']['route_sha256']):
            raise ValueError('Capture receipt must retain its exact original source/scope/mandate')
    return value


def validate_execution(section, repo):
    value = execution_policy({'execution_policy': section.get('policy')})
    for key in ('claims', 'tasks', 'actors', 'completed', 'source_reviews', 'approvals', 'batches'):
        if not isinstance(section.get(key), dict):
            raise ValueError('Invalid modern execution collection: ' + key)
    if (not isinstance(section.get('review_queue'), list) or not isinstance(section.get('history'), list)
            or len(section['claims']) > value['implementation_limit']
            or len(section['source_reviews']) > value['review_limit']):
        raise ValueError('Invalid modern execution queue/capacity')
    queued = [subject_key(item) for item in section['review_queue']]
    if len(queued) != len(set(queued)):
        raise ValueError('Duplicate queued modern subject')
    work_identities={k:set() for k in ('id','actor','session')}
    for key, owner in section['claims'].items():
        if not isinstance(owner, dict):
            raise ValueError('Invalid modern Worker record')
        selected = subject(owner.get('subject'))
        if (key != subject_key(selected) or 'issue' in owner or 'issues' in owner or 'worktree' in owner
                or not isinstance(owner.get('id'), str) or not OP.fullmatch(owner['id']) or owner['id'] == selected['id']
                or not hashed(owner.get('worktree_sha256')) or not branch(owner.get('branch'))):
            raise ValueError('Invalid modern Worker subject/claim/worktree binding')
        validate_owner(owner, 'Worker', section)
        if owner.get('state') not in ('implementing', 'handoff', 'stopping') or not timestamp(owner.get('started_at')):
            raise ValueError('Invalid actual modern Worker transition state')
        if owner['state'] in ('handoff', 'stopping') and not hashed(owner.get('report_sha256')):
            raise ValueError('Pending source transition requires its exact report digest')
        row=record(section, selected)
        for field,identities in work_identities.items():
            if owner[field] in identities:raise ValueError('Each retained Worker needs one actual distinct claim/run')
            identities.add(owner[field])
        if row.get('state')!='implementing':raise ValueError('Actual current Worker must bind its implementing source record')
        if owner['state'] in ('handoff','stopping'):
            retained=row.get('handoff');handoff(retained,repo,selected)
            if (retained['claim_sha256']!=claim_digest(repo,selected,owner,owner['id'])
                    or retained['report_sha256']!=owner['report_sha256']
                    or retained['kind']!=('handoff' if owner['state']=='handoff' else 'stop')):
                raise ValueError('Pending Worker needs the exact installed non-authorizing handoff binding')
    seen = {key: set() for key in ('id', 'actor', 'session', 'pr')}
    subjects = set()
    for key, lane in section['source_reviews'].items():
        if not isinstance(lane, dict):
            raise ValueError('Invalid modern source review record')
        selected = subjects_for_lane(section, lane)[0]
        if key != str(lane.get('pr')) or lane.get('batch_id') or subject_key(selected) in subjects:
            raise ValueError('Invalid modern source review identity')
        subjects.add(subject_key(selected))
        record(section, selected)
    lanes = list(section['source_reviews'].values())
    if section.get('review'):
        if not isinstance(section['review'], dict) or not section['review'].get('batch_id'):
            raise ValueError('Modern integration Reviewer is only a real batch subrole')
        lanes.append(section['review'])
    for lane in lanes:
        validate_owner(lane, 'Reviewer', section)
        if (lane.get('state') not in ('reviewing', 'returning', 'fixing', 'approving')
                or not integer(lane.get('pr')) or not timestamp(lane.get('started_at'))):
            raise ValueError('Invalid actual modern Reviewer transition')
        for field in seen:
            item = lane.get(field)
            if item is None or not isinstance(item, (str, int)) or type(item) is bool or item in seen[field]:
                raise ValueError('Each current review needs a distinct actual identity and run')
            seen[field].add(item)
        if lane['id'] in work_identities['id'] or lane['actor'] in work_identities['actor'] or lane['session'] in work_identities['session']:
            raise ValueError('Actual implementation and independent review runs must remain distinct')
        if not lane.get('batch_id'):
            selected=subject(lane['subject']);row=record(section,selected)
            if lane['id']==selected['id']:raise ValueError('Public operation identity is never a Reviewer claim')
            retained=row.get('handoff');handoff(retained,repo,selected)
            if retained['source']['pr']!=lane['pr'] or retained['kind']!='handoff':
                pending=section['claims'].get(subject_key(selected),{})
                prior=([e['bindings']['handoff'] for e in row.get('history',[])] if selected['kind']=='capture'
                       else [e.get('handoff') for e in section['history'] if e.get('subject')==selected])
                matches=[b for b in prior if isinstance(b,dict) and b.get('schema_version')==1 and b.get('source',{}).get('pr')==lane['pr'] and b.get('kind')=='handoff']
                if lane['state']!='fixing' or pending.get('state')!='handoff' or not matches:
                    raise ValueError('Source Reviewer must bind the actual retained Worker handoff or its exact pending owned fix')
                for previous in matches:handoff(previous,repo,selected)
            if any(w.get('actor')==lane['actor'] or w.get('session')==lane['session'] for w in row.get('implementers',[])):
                raise ValueError('Retained implementer cannot independently review this source')
            if subject_key(selected) in section['claims'] and lane['state']!='fixing':
                raise ValueError('Concurrent author fix requires the retained Reviewer in fixing')
    for selected in section['review_queue']:
        row=record(section,selected)
        if row.get('state')!='queued' or subject_key(selected) in section['claims'] or subject_key(selected) in subjects:
            raise ValueError('Queued subject must retain its actual completed source handoff only')
        retained=row.get('handoff');handoff(retained,repo,selected)
        if retained['kind']!='handoff':raise ValueError('Stopped work cannot fabricate a source review queue entry')
    for operation_id, operation in section['captures']['operations'].items():
        capture_record(operation, repo, operation_id)
        selected={'kind':'capture','id':operation_id};key=subject_key(selected)
        if operation['state'] in ('preparing','implementing') and key not in section['claims']:
            raise ValueError('Capture cannot orphan its current actual implementation owner')
        if operation['state']=='queued' and selected not in section['review_queue']:
            raise ValueError('Capture queue state needs its actual retained queue reservation')
        if operation['state'] in ('reviewing','stage','main-merged','finalizing') and key not in subjects:
            raise ValueError('Capture cannot orphan its genuine held source Reviewer')
        if operation['state'] in ('completed','cancelled-before-merge'):
            if key in section['claims'] or key in subjects or selected in section['review_queue']:
                raise ValueError('A terminal capture cannot erase or disguise current held ownership')
            if operation['state']=='completed':
                archive=section['completed'].get(str(operation['receipt']['main_pr']),{})
                if (archive.get('subject')!=selected or archive.get('finalization',{}).get('phase')!='released'
                        or archive['finalization'].get('receipt_sha256')!=digest(operation['receipt'])):
                    raise ValueError('Completed capture needs its exact atomic released archive/receipt')
            elif not any(e.get('event')=='capture-cancelled' and e.get('subject')==selected and e.get('source')==operation['source']
                         and e.get('native_state')==('closed-unmerged' if operation['source']['pr'] else 'not-dispatched')
                         and e.get('request_sha256')==operation['pr_intent']['request_sha256'] and hashed(e.get('report_sha256'))
                         for e in section['history']):
                raise ValueError('Capture cancellation needs its retained verified native/no-dispatch audit')
    for key, task in section['tasks'].items():
        if not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]*', key) or not isinstance(task, dict):
            raise ValueError('Invalid real task map')
        retained = task.get('handoff')
        if isinstance(retained, dict) and 'schema_version' in retained:
            handoff(retained, repo, {'kind': 'task', 'id': int(key)})
    for pointer in ('integration', 'intake', 'review'):
        if section.get(pointer) is not None and not isinstance(section[pointer], dict):
            raise ValueError('Invalid actual modern ownership pointer')
    integration = section.get('integration')
    if integration is not None:
        validate_owner(integration, 'Reviewer' if integration.get('kind') == 'direct' else 'Coordinator', section)
        if integration.get('kind') == 'direct':
            selected = subject(integration.get('subject'))
            record(section, selected)
            if ('issues' in integration or 'issue' in integration
                    or not integer(integration.get('source_pr'))
                    or integration.get('promotion_pr') is not None and not integer(integration['promotion_pr'])
                    or any(not git_sha(integration.get(k)) for k in ('source_head_sha', 'source_tree_sha', 'main_sha'))
                    or any(not hashed(integration.get(k)) for k in ('scope_sha256', 'review_sha256', 'route_policy_sha256', 'review_claim_sha256'))
                    or type(integration.get('entered_stage')) is not bool
                    or integration.get('state') not in ('reserved', 'stage', 'fixing', 'main-merged', 'delivery-pending', 'finalizing', 'completed', 'cancelled-before-merge')
                    or not isinstance(integration.get('history'), list) or not integration['history']):
                raise ValueError('Invalid retained direct reservation')
            matching = section['source_reviews'].get(str(integration['source_pr']))
            if (not matching or matching['subject'] != selected or integration['id']==selected['id']
                    or (integration['actor'],integration['session'],integration['review_claim_sha256'])!=(matching['actor'],matching['session'],digest(matching['id']))):
                raise ValueError('Direct reservation must retain its genuine source Reviewer')
            finalization(integration.get('finalization'),selected,integration['source_pr'])
            if integration.get('delivery_request') is not None:
                direct_authorization(integration['delivery_request'],integration)
        elif integration.get('kind') == 'batch':
            numbers = integration.get('issues')
            if (value['route'] != 'batch' or not isinstance(numbers, list) or not numbers
                    or any(not integer(n) for n in numbers) or len(set(numbers)) != len(numbers)
                    or len(numbers) > value['batch_limit'] or not isinstance(integration.get('members'), list)
                    or [m.get('issue') for m in integration['members']] != numbers
                    or not git_sha(integration.get('main_sha'))):
                raise ValueError('Invalid actual modern batch membership')
        else:
            raise ValueError('Integration must discriminate actual direct or batch ownership')
    return section


def finalization(value,selected,source_pr):
    if value is None:return
    if (not isinstance(value,dict) or set(value)!={'schema_version','subject','source_pr','main_pr','merge_sha','receipt_sha256','status_state','phase'}
            or type(value.get('schema_version')) is not int or value['schema_version']!=1 or value.get('subject')!=subject(selected)
            or value.get('source_pr')!=source_pr or not integer(value.get('source_pr')) or not integer(value.get('main_pr'))
            or not git_sha(value.get('merge_sha')) or value.get('phase') not in ('main-observed','receipt-recorded','status-reconciled','released')
            or value['receipt_sha256'] is not None and not hashed(value['receipt_sha256'])
            or value['phase']!='main-observed' and not hashed(value['receipt_sha256'])
            or selected['kind']=='capture' and value.get('status_state')!='not-applicable'
            or selected['kind']=='task' and value.get('status_state') not in ('pending','reconciled')
            or selected['kind']=='task' and value['phase'] in ('status-reconciled','released') and value['status_state']!='reconciled'):
        raise ValueError('Invalid retained ordered source receipt/status/release journal')


def direct_authorization(value,reservation):
    fields={'schema_version','kind','state','subject','reservation_sha256','source_pr','source_head_sha','scope_sha256','policy',
            'route_policy_sha256','target','workflow','job','actor','session','requested_at'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value.get('schema_version')) is not int or value['schema_version']!=2
            or value.get('kind')!='direct' or value.get('state')!='requested' or value.get('subject')!=reservation['subject']
            or task_number(value['subject'])<1 or not integer(value.get('source_pr')) or value.get('reservation_sha256')!=digest(reservation['id'])
            or any(value.get(k)!=reservation[k] for k in ('source_pr','source_head_sha','scope_sha256','route_policy_sha256'))
            or not isinstance(value.get('actor'),str) or not re.fullmatch(r'Reviewer-[A-Za-z0-9]{4,32}',value['actor'])
            or not isinstance(value.get('session'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}',value['session'])
            or not hashed(value.get('policy')) or not text(value.get('target'),1024)
            or not isinstance(value.get('workflow'),str) or not re.fullmatch(r'\.github/workflows/[A-Za-z0-9][A-Za-z0-9_-]*\.ya?ml',value['workflow'])
            or not text(value.get('job'),200) or not timestamp(value.get('requested_at'))):
        raise ValueError('Invalid durable typed direct delivery authorization')


def validate_owner(owner, role, section):
    if (not isinstance(owner, dict) or not isinstance(owner.get('id'), str) or not OP.fullmatch(owner['id'])
            or not isinstance(owner.get('actor'), str) or not re.fullmatch(role + r'-[A-Za-z0-9]{4,32}', owner['actor'])
            or not isinstance(owner.get('session'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}', owner['session'])
            or role != 'Coordinator' and (not isinstance(owner.get('coordinator'), str) or not re.fullmatch(r'Coordinator-[A-Za-z0-9]{4,32}', owner['coordinator']))
            or section['actors'].get(owner['actor']) != {'role': role, 'session': owner['session']}):
        raise ValueError('Invalid actual registered modern ownership identity')


def protocol(shared):
    """Validate outer7 only and return its actual known underlying wrapper.

    Inner completion/context/retry validation remains with their canonical readers.
    No generic wrapper guessing, fallback, normalization or mutation occurs here.
    """
    outer = shared.get('schema_version')
    held = shared.get('maintenance')
    effective = held.get('base_schema_version') if type(outer) is int and outer == 3 and isinstance(held, dict) else outer
    contract = shared.get('execution_contract')
    section = shared.get('execution')
    selected = section.get('policy') if isinstance(section, dict) else None
    context = shared.get('task_context')
    context_selection = context.get('policy') if isinstance(context, dict) else None
    explicit = ('execution_contract' in shared or isinstance(section, dict) and
                (isinstance(selected, dict) and selected.get('schema_version') == 4 or 'captures' in section)
                or isinstance(context_selection, dict) and context_selection.get('schema_version') == 2)
    if type(effective) is not int or effective != 7:
        if explicit:
            raise ValueError('Modern execution/context/capture requires the outer7 reader/writer fence')
        return effective
    if shared.get('active') is not None or (shared.get('run') or {}).get('state')=='running':
        raise ValueError('Modern execution cannot coexist with retained legacy implementation/run ownership')
    if (not isinstance(contract, dict) or set(contract) != {'schema_version', 'base_schema_version', 'repository', 'history'}
            or type(contract.get('schema_version')) is not int or contract['schema_version'] != 1
            or type(contract.get('base_schema_version')) is not int or contract['base_schema_version'] not in (5, 6)
            or not repository(contract.get('repository')) or not isinstance(contract.get('history'), list)
            or not contract['history']):
        raise ValueError('Invalid modern execution contract; initial7 bases are5/6 only')
    for event in contract['history']:
        if (not isinstance(event, dict) or set(event) != {'event', 'at', 'main_sha', 'policy_sha256', 'report_sha256'}
                or event.get('event') not in ('adopted', 'policy-changed') or not timestamp(event.get('at'))
                or not git_sha(event.get('main_sha')) or not hashed(event.get('policy_sha256'))
                or not hashed(event.get('report_sha256'))):
            raise ValueError('Invalid retained modern policy transition')
    if contract['history'][0]['event'] != 'adopted' or sum(e['event'] == 'adopted' for e in contract['history']) != 1:
        raise ValueError('Modern adoption history must retain its first adoption')
    if (not isinstance(section, dict) or not isinstance(section.get('policy'), dict)
            or section['policy'].get('schema_version') != 4):
        raise ValueError('Outer7 requires explicit modern execution state')
    execution_policy({'execution_policy': section['policy']})
    capture = section.get('captures')
    if (not isinstance(capture, dict) or set(capture) != {'schema_version', 'policy', 'operations'}
            or type(capture.get('schema_version')) is not int or capture['schema_version'] != 1
            or not isinstance(capture.get('operations'), dict)):
        raise ValueError('Modern execution requires its exact capture container, including genuine absence')
    if context_selection != {'schema_version': 2} or type(context_selection.get('schema_version')) is not int:
        raise ValueError('Outer7 requires adopted task context2')
    if not isinstance(shared.get('completion'), dict) or not shared['completion']:
        raise ValueError('Outer7 requires adopted scoped completion1')
    if contract['base_schema_version'] == 5 and shared['completion'].get('base_schema_version') != 4:
        raise ValueError('Modern completion must retain actual task context')
    if contract['base_schema_version'] == 6 and (not isinstance(shared.get('ci_retry'), dict)
            or shared['ci_retry'].get('base_schema_version') != 5
            or shared['completion'].get('base_schema_version') != 4):
        raise ValueError('Modern retry must retain completion/context without rebasing its adoption anchor')
    if capture['policy'] is not None:
        capture_policy({'execution_policy': section['policy'], 'task_context_policy': context_selection,
                        'completion_policy': {'schema_version': 1}, 'knowledge_capture_policy': capture['policy'],
                        'delivery_policy': {'completion': 'reviewed-merge'}})
    validate_execution(section, contract['repository'])
    return contract['base_schema_version']


def state(shared, config):
    """Matching selection/adoption check, after canonical wrapper validation."""
    protocol(shared)
    selected = bundle(config)
    contract = shared.get('execution_contract')
    if selected is None:
        if contract:
            raise ValueError('Restore the explicitly adopted modern policy; no implicit downgrade')
        return None
    if (not contract or contract['repository'] != config['project']['repository']
            or contract['history'][-1]['policy_sha256'] != digest(selected)
            or shared['execution']['policy'] != selected['execution_policy']
            or shared['execution']['captures']['policy'] != selected['knowledge_capture_policy']):
        raise ValueError('Modern route bundle changed/not adopted; use owned idle execution migrate')
    return shared['execution']


def rules_module():
    try:
        from . import delivery_policy
    except ImportError:
        import tracker_delivery_policy as delivery_policy
    return delivery_policy


def process_module():
    try:
        from . import process_instructions
    except ImportError:
        import tracker_process_instructions as process_instructions
    return process_instructions


def linkage(body, config):
    """Source discriminator only; association and native identity follow in check."""
    captures = re.findall(r'^Tracker capture:\s*([^\n]*)', body or '', re.M | re.I)
    issues = re.findall(r'^Tracker issues:', body or '', re.M | re.I)
    if captures:
        if len(captures) != 1 or issues or not OP.fullmatch(captures[0].strip()):
            raise ValueError('Use exactly one capture linkage and no Issue line')
        return subject({'kind':'capture', 'id':captures[0].strip()})
    try:
        from . import pr_checks
    except ImportError:
        import tracker_pr_check as pr_checks
    numbers = pr_checks.linked_numbers(body, config)
    if len(numbers) != 1:
        raise ValueError('Modern sources use one actual task subject; real batch membership stays separate')
    return subject({'kind':'task', 'id':numbers[0]})


def capture_scope(request, config, selected, base_sha, head_sha):
    """Complete immutable tree/blob classifier; semantic mandate is attested."""
    value = capture_policy(config)
    if not value or subject(selected)['kind'] != 'capture':
        raise ValueError('Explicit capture mandate and operation are required')
    rules, objects = rules_module(), process_module()
    root = 'repos/' + config['project']['repository']
    base_tree, head_tree = rules.tree(request, root, base_sha), rules.tree(request, root, head_sha)
    before, after = objects.entries(request, root, base_tree), objects.entries(request, root, head_tree)
    paths = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    if not paths or len(paths) > 100:
        raise ValueError('Capture requires a complete bounded nonempty ordinary-note diff; use a tracked engineering task')
    manifest = []
    for path in paths:
        parts = path.lower().split('/')
        name = parts[-1]
        if (not objects.safe_path(path) or not any(path.startswith(p) for p in value['roots'])
                or not name.endswith(('.md','.txt'))
                or any(part.startswith('.') for part in parts)
                or name in ('agents.md','claude.md','skill.md','readme.md','readme.txt','index.md','index.txt')
                or any(part in ('scripts','automation','config','rules','skills','repository','runtime') for part in parts)
                or path not in after
                or any(row and (row['type'] != 'blob' or row['mode'] != '100644') for row in (before.get(path),after.get(path)))):
            raise ValueError('Governance, routing, mixed, deleted, renamed or non-regular changes require a tracked reviewed task')
        for row in (before.get(path),after[path]):
            if row:
                objects.blob(request,root,row['sha'])
        manifest.append({'path':path,'before':before.get(path),'after':after[path]})
    # A note cannot select its own mandate. Main's exact already-reviewed policy
    # and all explicit route guards must exist in both the original baseline and
    # current official main. Parsing rejects duplicate keys before equality.
    for tree_map in (before,after):
        stored = tree_map.get('tracker/config.json')
        if not stored or stored['type'] != 'blob' or stored['mode'] != '100644':
            raise ValueError('Capture requires the prior authoritative main mandate')
        official = json.loads(objects.blob(request,root,stored['sha']), object_pairs_hook=unique_object)
        if (bundle(official) != bundle(config) or official.get('project') != config.get('project')
                or rules.policy(official) != rules.policy(config)):
            raise ValueError('Capture cannot enable/change its own mandate or selected route')
    return dict(schema_version=1,repository=config['project']['repository'],subject=subject(selected),
                base_sha=base_sha,base_tree_sha=base_tree,head_sha=head_sha,head_tree_sha=head_tree,
                capture_policy_sha256=digest(value),files_sha256=digest(manifest))


def unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError('Ambiguous duplicate JSON key')
        result[key] = value
    return result


def require_official_config(request,config,revision):
    if not git_sha(revision):raise ValueError('Actual immutable official main is required')
    root='repos/'+config['project']['repository']
    row=request(root+'/contents/tracker/config.json?ref='+revision)
    actual=json.loads(rules_module().file_bytes(request,root,row),object_pairs_hook=unique_object)
    # Validate before equality: bool/float aliases cannot become a typed policy.
    rules_module().execution_policy(actual);rules_module().completion_policy(actual)
    rules_module().context_module().policy(actual);bundle(actual)
    if digest(actual)!=digest(config):raise ValueError('Use the exact reviewed official current-main modern configuration')
    return actual


def check_capture(request, config, pr, selected, shared):
    section = state(shared,config)
    operation = record(section,selected)
    if not integer(pr.get('number')) or not git_sha(pr.get('head',{}).get('sha')) or not git_sha(pr.get('base',{}).get('sha')):
        raise ValueError('Native capture PR needs its exact true positive identity and immutable head/base')
    intent=operation['pr_intent']
    if intent['state'] in ('dispatching','unknown'):
        raise ValueError('Reconcile the original owned unknown PR request by exact native GET before admission or finalization')
    repo = config['project']['repository']
    root = 'repos/' + repo
    stage = config['delivery_policy']['staging']
    promotion = stage['enabled'] and pr['base']['ref'] == config['project']['base_branch'] and pr['head']['ref'] == stage['branch']
    if (pr['head'].get('repo',{}).get('full_name','').lower() != repo.lower()
            or pr['base'].get('repo',{}).get('full_name','').lower() != repo.lower()
            or pr.get('draft') or pr.get('state') not in ('open','closed') or pr.get('state') == 'closed' and not pr.get('merged')):
        raise ValueError('Capture needs its actual same-repository ready source/promotion PR')
    if promotion:
        reservation = section.get('integration')
        reservation = reservation or section['completed'].get(str(pr['number']))
        if (not reservation or reservation.get('kind') != 'direct' or reservation.get('subject') != selected
                or reservation.get('promotion_pr')!=pr['number'] or intent['state']!='observed'
                or intent['purpose']!='promotion' or intent['pr']!=pr['number']):
            raise ValueError('Capture promotion has no exact retained direct reservation')
    elif (operation['source']['pr'] != pr['number'] or operation['source']['branch'] != pr['head']['ref']
          or operation['source']['head_sha'] != pr['head']['sha']):
        raise ValueError('Capture PR is not the exact source associated by its actual owner')
    rules_module().reject_closing((pr.get('title') or '')+'\n'+(pr.get('body') or ''))
    if not re.match(r'^\[Capture '+re.escape(selected['id'])+r'\]\s+\S',pr.get('title','')):
        raise ValueError('Capture title must name its exact public operation: [Capture <32hex>] Summary')
    return operation


def source_route(config, pr):
    stage = config['delivery_policy']['staging']
    main = config['project']['base_branch']
    if stage['enabled']:
        if pr['base']['ref'] == stage['branch'] and pr['head']['ref'] not in (main,stage['branch']):
            return 'stage'
        if pr['base']['ref'] == main and pr['head']['ref'] == stage['branch']:
            return 'promotion'
        raise ValueError('Modern staged source follows working branch → Stage → main')
    if pr['base']['ref'] != main or pr['head']['ref'] == stage['branch']:
        raise ValueError('Modern Stage-off source must target its actual main')
    return 'main'


def lane_for(section, selected):
    matches = [row for row in section['source_reviews'].values() if row['subject'] == selected]
    if len(matches) > 1:
        raise ValueError('Conflicting actual source Reviewers')
    return matches[0] if matches else None


def current_scope(request, config, selected, operation=None):
    if selected['kind'] == 'task':
        return rules_module().task_scope(request('repos/'+config['project']['repository']+'/issues/'+str(task_number(selected))), config)
    if not operation or not operation['scope'] or not operation['assessment'] or operation['assessment']['verdict'] != 'within-mandate':
        raise ValueError('Capture full-diff mandate assessment is absent')
    return operation['scope']


def review_matches(receipt, config, pr, content, selected):
    if (not isinstance(receipt, dict) or receipt.get('subject') != selected
            or receipt.get('kind')!='independent-review' or receipt.get('repository')!=config['project']['repository']
            or not integer(receipt.get('pr')) or not integer(pr.get('number')) or receipt['pr']!=pr['number']
            or any(not git_sha(receipt.get(k)) for k in ('head_sha','base_sha','tree_sha','main_sha'))
            or not hashed(receipt.get('assessment_sha256')) or not timestamp(receipt.get('recorded_at'))
            or not isinstance(receipt.get('review_session'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}',receipt['review_session'])
            or receipt.get('verdict') != 'approved' or receipt.get('policy') != rules_module().fingerprint(config)
            or receipt.get('route_policy_sha256') != digest(bundle(config))
            or receipt.get('head_sha') != pr['head']['sha'] or receipt.get('tree_sha') != content
            or not hashed(receipt.get('report_sha256')) or not hashed(receipt.get('execution_lane_sha256'))
            or not isinstance(receipt.get('reviewer'),str) or not re.fullmatch(r'Reviewer-[A-Za-z0-9]{4,32}',receipt['reviewer'])
            or not text(receipt.get('implementer')) or receipt['reviewer'] == receipt['implementer']):
        return False
    return True


def compatible(request, config, receipt, current):
    """Retain original source review; require explicit independent forward proof."""
    previous = receipt['main_sha']
    predecessor = None
    root = 'repos/' + config['project']['repository']
    base_digest = digest({k:v for k,v in receipt.items() if k != 'compatibility'})
    for row in receipt.get('compatibility', []):
        expected = dict(kind='direct-source-compatibility',repository=config['project']['repository'],subject=receipt['subject'],
            source_pr=receipt['pr'],source_head_sha=receipt['head_sha'],source_tree_sha=receipt['tree_sha'],
            scope_sha256=digest(receipt['scope']),original_review_sha256=base_digest,
            previous_main_sha=previous,previous_receipt_sha256=predecessor,policy=receipt['policy'],
            route_policy_sha256=receipt['route_policy_sha256'])
        if (not isinstance(row,dict) or any(row.get(k)!=v for k,v in expected.items())
                or row.get('verdict')!='compatible' or not git_sha(row.get('main_sha')) or row['main_sha']==previous
                or not git_sha(row.get('combined_head_sha')) or not git_sha(row.get('combined_tree_sha'))
                or not hashed(row.get('report_sha256')) or row.get('reviewer')!=receipt['reviewer']
                or row.get('review_session')!=receipt['review_session']
                or row.get('execution_lane_sha256')!=receipt['execution_lane_sha256']
                or request(root+'/compare/'+previous+'...'+row['main_sha']).get('status')!='ahead'):
            raise ValueError('Invalid genuine independent forward-main compatibility chain')
        commit = request(root+'/git/commits/'+row['combined_head_sha'])
        if ([p.get('sha') for p in commit.get('parents',[])] != [row['main_sha'],receipt['head_sha']]
                or commit.get('tree',{}).get('sha')!=row['combined_tree_sha']):
            raise ValueError('Compatibility needs the exact native current-main/source combined candidate')
        previous,predecessor = row['main_sha'],digest(row)
    return previous == current


def reviewed_source(request, config, shared, selected, *, completed=False):
    rules = rules_module()
    section = state(shared,config)
    row = record(section,selected)
    binding = row.get('handoff')
    if not binding:
        raise ValueError('Source has no exact retained Worker handoff')
    source_pr = binding['source']['pr']
    receipt = shared.get('delivery_workflow',{}).get('reviews',{}).get(str(source_pr),{})
    pr = request('repos/'+config['project']['repository']+f'/pulls/{source_pr}')
    if (not review_matches(receipt,config,pr,binding['source']['tree_sha'],selected)
            or receipt.get('scope') != current_scope(request,config,selected,row if selected['kind']=='capture' else None)
            or receipt['head_sha'] != binding['source']['head_sha']):
        raise ValueError('Independent source review is missing/stale for the actual scope/source/policy')
    lane = lane_for(section,selected)
    if not completed:
        if (not lane or lane.get('state')!='reviewing' or lane['pr']!=source_pr
                or digest(lane['id'])!=receipt['execution_lane_sha256']
                or (lane['actor'],lane['session'])!=(receipt['reviewer'],receipt.get('review_session'))):
            raise ValueError('Retain the genuine exact independent source Reviewer')
        if any(w['actor']==lane['actor'] or w['session']==lane['session'] for w in row['implementers']):
            raise ValueError('Source Reviewer must remain independent of every implementer')
    return pr,receipt,lane


def admit(request, config, pr, selected, shared):
    rules = rules_module()
    section = state(shared,config)
    root = 'repos/' + config['project']['repository']
    if pr.get('draft') or pr.get('state')=='closed' and not pr.get('merged'):
        raise ValueError('Draft/abandoned source cannot enter main or Stage')
    direction = rules.route(config,pr,request)
    if direction not in ('main','stage','promotion','process'):
        raise ValueError('This source follows actual batch approval/integration, not direct admission')
    reservation = section.get('integration')
    archived = False
    if pr.get('merged') and section['completed'].get(str(pr['number'])):
        reservation = section['completed'][str(pr['number'])]
        archived = True
    if (not isinstance(reservation,dict) or reservation.get('kind')!='direct' or reservation.get('subject')!=selected
            or reservation.get('state')=='fixing' or archived and reservation.get('finalization',{}).get('phase')!='released'):
        raise ValueError('This exact source must own the one current direct integration reservation')
    source_pr,review,lane = reviewed_source(request,config,shared,selected,completed=archived)
    if selected['kind']=='capture':check_capture(request,config,pr,selected,shared)
    if direction=='process':
        process=process_module();actual=process.classify(request,config,pr)
        if (not actual or review.get('process_policy')!=process.fingerprint(config) or review.get('process_proof')!=actual
                or review.get('base_sha')!=actual['base_sha'] or not archived and lane.get('route')!='process'):
            raise ValueError('Modern process source needs its exact retained instruction-only proof and actual independent lane')
        rules.checked(request,root,pr['head']['sha'],process.policy(config)['required_checks'])
    if (reservation['source_pr']!=source_pr['number'] or reservation['review_sha256']!=digest(review)
            or reservation['scope_sha256']!=digest(review['scope'])
            or reservation['route_policy_sha256']!=digest(bundle(config))):
        raise ValueError('Direct reservation source/review/scope changed; retain ownership and genuinely re-review')
    if not archived and (reservation['actor'],reservation['session'],reservation['review_claim_sha256'])!=(lane['actor'],lane['session'],digest(lane['id'])):
        raise ValueError('Direct reservation and actual held source Reviewer must agree')
    actual_main = request(root+'/git/ref/heads/'+config['project']['base_branch'])['object']['sha']
    main_before = rules.merge_base(request,root,pr) if pr.get('merged') and direction in ('main','process','promotion') else actual_main
    if direction=='promotion':
        candidate = shared.get('delivery_workflow',{}).get('promotions',{}).get(str(pr['number'])) or shared.get('delivery_workflow',{}).get('stage',{})
        if (candidate.get('state') not in ('passed','promoted') or candidate.get('subject')!=selected
                or candidate.get('reservation_sha256')!=digest(reservation['id'])
                or candidate.get('policy')!=rules.fingerprint(config)
                or rules.pr_tree(request,root,pr)!=candidate.get('tree_sha')
                or rules.tree(request,root,main_before)!=candidate.get('main_tree')
                or reservation.get('promotion_pr') not in (None,pr['number'])):
            raise ValueError('Promotion needs exact passed Stage/source/reservation/current-main proof')
    else:
        if pr['number']!=reservation['source_pr'] or pr['head']['sha']!=reservation['source_head_sha']:
            raise ValueError('Another source cannot use this direct reservation')
        if not compatible(request,config,review,main_before):
            raise ValueError('Main advanced; obtain actual independent source compatibility and current combined checks')
        expected_base = main_before
        actual_base = rules.merge_base(request,root,pr) if pr.get('merged') else pr['base']['sha']
        if direction=='stage':
            if rules.tree(request,root,actual_base)!=rules.tree(request,root,main_before):
                prior=shared.get('delivery_workflow',{}).get('stage',{})
                if (not reservation['entered_stage'] or prior.get('subject')!=selected
                        or prior.get('reservation_sha256')!=digest(reservation['id']) or prior.get('pr')==pr['number']
                        or prior.get('tree_sha')!=rules.tree(request,root,actual_base)
                        or prior.get('main_tree')!=rules.tree(request,root,main_before)):
                    raise ValueError('Stage must start from current main or its exact retained same-operation Stage before an owned fix')
        elif actual_base != expected_base:
            raise ValueError('Actual merged/base source differs from reviewed current main')
        if review.get('compatibility'):
            latest=review['compatibility'][-1]
            rules.checked(request,root,latest['combined_head_sha'],config['delivery_policy']['required_checks'])
            if pr.get('merged') and rules.pr_tree(request,root,pr)!=latest['combined_tree_sha']:
                raise ValueError('Actual merge content differs from independently checked combined candidate')
    if selected['kind']=='task':
        rules.dependencies_ready(request,config,[task_number(selected)],shared)
    else:
        operation = record(section,selected)
        require_official_config(request,config,actual_main)
        if capture_scope(request,config,selected,operation['scope']['base_sha'],source_pr['head']['sha'])!=operation['scope']:
            raise ValueError('Capture complete immutable scope changed')
    rules.checked(request,root,pr['head']['sha'],config['delivery_policy']['required_checks'])
    for page in range(1,101):
        commits=request(root+f'/pulls/{pr["number"]}/commits?per_page=100&page={page}')
        for commit in commits:rules.reject_closing(commit.get('commit',{}).get('message',''))
        if len(commits)<100:break
    else:raise ValueError('Cannot verify bounded actual source commit history')
    result=dict(tree_sha=rules.pr_tree(request,root,pr),policy=rules.fingerprint(config),route=direction,
                subject=selected,main_sha=main_before,source_pr=reservation['source_pr'],
                scope_sha256=reservation['scope_sha256'],review_sha256=reservation['review_sha256'],
                reservation_sha256=digest(reservation['id']))
    if direction=='process':result['process_policy']=process_module().fingerprint(config)
    if not pr.get('merged') and direction!='promotion' and review.get('compatibility'):
        result['tree_sha']=review['compatibility'][-1]['combined_tree_sha']
    return result
