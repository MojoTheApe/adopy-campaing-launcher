"""Portable task-pause wire fence and safe projection; no engine/API imports.

Outer8 protects the complete incumbent1..7 protocol. Native readers work with a
decoded view; every writer must encode it again. This is not a new route/policy.
"""
import copy
import datetime
import re

FIELD = 'task_pause'
WIRE = 8
KINDS = ('implementing', 'reviewing', 'merging')


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|\+00:00)', value):
        return False
    try:
        datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False


def text(value, limit=1000000):
    return isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= limit


def sha(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{40}', value))


def validate(state):
    if not isinstance(state, dict):
        raise ValueError('Task pause requires an object state')
    if (FIELD in state or state.get('schema_version') == WIRE) and type(state.get('schema_version')) is not int:
        raise ValueError('Task pause requires an integer shared state protocol')
    value = state.get(FIELD)
    if value is None:
        if state.get('schema_version') == WIRE:
            raise ValueError('Task-pause fence requires its exact retained contract')
        return
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'base_schema_version', 'repository', 'activation', 'records'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or type(value.get('base_schema_version')) is not int or value['base_schema_version'] not in range(1, 8)
            or not isinstance(value.get('repository'), str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', value['repository'])
            or not isinstance(value.get('records'), dict) or len(value['records']) > 10000):
        raise ValueError('Invalid task-pause contract')
    activation = value['activation']
    if (not isinstance(activation, dict) or set(activation) != {'at', 'actor', 'session', 'main_sha', 'report'}
            or not timestamp(activation.get('at')) or not text(activation.get('actor'),100)
            or not re.fullmatch(r'Maintainer-[A-Za-z0-9]{4,32}', activation['actor']) or not text(activation.get('session'),100)
            or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}',activation['session']) or not sha(activation.get('main_sha')) or not text(activation.get('report'))):
        raise ValueError('Task pause requires retained owned compatible-reader/writer activation evidence')
    occupied = set()
    required = {'id', 'kind', 'issues', 'state', 'paused_at', 'actor', 'session', 'report',
                'checkpoint', 'statuses', 'scopes', 'main_sha', 'source_heads'}
    for key, row in value['records'].items():
        if (not isinstance(row, dict) or set(row) - required - {'resumed_at', 'resumed_by', 'new_claim'}
                or not required <= set(row) or key != row.get('id')
                or not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{32}', key)
                or row.get('kind') not in KINDS or row.get('state') not in ('paused', 'resumed')
                or not timestamp(row.get('paused_at')) or not text(row.get('actor'), 100)
                or not text(row.get('session'), 100) or not text(row.get('report'))
                or not isinstance(row.get('checkpoint'), dict) or not row['checkpoint']
                or not isinstance(row.get('issues'), list) or not row['issues']
                or any(type(n) is not int or n <= 0 for n in row['issues'])
                or len(set(row['issues'])) != len(row['issues']) or not sha(row.get('main_sha'))):
            raise ValueError('Invalid retained task pause')
        numbers = {str(n) for n in row['issues']}
        if (not isinstance(row.get('statuses'), dict) or set(row['statuses']) != numbers
                or any(not isinstance(v, dict) or set(v) != {'id', 'phase'}
                       or not text(v.get('id'), 100) or v.get('phase') not in
                       ('in_progress', 'review', 'approved', 'stage', 'merged') for v in row['statuses'].values())
                or not isinstance(row.get('scopes'), dict) or set(row['scopes']) != numbers
                or any(not isinstance(s, str) or not re.fullmatch(r'(?:v2:)?[a-f0-9]{64}', s) for s in row['scopes'].values())
                or not isinstance(row.get('source_heads'), dict)
                or any(not re.fullmatch(r'[1-9][0-9]*', k) or not sha(v) for k, v in row['source_heads'].items())):
            raise ValueError('Task pause requires exact native position, scope and source')
        if row['state'] == 'paused':
            subjects = {(row['kind'], n) for n in row['issues']}
            if occupied.intersection(subjects) or any(k in row for k in ('resumed_at', 'resumed_by', 'new_claim')):
                raise ValueError('Conflicting active task pauses')
            occupied.update(subjects)
            checkpoint = row['checkpoint']
            resource, owner = checkpoint.get('resource'), checkpoint.get('owner')
            if resource not in ('legacy', 'legacy-observation', 'work', 'source-review', 'integration-review', 'integration') or not isinstance(owner, dict) or not owner:
                raise ValueError('Pause requires its exact original resource checkpoint')
            section = state.get('execution', {})
            if row['kind'] == 'implementing':
                held = list(section.get('claims', {}).values())
            elif row['kind'] == 'reviewing':
                held = [*section.get('source_reviews', {}).values(), section.get('review') or {}]
            else:
                held = [section.get('integration') or {}]
            for current in held:
                subjects = current.get('issues') or [current.get('issue') or current.get('subject', {}).get('id')]
                if set(row['issues']).intersection(subjects):
                    raise ValueError('Paused executor subject cannot also retain an active owner in that role')
            if resource.startswith('legacy') and isinstance(state.get('active'), dict) and state['active'].get('issue') in row['issues']:
                raise ValueError('Paused legacy task cannot retain its shared slot')
        elif (not timestamp(row.get('resumed_at')) or not text(row.get('resumed_by'), 100)
              or not isinstance(row.get('new_claim'), str) or not re.fullmatch(r'[a-f0-9]{32}', row['new_claim'])):
            raise ValueError('Resumed pause requires its actual fresh authority')


def decode(state):
    """Decode only the known exact wire fence; never infer/adopt another wrapper."""
    if state.get('schema_version') != WIRE:
        if FIELD in state:
            raise ValueError('Task pause requires the outer8 reader/writer fence')
        return state
    validate(state)
    result = copy.deepcopy(state)
    result['schema_version'] = result[FIELD]['base_schema_version']
    return result


def encode(state):
    if FIELD not in state:
        return state
    if state.get('schema_version') == WIRE:
        validate(state)
        return state
    if type(state.get('schema_version')) is not int or state['schema_version'] not in range(1, 8):
        raise ValueError('Task pause must retain a known incumbent protocol')
    result = copy.deepcopy(state)
    result[FIELD]['base_schema_version'] = result['schema_version']
    result['schema_version'] = WIRE
    validate(result)
    return result


def active(state, number=None):
    validate(state)
    return [r for r in state.get(FIELD, {}).get('records', {}).values()
            if r['state'] == 'paused' and (number is None or number in r['issues'])]


def require_unpaused(state, number):
    if active(state, number):
        raise ValueError('Task is paused; use explicit pause resume with fresh ownership')


def integration_held(state):
    return any(r['kind'] == 'merging' or r['checkpoint'].get('integration') for r in active(state))


def public(state):
    return {str(n): {'state': 'paused', 'kind': row['kind'], 'paused_at': row['paused_at'],
                     'previous_status': row['statuses'][str(n)]['id'],
                     'previous_phase': row['statuses'][str(n)]['phase'],
                     'category': 'delivery' if row['kind']=='merging' or row['checkpoint']['resource']=='integration-review'
                     else {'in_progress':'in_progress','review':'review','approved':'delivery','stage':'delivery','merged':'delivery'}[row['statuses'][str(n)]['phase']]}
            for row in active(state) for n in row['issues']}


def legacy_review_bindings(state, numbers, pr=None):
    """Real current/retained fresh review bindings, never a liveness producer.

    A finished legacy clock does not erase the provenance of its completed
    review. Any subsequent pause revokes that binding through its checkpoint.
    """
    numbers=set(numbers)
    rows=[row for row in state.get(FIELD,{}).get('records',{}).values()
          if row['kind']=='reviewing' and row['checkpoint']['resource']=='legacy-observation'
          and numbers.intersection(row['issues'])]
    revoked={row['checkpoint']['owner'].get('id') for row in rows}
    tracked = pr is None or any(str(pr) in row['source_heads'] for row in rows)
    candidates=[dict(id=row['new_claim'],actor=row['resumed_by'],session=row['checkpoint']['resumed_session'],issues=row['issues'])
                for row in rows if tracked and row['state']=='resumed' and row['new_claim'] not in revoked]
    actual=[state.get('active') or {},*state.get('activity_timers',{}).values()]
    candidates += [owner for owner in actual if owner.get('kind')=='reviewing' and owner.get('id')
                   and numbers.intersection(owner.get('issues') or [owner.get('issue')])]
    result={}
    for owner in candidates:
        identity=(owner.get('id'),owner.get('actor'),owner.get('session'))
        result[identity]=owner
    return list(result.values())


def require_legacy_review(state,numbers,document,pr=None):
    for owner in legacy_review_bindings(state,numbers,pr):
        if (document or {}).get('execution_lane')!=owner['id'] or (document or {}).get('reviewer')!=owner['actor'] or (document or {}).get('review_session')!=owner['session']:
            raise ValueError('Legacy review requires the genuine freshly resumed Reviewer actor/session/claim')
