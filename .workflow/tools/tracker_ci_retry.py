"""Canonical stdlib-only rules for explicitly selected bounded CI retries.

Structural policy validation is also used by a trusted fixed Master snapshot.
Selection and artifact support never activate private state or dispatch work.
"""
import hashlib
import datetime
import json
import re

INTENT_STATES = ('reserved', 'dispatching', 'accepted', 'dispatch_unknown', 'observed',
                 'rejected_no_effect', 'cancelled_before_dispatch')
CATEGORIES = ('product', 'transient-infrastructure', 'configuration', 'account', 'unknown')
CONCLUSIONS = ('success','failure','neutral','cancelled','skipped','timed_out',
               'action_required','stale','startup_failure')
SHA = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[0-9a-f]{32}\Z')
GIT_SHA = re.compile(r'[0-9a-f]{40}\Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= 65536


def _id(value):
    return isinstance(value, str) and bool(ID.fullmatch(value))


def _hash(value):
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def _time(value):
    if not _text(value):return False
    try:return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).utcoffset() is not None
    except ValueError:return False


def governance_anchor(section):
    """First explicit adoption is permanent, never a rolling retry window."""
    history=section['history']
    if (history[0].get('event')!='adopted'
            or sum(row.get('event')=='adopted' for row in history)!=1):
        raise ValueError('CI retry requires one retained first-adoption governance anchor')
    return history[0]['at']


def _owner(actor,session):
    return (isinstance(actor,str) and bool(re.fullmatch(r'Maintainer-[A-Za-z0-9]{4,32}',actor))
            and isinstance(session,str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{4,100}',session)))


def _history(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(row, dict) and _text(row.get('event')) and _time(row.get('at')) for row in value))


def native_key(run, attempt):
    if type(run) is not int or run < 1 or type(attempt) is not int or attempt < 1:
        raise ValueError('Invalid native execution identity')
    return str(run) + ':' + str(attempt)


def native_metadata(status,conclusion,triggering_actor):
    """Unknown is explicit null; malformed values never become failure/identity."""
    if (status not in ('queued','in_progress','completed')
            or conclusion is not None and (not isinstance(conclusion,str) or conclusion not in CONCLUSIONS)
            or status!='completed' and conclusion is not None
            or triggering_actor is not None and (type(triggering_actor) is not int or triggering_actor<1)):
        raise ValueError('Invalid native outcome metadata; require supported conclusion and true positive actor ID or null')


def _path(value):
    return (isinstance(value,str) and bool(value) and not value.startswith(('/','-'))
            and not any(x in value.split('/') for x in ('','.','..'))
            and not any(x in value for x in '\n\r\\'))


def specification(value):
    fields={'schema_version','closure_paths','input_paths','profile','environment','unrelated_workflows'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value.get('schema_version')) is not int
            or value['schema_version']!=1 or not isinstance(value.get('profile'),str)
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',value['profile'])
            or not isinstance(value.get('environment'),dict)
            or set(value['environment'])!={'platform','runner_labels','container','runtime'}
            or not isinstance(value.get('unrelated_workflows'),dict)):
        raise ValueError('Plan needs schema1, exact closure/input paths, profile, environment and unrelated_workflows')
    env=value['environment']
    if (not isinstance(env['runner_labels'],list) or not env['runner_labels']
            or any(not _text(x) or len(x)>100 for x in env['runner_labels'])
            or any(not _text(env[f]) or len(env[f])>200 for f in ('platform','runtime'))
            or env['container'] is not None and (not _text(env['container']) or len(env['container'])>300)):
        raise ValueError('Use bounded redacted platform/runner/container/runtime facts, never arbitrary environment variables')
    for field in ('closure_paths','input_paths'):
        paths=value[field]
        if (not isinstance(paths,list) or not paths or len(paths)>100
                or any(not _path(path) for path in paths) or len(set(paths))!=len(paths)):
            raise ValueError('Plan paths must be explicit unique repository paths (1..100)')
    if not set(value['closure_paths'])<=set(value['input_paths']):
        raise ValueError('Budget inputs must include every reviewed workflow/reusable/script/producer closure path')
    if any(not _path(p) or not p.startswith('.github/workflows/') or not _hash(h)
           for p,h in value['unrelated_workflows'].items()):
        raise ValueError('Unrelated workflow exclusions need exact reviewed immutable bytes')
    if not set(value['unrelated_workflows'])<=set(value['closure_paths']):
        raise ValueError('An unrelated workflow purpose must be included in the full reviewed closure too')
    digest(value)
    return value


