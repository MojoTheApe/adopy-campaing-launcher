"""Portable lossless state archives. Semantic callers always receive full state.

Outer9 wraps (and preserves) the incumbent wire format. Immutable archive bytes
are disposable-cacheable; only the current Git state blob establishes authority.
"""
import base64
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

WIRE = 9
LIMIT = 1024 * 1024
ROTATE = 750 * 1024
MAX_ARCHIVE = 48 * 1024 * 1024
MAPS = (('execution', 'tasks'), ('execution', 'approvals'), ('execution', 'batches'),
        ('execution', 'completed'), ('execution', 'captures', 'operations'),
        ('delivery_workflow', 'tasks'), ('delivery_workflow', 'reviews'),
        ('delivery_workflow', 'promotions'), ('delivery_workflow', 'main_batches'),
        ('delivery_workflow', 'acceptances'))
LISTS = (('execution', 'history'), ('delivery_workflow', 'history'), ('maintenance_history',))
MISSING = object()
CACHE = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'task-tracker/state-archive'


class ArchiveConflict(ValueError):
    """The archive/live transaction did not advance the expected branch."""


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate state archive JSON key')
        value[key] = item
    return value


def loads(raw):
    def invalid(_):
        raise ValueError('Non-finite state archive number')
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('Invalid state archive JSON') from error


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def identity(value, length=40):
    return isinstance(value, str) and bool(re.fullmatch('[a-f0-9]{' + str(length) + '}', value))


def validate_activation(value):
    keys = {'schema_version', 'id', 'actor', 'session', 'claim', 'at', 'main_sha', 'source_state_sha', 'report'}
    if (not isinstance(value, dict) or set(value) != keys or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or not identity(value.get('id'), 32)
            or not identity(value.get('claim'), 32) or not identity(value.get('main_sha'))
            or not identity(value.get('source_state_sha'))
            or not isinstance(value.get('actor'), str) or not re.fullmatch(r'Maintainer-[A-Za-z0-9]{4,32}', value['actor'])
            or not isinstance(value.get('session'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{4,100}', value['session'])
            or not isinstance(value.get('report'), str) or not value['report'].strip()
            or len(value['report'].encode()) > 65536 or not isinstance(value.get('at'), str)
            or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|\+00:00)', value['at'])):
        raise ValueError('State archive requires its retained owned activation evidence')
    try: datetime.datetime.fromisoformat(value['at'].replace('Z', '+00:00'))
    except ValueError as error: raise ValueError('Invalid archive activation date') from error


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', value):
        raise ValueError('State archive requires an official repository identity')


def get(value, path, default=MISSING):
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def parent(value, path):
    target = get(value, path[:-1])
    if not isinstance(target, dict):
        raise ValueError('Archive record has no original object parent')
    return target


def record_path(value):
    return (isinstance(value, list) and len(value) >= 2 and tuple(value[:-1]) in MAPS
            and isinstance(value[-1], str) and bool(re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', value[-1])))


def validate_archive(value, repo, source):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'repository', 'source_state_sha', 'records', 'prefixes'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value['repository'] != repo or value['source_state_sha'] != source
            or not isinstance(value['records'], list) or not isinstance(value['prefixes'], list)):
        raise ValueError('Archive format, repository or source snapshot differs')
    seen = set()
    for row in value['records']:
        if (not isinstance(row, dict) or set(row) != {'path', 'value'} or not record_path(row['path'])
                or tuple(row['path']) in seen or not isinstance(row['value'], dict)):
            raise ValueError('Invalid, duplicate or overlapping atomic archive record')
        seen.add(tuple(row['path']))
    seen = set()
    for row in value['prefixes']:
        if (not isinstance(row, dict) or set(row) != {'path', 'items'} or not isinstance(row['path'], list)
                or tuple(row['path']) not in LISTS or tuple(row['path']) in seen
                or not isinstance(row['items'], list) or not row['items']):
            raise ValueError('Invalid or duplicate archived history prefix')
        seen.add(tuple(row['path']))
    return value


def pointer(data, value):
    return {'path': '.tracker-archive/' + sha(data) + '.json', 'sha256': sha(data),
            'git_sha': git_sha(data), 'size': len(data), 'source_state_sha': value['source_state_sha']}


def archive_bytes(request, repo, selected):
    if (not isinstance(selected, dict) or set(selected) != {'path', 'sha256', 'git_sha', 'size', 'source_state_sha'}
            or not identity(selected['sha256'], 64) or not identity(selected['git_sha'])
            or not identity(selected['source_state_sha']) or type(selected['size']) is not int
            or not 0 < selected['size'] <= MAX_ARCHIVE
            or selected['path'] != '.tracker-archive/' + selected['sha256'] + '.json'):
        raise ValueError('Invalid immutable state archive pointer')
    cached = CACHE / (selected['sha256'] + '.json')
    def verify(data):
        if (len(data) != selected['size'] or sha(data) != selected['sha256'] or git_sha(data) != selected['git_sha']):
            raise ValueError('State archive length or content identity differs')
        value = loads(data)
        return validate_archive(value, repo, selected['source_state_sha'])
    try:
        descriptor = os.open(cached, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            data = stream.read(MAX_ARCHIVE + 1)
        return verify(data)
    except (OSError, ValueError):
        pass  # A disposable cache is never authority; fetch the immutable blob.
    blob = request('repos/' + repo + '/git/blobs/' + selected['git_sha'])
    if (not isinstance(blob, dict) or blob.get('sha') != selected['git_sha'] or blob.get('encoding') != 'base64'
            or type(blob.get('size')) is not int or blob['size'] != selected['size']
            or not isinstance(blob.get('content'), str) or len(blob['content']) > MAX_ARCHIVE * 2):
        raise ValueError('State archive blob is unavailable, truncated or oversized')
    try:
        data = base64.b64decode(''.join(blob['content'].split()), validate=True)
    except ValueError as error:
        raise ValueError('Invalid state archive blob encoding') from error
    value = verify(data)
    temporary = None
    try:
        CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.archive-', dir=CACHE)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, cached)
    except OSError:
        pass  # Unavailable caching does not weaken or prevent a verified read.
    finally:
        if temporary:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
    return value


def hydrate(wire, repo, snapshot_sha, request):
    repository(repo)
    if not isinstance(wire, dict):
        raise ValueError('State archive needs an object state')
    context = {'snapshot_sha': snapshot_sha, 'wire': copy.deepcopy(wire), 'archive': None}
    if wire.get('schema_version') != WIRE:
        return copy.deepcopy(wire), context
    if not identity(snapshot_sha):
        raise ValueError('State archive needs the exact source Git blob')
    if (type(wire['schema_version']) is not int
            or set(wire) != {'schema_version', 'repository', 'activation', 'archive', 'state', 'overrides', 'deleted'}
            or wire['repository'] != repo or not isinstance(wire['activation'], dict)
            or not isinstance(wire['state'], dict) or wire['state'].get('schema_version') == WIRE):
        raise ValueError('Invalid state archive envelope')
    validate_activation(wire['activation'])
    archive = archive_bytes(request, repo, wire['archive'])
    result = reconstruct(wire, archive)
    context['archive'] = archive
    return result, context


def reconstruct(wire, archive):
    """Restore atomic records and history without a semantic partial-state view."""
    known = {tuple(row['path']) for row in archive['records']}
    overlays = []
    for field in ('overrides', 'deleted'):
        values = wire[field]
        if (not isinstance(values, list) or any(not record_path(path) for path in values)
                or len({tuple(path) for path in values}) != len(values)
                or not {tuple(path) for path in values} <= known):
            raise ValueError('Unknown or duplicate archive overlay path')
        overlays.append({tuple(path) for path in values})
    overrides, deleted = overlays
    if overrides & deleted:
        raise ValueError('Archive overrides and deletions overlap')
    result = copy.deepcopy(wire['state'])
    for row in archive['records']:
        path = row['path']; key = tuple(path); target = parent(result, path)
        present = path[-1] in target
        if key in overrides:
            if not present or not isinstance(target[path[-1]], dict):
                raise ValueError('Archive override needs its complete live record')
        elif present:
            raise ValueError('Unmarked archive/live record collision')
        elif key not in deleted:
            target[path[-1]] = copy.deepcopy(row['value'])
    for row in archive['prefixes']:
        tail = get(result, row['path'])
        if not isinstance(tail, list):
            raise ValueError('Archived history requires its explicit live tail')
        parent(result, row['path'])[row['path'][-1]] = copy.deepcopy(row['items']) + tail
    return result


def references(value):
    """Only explicit current ownership, never the entire historical journal."""
    issues, prs, captures = set(), set(), set()
    def visit(row):
        if isinstance(row, list):
            for child in row: visit(child)
        elif isinstance(row, dict):
            if type(row.get('issue')) is int: issues.add(str(row['issue']))
            numbers = row.get('issues')
            if isinstance(numbers, list): issues.update(str(n) for n in numbers if type(n) is int)
            if row.get('kind') == 'task' and type(row.get('id')) is int: issues.add(str(row['id']))
            if row.get('kind') == 'capture' and isinstance(row.get('id'), str): captures.add(row['id'])
            for key in ('pr', 'source_pr', 'promotion_pr', 'main_pr'):
                if type(row.get(key)) is int: prs.add(str(row[key]))
            if isinstance(row.get('main_prs'), list):
                prs.update(str(n) for n in row['main_prs'] if type(n) is int)
            for key, child in row.items():
                if key not in ('history', 'policy_reconciliations') and isinstance(child, (dict, list)): visit(child)
    visit(value)
    return issues, prs, captures


def pending(row, state):
    if not (row.get('state') == 'merged' or row.get('kind') == 'direct' and row.get('state') == 'completed'):
        return False
    auth = row.get('delivery_request') or {}
    if auth.get('state') != 'requested': return False
    numbers = row.get('issues') or ([row.get('subject', {}).get('id')] if row.get('subject', {}).get('kind') == 'task' else [])
    expected = set(row.get('main_prs') or [row.get('finalization', {}).get('main_pr')]) - {None}
    if not numbers or not expected: return True
    for number in numbers:
        receipt = get(state, ('delivery_workflow', 'tasks', str(number)), {})
        if not any(r.get('state') == 'verified' and expected <= {s.get('pr') for s in r.get('sources', [])}
                   for r in [receipt, *receipt.get('history', [])]):
            return True
    return False


def extract(state, repo, source, tail=32, completion=None):
    """Archive closed records only; all existing semantics run after hydration."""
    live = copy.deepcopy(state); execution = state.get('execution') or {}
    subjects = [state.get('active'), state.get('run'), state.get('release_rollover'),
                execution.get('claims'), execution.get('review'), execution.get('source_reviews'),
                [({'issue': n} if type(n) is int else n) for n in execution.get('review_queue', [])],
                execution.get('integration')]
    if (execution.get('intake') or {}).get('state') == 'running': subjects.append(execution['intake'])
    stage = get(state, ('delivery_workflow', 'stage'), {})
    if stage.get('state') != 'promoted': subjects.append(stage)
    subjects.extend(row for row in get(state, ('task_pause', 'records'), {}).values() if row.get('state') == 'paused')
    for key, row in execution.get('tasks', {}).items():
        if row.get('state') not in ('merged', 'archived'):
            subjects.append(row); subjects.append({'issue': int(key)} if key.isdigit() else row.get('subject'))
    for key, row in get(state, ('delivery_workflow', 'tasks'), {}).items():
        terminal = get(state, ('execution', 'tasks', key), {}).get('state') == 'archived'
        if row.get('state') != 'verified' and not terminal and completion != 'reviewed-merge':
            subjects.extend([row, {'issue': int(key)} if key.isdigit() else None])
    subjects.extend(row for row in execution.get('batches', {}).values() if pending(row, state))
    subjects.extend(row for row in execution.get('completed', {}).values() if pending(row, state))
    issues, prs, captures = references(subjects)
    records = []
    for path in MAPS:
        rows = get(state, path, {})
        if not isinstance(rows, dict): raise ValueError('State archive record collection must be an object')
        for key, row in rows.items():
            if not isinstance(row, dict): raise ValueError('State archive records must remain objects')
            own = {name: row[name] for name in ('issue', 'issues', 'subject', 'pr', 'main_prs', 'source_pr', 'promotion_pr') if name in row}
            row_issues, row_prs, row_captures = references(own)
            eligible = not (row_issues & issues or row_prs & prs or row_captures & captures)
            if path in (('execution', 'tasks'), ('execution', 'approvals'), ('delivery_workflow', 'tasks')):
                number = str(row.get('subject', {}).get('id', key)).removeprefix('task:')
                # Cold records may mention live work in old shared delivery
                # manifests. Their own identity determines physical retention;
                # the full record is always restored before semantic lookup.
                eligible = number not in issues
                terminal = get(state, ('execution', 'tasks', key), {}).get('state') in ('merged', 'archived')
                if path == ('execution', 'tasks'): eligible = eligible and row.get('state') in ('merged', 'archived')
                elif path == ('delivery_workflow', 'tasks'):
                    eligible = eligible and (row.get('state') == 'verified' or completion == 'reviewed-merge'
                        or get(state, ('execution', 'tasks', key), {}).get('state') == 'archived')
                else: eligible = eligible and terminal
            elif path in (('execution', 'batches'), ('execution', 'completed')):
                eligible = eligible and (path == ('execution', 'completed') or
                    row.get('state') in ('merged', 'completed', 'cancelled', 'cancelled-before-merge')) and not pending(row, state)
            elif path == ('execution', 'captures', 'operations'):
                eligible = eligible and key not in captures and row.get('state') in ('completed', 'cancelled-before-merge')
            else:
                eligible = eligible and key not in prs
            if eligible:
                record = list(path) + [key]
                if not record_path(record): raise ValueError('Unsupported native archive record key')
                records.append({'path': record, 'value': copy.deepcopy(row)})
                del get(live, path)[key]
    prefixes = []
    for path in LISTS:
        values = get(state, path)
        if values is MISSING: continue
        if not isinstance(values, list): raise ValueError('State archive history must be a list')
        cut = max(0, len(values) - tail)
        if cut:
            prefixes.append({'path': list(path), 'items': copy.deepcopy(values[:cut])})
            parent(live, path)[path[-1]] = copy.deepcopy(values[cut:])
    archive = {'schema_version': 1, 'repository': repo, 'source_state_sha': source,
               'records': records, 'prefixes': prefixes}
    validate_archive(archive, repo, source)
    return live, archive


def envelope(state, repo, selected, activation, overrides=(), deleted=()):
    return {'schema_version': WIRE, 'repository': repo, 'archive': selected, 'activation': copy.deepcopy(activation),
            'state': state, 'overrides': sorted(overrides), 'deleted': sorted(deleted)}


def plan(state, repo, expected_sha, context=None, activation=None, completion=None):
    repository(repo)
    if context is not None and context['snapshot_sha'] != expected_sha:
        raise ValueError('Archive codec context belongs to a different state snapshot')
    if context is None or context['archive'] is None:
        if activation is None: return {'wire': state, 'archive_bytes': None}
    else:
        archive = context['archive']; partial = copy.deepcopy(state); overrides, deleted = [], []
        for row in archive['records']:
            value = get(partial, row['path'])
            if value is MISSING:
                parent(partial, row['path'])
                deleted.append(row['path'])
            elif raw(value) == raw(row['value']): del parent(partial, row['path'])[row['path'][-1]]
            else: overrides.append(row['path'])
        for row in archive['prefixes']:
            values = get(partial, row['path']); prefix = row['items']
            original = prefix + get(context['wire']['state'], row['path'])
            if not isinstance(values, list) or raw(values[:len(original)]) != raw(original):
                raise ValueError('Archived history is append-only; its retained prefix changed')
            parent(partial, row['path'])[row['path'][-1]] = copy.deepcopy(values[len(prefix):])
        wire = envelope(partial, repo, context['wire']['archive'], context['wire']['activation'], overrides, deleted)
        if raw(reconstruct(wire, archive)) != raw(state):
            raise ValueError('Archive overlays do not preserve the complete logical state')
        if len(raw(wire)) <= ROTATE: return {'wire': wire, 'archive_bytes': None}
        activation = context['wire']['activation']
    if not identity(expected_sha) or not isinstance(activation, dict) or not activation:
        raise ValueError('Archive activation needs exact owned snapshot evidence')
    validate_activation(activation)
    for tail in (32, 16, 8, 4, 1, 0):
        live, archive = extract(state, repo, expected_sha, tail, completion)
        data = raw(archive)
        if len(data) > MAX_ARCHIVE: raise ValueError('Complete state archive exceeds the 48 MiB Git blob bound')
        wire = envelope(live, repo, pointer(data, archive), activation)
        if len(raw(wire)) <= ROTATE: break
    if len(raw(wire)) > LIMIT:
        raise ValueError('Non-archivable active state exceeds 1 MiB; no records were discarded')
    return {'wire': wire, 'archive_bytes': data}


def write(request, repo, state, expected_sha, context=None, activation=None,
          message='Update tracker execution state [skip ci]', completion=None, legacy_indent=None):
    if expected_sha is not None and context is None:
        raise ValueError('Writing existing state requires its actual snapshot codec context')
    selected = plan(state, repo, expected_sha, context, activation, completion)
    # Preserve incumbent writer bytes and record key order until explicit
    # activation. Canonical sorting belongs to archive envelopes and hashes.
    data = (raw(selected['wire']) if selected['wire'].get('schema_version') == WIRE else
            json.dumps(selected['wire'], indent=legacy_indent, allow_nan=False,
                       **({'separators': (',', ':')} if legacy_indent is None else {})).encode())
    root = 'repos/' + repo
    if selected['archive_bytes'] is None:
        payload = {'message': message, 'branch': 'tracker-state', 'content': base64.b64encode(data).decode()}
        if expected_sha is not None: payload['sha'] = expected_sha
        return request(root + '/contents/.tracker-state.json', 'PUT', payload)
    # The live blob and archive enter authority together, on the exact fresh
    # branch tree. Non-forced advancement preserves concurrent neighboring files.
    blobs = []
    for content in (selected['archive_bytes'], data):
        result = request(root + '/git/blobs', 'POST', {'encoding': 'base64', 'content': base64.b64encode(content).decode()})
        if result.get('sha') != git_sha(content): raise ValueError('Git did not retain the exact archive/live blob')
        blobs.append(result['sha'])
    for attempt in range(3):
        ref = request(root + '/git/ref/heads/tracker-state')['object']
        if ref.get('type') != 'commit' or not identity(ref.get('sha')): raise ValueError('Invalid native state branch head')
        head = ref['sha']
        row = request(root + '/contents/.tracker-state.json?ref=' + head)
        if row.get('sha') != expected_sha: raise ArchiveConflict('Tracker state changed before archive transaction')
        tree = request(root + '/git/commits/' + head)['tree']['sha']
        if not identity(tree): raise ValueError('Invalid original state branch tree')
        created = request(root + '/git/trees', 'POST', {'base_tree': tree, 'tree': [
            {'path': selected['wire']['archive']['path'], 'mode': '100644', 'type': 'blob', 'sha': blobs[0]},
            {'path': '.tracker-state.json', 'mode': '100644', 'type': 'blob', 'sha': blobs[1]}]})
        if not identity(created.get('sha')): raise ValueError('Invalid archive transaction tree')
        commit = request(root + '/git/commits', 'POST', {'message': message, 'tree': created['sha'], 'parents': [head]})
        if not identity(commit.get('sha')): raise ValueError('Invalid archive transaction commit')
        try:
            request(root + '/git/refs/heads/tracker-state', 'PATCH', {'sha': commit['sha'], 'force': False})
        except Exception as error:
            if getattr(error, 'status', None) not in (409, 422):
                raise  # An uncertain ref write may have committed; never replay it.
            if attempt == 2:
                raise ArchiveConflict('Tracker branch kept advancing; reload before retrying') from error
            # Only rebuild the physical transaction. The next exact state-blob
            # check must still match; semantic operations are never replayed.
            continue
        return {'content': {'sha': blobs[1]}, 'commit': {'sha': commit['sha']}}