def identity(value):
    fields={'repository','run','attempt','original_sha','original_ref','original_actor','event','workflow_id',
            'workflow_path','workflow_sha256','closure','inputs','profile','environment','jobs','pr','route',
            'head_sha','base_sha','tested_sha','tree_sha','issue_scopes','parents'}
    if (not isinstance(value,dict) or set(value) not in (fields,fields|{'composition'})
            or not isinstance(value.get('repository'),str) or not re.fullmatch(r'[\w.-]+/[\w.-]+',value['repository'])
            or any(not isinstance(value.get(k),str) or not GIT_SHA.fullmatch(value[k])
                   for k in ('original_sha','head_sha','tested_sha','tree_sha'))
            or value.get('base_sha') is not None and (not isinstance(value['base_sha'],str) or not GIT_SHA.fullmatch(value['base_sha']))
            or not isinstance(value.get('original_actor'),dict) or set(value['original_actor'])!={'id','login'}
            or type(value['original_actor']['id']) is not int or value['original_actor']['id']<1
            or not _text(value['original_actor']['login']) or not _text(value.get('original_ref'))
            or not _text(value.get('event')) or not _text(value.get('profile')) or not _text(value.get('route'))
            or type(value.get('workflow_id')) is not int or value['workflow_id']<1
            or not _path(value.get('workflow_path')) or not value['workflow_path'].startswith('.github/workflows/')
            or not _hash(value.get('workflow_sha256')) or not isinstance(value.get('closure'),dict)
            or not value['closure'] or not isinstance(value.get('inputs'),dict) or not value['inputs']
            or any(not _path(p) or not _hash(h) for group in ('inputs','closure') for p,h in value[group].items())
            or value['closure'].get(value['workflow_path'])!=value['workflow_sha256']
            or value.get('pr') is not None and (type(value['pr']) is not int or value['pr']<1)
            or not isinstance(value.get('issue_scopes'),dict) or any(not _text(k) or not _text(v) for k,v in value['issue_scopes'].items())
            or not isinstance(value.get('parents'),list) or any(not isinstance(p,str) or not GIT_SHA.fullmatch(p) for p in value['parents'])
            or not isinstance(value.get('jobs'),list) or not value['jobs'] or any(not _hash(j) for j in value['jobs'])
            or 'composition' in value and not _hash(value['composition'])):
        raise ValueError('Invalid strict CI retry native identity')
    native_key(value['run'],value['attempt'])
    return value


def validate(section):
    """Validate the portable ledger, without consulting API, clock or engine.

    Recorded observations, dispatches and diagnoses are separate concepts. In
    particular, an interval in an accounting report is never a budget unit.
    """
    if (not isinstance(section, dict) or set(section) != {'schema_version', 'base_schema_version',
            'repository', 'policy', 'episodes', 'diagnoses', 'causes', 'history'}
            or type(section.get('schema_version')) is not int or section['schema_version'] != 1
            or type(section.get('base_schema_version')) is not int or section['base_schema_version'] not in (1, 2, 4, 5)
            or not isinstance(section.get('repository'), str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', section['repository'])
            or not _history(section.get('history'))):
        raise ValueError('Invalid CI retry state protocol')
    policy({'ci_retry_policy': section['policy']})
    governance_anchor(section)
    if any(not isinstance(section.get(key), dict) for key in ('episodes', 'diagnoses', 'causes')):
        raise ValueError('Invalid CI retry ledger collections')
    for key, row in section['episodes'].items():
        if (not _id(key) or not isinstance(row, dict) or set(row)!={'schema_version','id','lineage','inputs','identity','specification','previous','observations','intents','history'} or row.get('id') != key
                or row.get('schema_version') != 1 or type(row.get('schema_version')) is not int
                or not _id(row.get('lineage')) or not _hash(row.get('inputs'))
                or not isinstance(row.get('identity'), dict) or not _history(row.get('history'))
                or row.get('previous') is not None and row['previous'] not in section['episodes']
                or not isinstance(row.get('observations'), dict) or not row['observations']
                or not isinstance(row.get('intents'), dict)):
            raise ValueError('Invalid CI retry episode')
        identity(row['identity'])
        specification(row.get('specification'))
        if (row['identity']['repository']!=section['repository'] or row['identity']['environment']!=row['specification']['environment']
                or set(row['identity']['inputs'])!=set(row['specification']['input_paths'])
                or set(row['identity']['closure'])!=set(row['specification']['closure_paths'])
                or row['inputs']!=digest({'inputs':row['identity']['inputs'],'environment':row['identity']['environment']})
                or row.get('previous')==key):
            raise ValueError('CI retry episode lineage/input relation changed')
        for observation_key, observation in row['observations'].items():
            if (not isinstance(observation, dict) or set(observation)!={'run','attempt','status','conclusion','triggering_actor','observed_at','relation'}
                    or observation_key != native_key(observation.get('run'), observation.get('attempt'))
                    or observation.get('status') not in ('queued', 'in_progress', 'completed')
                    or observation.get('relation') not in ('equivalent-inputs','unknown-inputs-held')
                    or not _time(observation.get('observed_at'))):
                raise ValueError('Invalid CI retry native observation')
            native_metadata(observation['status'],observation['conclusion'],observation['triggering_actor'])
        for intent_id, intent in row['intents'].items():
            fields={'id','state','actor','session','principal','at','identity','diagnosis','run','expected_attempt','history'}
            if (not _id(intent_id) or not isinstance(intent, dict) or set(intent) not in (fields,fields|{'observation'}) or intent.get('id') != intent_id
                    or intent.get('state') not in INTENT_STATES or not _owner(intent.get('actor'),intent.get('session'))
                    or not _time(intent.get('at')) or type(intent.get('run')) is not int or intent['run']<1
                    or not _hash(intent.get('identity')) or not _id(intent.get('diagnosis'))
                    or intent['diagnosis'] not in section['diagnoses']
                    or type(intent.get('expected_attempt')) is not int or intent['expected_attempt'] < 2
                    or not _history(intent.get('history'))):
                raise ValueError('Invalid CI retry intent')
            if (not isinstance(intent.get('principal'),dict) or set(intent['principal'])!={'id','login'}
                    or type(intent['principal']['id']) is not int or intent['principal']['id']<1 or not _text(intent['principal']['login'])
                    or not isinstance(section['diagnoses'][intent['diagnosis']],dict)
                    or section['diagnoses'][intent['diagnosis']].get('episode')!=key):
                raise ValueError('Invalid CI retry intent principal/episode')
            if intent['state'] == 'observed' and intent.get('observation') not in row['observations']:
                raise ValueError('Observed CI retry needs an attributable native execution')
            events=[e['event'] for e in intent['history']]
            if (events[0]!='reserved' or events.count('dispatch-barrier')>1
                    or intent['state']=='rejected_no_effect'
                    or intent['state'] in ('reserved','cancelled_before_dispatch') and 'dispatch-barrier' in events
                    or intent['state'] in ('dispatching','accepted','dispatch_unknown','observed') and 'dispatch-barrier' not in events
                    or intent['state']=='cancelled_before_dispatch' and 'stopped-before-barrier' not in events
                    or intent['state']=='observed' and ('accepted' not in events
                        or intent['observation']!=native_key(intent['run'],intent['expected_attempt'])
                        or row['observations'][intent['observation']].get('triggering_actor')!=intent['principal']['id'])):
                raise ValueError('Invalid CI retry barrier/outcome provenance; no-effect POST recovery is unsupported in v1')
    for key, row in section['episodes'].items():
        seen={key}
        previous=row['previous']
        while previous is not None:
            ancestor=section['episodes'][previous]
            if previous in seen or ancestor['lineage']!=row['lineage']:
                raise ValueError('CI retry lineage must retain an acyclic same-lineage fix history')
            seen.add(previous)
            previous=ancestor['previous']
    for key, row in section['diagnoses'].items():
        fields={'schema_version','id','kind','episode','category','identity','report_sha256','plan_sha256','principal','actor','session','at','trust'}
        if (not _id(key) or not isinstance(row, dict) or set(row) not in (fields,fields|{'instruction'}) or row.get('id') != key
                or row.get('schema_version') != 1 or type(row.get('schema_version')) is not int
                or row.get('episode') not in section['episodes'] or row.get('category') not in CATEGORIES
                or row.get('kind') not in ('diagnosis','instruction')
                or not _hash(row.get('identity')) or not _hash(row.get('report_sha256'))
                or not _hash(row.get('plan_sha256')) or not isinstance(row.get('principal'), dict)
                or type(row['principal'].get('id')) is not int or row['principal']['id'] < 1
                or set(row['principal'])!={'id','login'} or not _text(row['principal'].get('login')) or not _time(row.get('at'))
                or not _owner(row.get('actor'),row.get('session'))
                or row.get('trust') != 'operator-attested; report digest preserves input, not correctness'):
            raise ValueError('Invalid operator-attested CI diagnosis')
        if row['kind']=='instruction':
            instruction=row.get('instruction')
            if (not isinstance(instruction,dict) or set(instruction)!={'source_sha256','message_sha256','decision_sha256','time','action','episode','causes','basis'}
                    or any(not _hash(instruction[k]) for k in ('source_sha256','message_sha256','decision_sha256'))
                    or instruction.get('action')!='one-full-verification-rerun' or instruction.get('episode')!=row['episode']
                    or not _time(instruction.get('time')) or instruction.get('basis')!='actual-direct-human-instruction'
                    or not isinstance(instruction.get('causes'),list) or not instruction['causes']
                    or any(not _id(c) or c not in section['causes'] for c in instruction['causes'])
                    or len(set(instruction['causes']))!=len(instruction['causes'])):
                raise ValueError('Invalid bounded operator-attested specific instruction')
    for key, row in section['causes'].items():
        if (not _id(key) or not isinstance(row, dict) or set(row)!={'schema_version','id','diagnosis','category','state','relevance','history'} or row.get('id') != key
                or type(row.get('schema_version')) is not int or row['schema_version']!=1
                or row.get('diagnosis') not in section['diagnoses'] or row.get('category') not in CATEGORIES
                or row.get('state') not in ('unresolved', 'resolved') or not _hash(row.get('relevance'))
                or not _history(row.get('history'))):
            raise ValueError('Invalid persistent CI retry cause')
        if (section['diagnoses'][row['diagnosis']]['category']!=row['category']
                or section['diagnoses'][row['diagnosis']]['kind']!='diagnosis'):
            raise ValueError('Persistent cause cannot be renamed away from its original diagnosis')
    return section


def state(shared, config, *, projected=False):
    """Selection is separate from activation; all configured readers are fenced."""
    if shared.get('schema_version') == 8:
        try:
            from . import pause_policy
        except ImportError:
            import tracker_pause_policy as pause_policy
        shared = pause_policy.decode(shared)
    selected = policy(config)
    section = shared.get('ci_retry')
    summary = projected and isinstance(section,dict) and set(section)=={'policy','repository'} and 'schema_version' not in shared
    outer=shared.get('schema_version')
    held=shared.get('maintenance')
    effective = held.get('base_schema_version') if type(outer) is int and outer == 3 and isinstance(held, dict) else outer
    if effective == 7 or 'execution_contract' in shared:
        try:
            from . import direct_policy
        except ImportError:
            import tracker_direct_policy as direct_policy
        effective = direct_policy.protocol(shared)
    fenced = type(effective) is int and effective == 6
    if not summary and ('ci_retry' in shared or fenced):
        if not fenced:
            raise ValueError('CI retry requires the fenced shared state protocol')
        validate(section)
    if (selected != (section or {}).get('policy')
            or section is not None and section['repository'] != config['project']['repository']):
        raise ValueError('CI retry policy changed or is not activated; use owned idle ci-retry adopt after compatible publication')
    return section


def pending(shared):
    return any(any(intent['state'] in ('reserved', 'dispatching', 'accepted', 'dispatch_unknown')
                   for intent in episode['intents'].values())
               or any(row['status'] in ('queued','in_progress') for row in episode['observations'].values())
               for episode in shared.get('ci_retry', {}).get('episodes', {}).values())


def consumed(episode):
    """Unknown POSTs retain a unit even when an unattributed run later appears."""
    return (len(episode['observations']) + sum(intent['state'] in
            ('reserved', 'dispatching', 'accepted', 'dispatch_unknown') for intent in episode['intents'].values()))


def policy(config):
    if 'ci_retry_policy' not in config:
        return None
    value = config['ci_retry_policy']
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'max_executions'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['max_executions']) is not int or value['max_executions'] not in (1, 2)):
        raise ValueError('CI retry policy needs schema_version 1 and explicit max_executions 1 or 2')
    return value
