"""Canonical process-support completion; never an admission or release waiver.

The schema-1 instruction route is unchanged. This proof is considered only after
the incumbent route admits the actual main merge and an independent reviewer has
classified the whole task outcome. Public Master publication authenticates the
export closure; consumer metadata cannot authenticate itself.
"""
import ast
import hashlib
import json
import re
from urllib.parse import quote
try:
    from . import delivery_policy as rules, process_instructions as git_proof
except ImportError:
    import tracker_delivery_policy as rules
    import tracker_process_instructions as git_proof

MASTER = 'MojoTheApe/master-repo'
BASELINE = '.workflow/template-base.json'
LAYOUT = {'schema_version': 1, 'repository': 'docs/repository/',
          'system': 'docs/system/', 'runtime': 'docs/runtime/', 'tools': '.workflow/tools/'}
KIND = 'canonical-process-support'
VERIFICATION = 'isolated-process-verification'
RETENTION = 'artifact-retention-verification'
GOVERNANCE = 'acl-delivery-governance'


def governance_proof(config, before, after, before_tree, after_tree):
    """ACL's bounded delivery adapter; never a global local-code exemption."""
    if config['project']['repository'] != 'MojoTheApe/adopy-campaing-launcher':
        return None
    controls = {'.github/workflows/complete-delivery.yml', 'scripts/acl_complete_delivery.py',
                'scripts/acl_publish_release.py'}
    tests = {'tests/test_acl_complete_delivery.py', 'tests/test_publish_release.py',
             'tests/test_acl_ci_workflows.py', 'tests/test_acl_ci_chain.py'}
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    if (not set(changed) & controls or any(name not in controls | tests and not git_proof.instruction_path(name) for name in changed)
            or any(name not in before or name not in after for name in controls)
            or any(any(row and (row['type'] != 'blob' or row['mode'] != '100644')
                       for row in (before.get(name), after.get(name))) for name in set(changed) | controls)):
        return None
    # Every build, packaging, product, runtime, pin and policy entry is identical.
    # Changed control logic still requires genuine full-scope and indirect
    # import/build isolation review plus ordinary source/batch/main checks.
    content = dict(schema_version=1, kind=GOVERNANCE, base_tree_sha=before_tree, tree_sha=after_tree,
                   files=[dict(path=name, before=before.get(name), after=after.get(name)) for name in changed],
                   unchanged_inputs_sha256=digest({name: row for name, row in before.items()
                                                  if name not in controls | tests and not git_proof.instruction_path(name)}))
    return dict(content, proof_sha256=digest(content))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate process-support metadata key')
        result[key] = value
    return result


def document(text):
    def invalid_number(_):
        raise ValueError('Invalid JSON number')
    value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_number)
    if not isinstance(value, dict):
        raise ValueError('Process-support metadata must be a JSON object')
    return value


def read(request, root, entries, name):
    row = entries.get(name)
    if not row or row['mode'] != '100644' or row['type'] != 'blob':
        raise ValueError('Missing regular canonical support file: ' + name)
    return git_proof.blob(request, root, row['sha'])


def published(request, descriptor, require_capability=False):
    if 'workflow_provider' in descriptor:
        return published_provider(request, descriptor, require_capability)
    selected = descriptor.get('standard', {})
    if (not isinstance(selected, dict) or set(selected) != {'repository', 'revision', 'version'}
            or selected['repository'] != 'https://github.com/' + MASTER
            or not isinstance(selected['revision'], str) or not rules.SHA.fullmatch(selected['revision'])
            or not isinstance(selected['version'], str) or not re.fullmatch(r'\d+\.\d+\.\d+', selected['version'])):
        raise ValueError('Canonical support needs an exact official published standard')
    root = 'repos/' + MASTER
    release = request(root + '/releases/tags/v' + selected['version'])
    if (release.get('draft') is not False or release.get('prerelease') is not False
            or release.get('tag_name') != 'v' + selected['version'] or not release.get('published_at')
            or type(release.get('id')) is not int or release['id'] < 1):
        raise ValueError('Canonical support standard is not a published stable release')
    ref = request(root + '/git/ref/tags/v' + selected['version'])['object']
    for _ in range(4):
        if ref.get('type') != 'tag':
            break
        if not isinstance(ref.get('sha'), str) or not rules.SHA.fullmatch(ref['sha']):
            raise ValueError('Invalid immutable standard tag')
        ref = request(root + '/git/tags/' + ref['sha'])['object']
    if ref != {'type': 'commit', 'sha': selected['revision']}:
        # GitHub adds a URL to real objects; it is not part of their identity.
        if ref.get('type') != 'commit' or ref.get('sha') != selected['revision']:
            raise ValueError('Published standard tag does not identify the selected commit')
    tree = rules.tree(request, root, selected['revision'])
    files = git_proof.entries(request, root, tree)
    version = read(request, root, files, 'VERSION').strip()
    if version != selected['version']:
        raise ValueError('Published standard version differs from its immutable source')
    compat = document(read(request, root, files, 'compatibility.json'))
    raw = read(request, root, files, 'scripts/vendor/tracker-export-contract.json')
    contract = document(raw)
    engine = {'repository': 'MojoTheApe/task-tracker', 'revision': compat.get('tracker_revision')}
    if (compat.get('tracker_repository') != engine['repository'] or compat.get('export_contract') != 1
            or hashlib.sha256(raw.encode()).hexdigest() != compat.get('export_contract_sha256')
            or not isinstance(engine['revision'], str) or not rules.SHA.fullmatch(engine['revision'])
            or contract.get('format_version') != 1 or contract.get('engine') != engine
            or contract.get('runtime') != {'engine_min_python': '3.11', 'portable_min_python': '3.9'}):
        raise ValueError('Published standard does not authenticate the selected portable closure')
    capabilities, layouts = contract.get('contracts'), contract.get('layouts')
    if not isinstance(capabilities, dict) or not isinstance(layouts, dict):
        raise ValueError('Invalid published portable contract')
    if require_capability and capabilities.get('process_support') != [1]:
        return None
    expected = layouts.get('separated')
    if (not isinstance(expected, dict) or not expected or len(expected) > 64
            or any(not re.fullmatch(r'(?:scripts/|\.workflow/tools/)(?:task_tracker|tracker_[a-z_]+)\.py', name)
                   or not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                   for name, value in expected.items())):
        raise ValueError('Unsupported published process helper closure')
    return dict(standard=selected, engine=engine, contract=contract, contract_text=raw,
                helpers=expected, release=release['id'])


def provider_export(request, root, files, revision):
    """Reconstruct the fixed published exporter as data; never execute upstream code."""
    module = ast.parse(read(request, root, files, 'tracker/export_contract.py'))
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    algorithms = {
        'digest': '7e2b61bdb800307ce234842ec7d11f79d199671c8e9c3ff5ef97957790c0a087',
        'forwarder': '5eaa07b6667582696b17e8523fe478bc05d82fc7f5cab098a35e2c5e40d1499d',
        'helper_files': 'c73734f6976a7b6ba1fe1621c5be80df6e153f0063eff0fd844ba210309ef815',
        'manifest_bytes': '67bff6cd27d77d58a818ee4b9fb5b64166ade570b256110e7dd3359b19c54ea9',
    }
    for name, expected in algorithms.items():
        if name not in functions or hashlib.sha256(ast.dump(functions[name], include_attributes=False).encode()).hexdigest() != expected:
            raise ValueError('Unsupported official provider export algorithm: ' + name)
    constants = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ('FORMAT', 'REPOSITORY', 'HELPERS'):
                if name in constants:
                    raise ValueError('Duplicate provider export declaration')
                constants[name] = ast.literal_eval(node.value)
    helpers = constants.get('HELPERS')
    if (type(constants.get('FORMAT')) is not int or constants['FORMAT'] != 1 or constants.get('REPOSITORY') != 'MojoTheApe/task-tracker'
            or not isinstance(helpers, dict) or not 1 <= len(helpers) <= 64
            or any(not re.fullmatch(r'(?:task_tracker|tracker_[a-z_]+)\.py', str(name))
                   or not git_proof.safe_path(path) or not path.endswith('.py') for name, path in helpers.items())):
        raise ValueError('Unknown published provider helper inventory')
    layouts = {'raw': {}, 'separated': {}}
    for name, source in helpers.items():
        raw = read(request, root, files, 'tracker/' + source)
        layouts['raw']['scripts/' + name] = hashlib.sha256(raw.encode()).hexdigest()
        if name in ('task_tracker.py', 'tracker_pr_check.py'):
            old = 'Path(__file__).resolve().parents[1]'
            if raw.count(old) != 1:
                raise ValueError('Unsupported provider helper root adaptation')
            raw = raw.replace(old, 'Path(__file__).resolve().parents[2]')
        forwarder = ('#!/usr/bin/env python3\n'
            '"""Compatibility entry point; shared implementation is in .workflow/tools."""\n'
            'from pathlib import Path\nimport runpy\nimport sys\n'
            "_tools = Path(__file__).resolve().parents[1] / '.workflow/tools'\n"
            'sys.path.insert(0, str(_tools))\n'
            "globals().update(runpy.run_path(str(_tools / '" + name + "'), run_name=__name__))\n")
        layouts['separated']['.workflow/tools/' + name] = hashlib.sha256(raw.encode()).hexdigest()
        layouts['separated']['scripts/' + name] = hashlib.sha256(forwarder.encode()).hexdigest()
    function = functions.get('manifest')
    if not function or len(function.body) != 2 or not isinstance(function.body[-1], ast.Return) or not isinstance(function.body[-1].value, ast.Dict):
        raise ValueError('Unknown provider manifest producer')
    values = function.body[-1].value
    fields = {ast.literal_eval(k): v for k, v in zip(values.keys, values.values)}
    expected = {'format_version', 'engine', 'runtime', 'contracts', 'ci_gate', 'ci_reuse_contexts',
                'validator', 'retry_validator', 'route_validator', 'layouts'}
    if set(fields) != expected or len(values.keys) != len(expected):
        raise ValueError('Unknown provider manifest fields')
    dynamic = dict(format_version='FORMAT', engine="{'repository': REPOSITORY, 'revision': revision}",
                   layouts="{layout: {name: digest(raw) for name, raw in helper_files(layout).items()} for layout in ('raw', 'separated')}")
    if any(ast.dump(fields[key]) != ast.dump(ast.parse(text, mode='eval').body) for key, text in dynamic.items()):
        raise ValueError('Unknown provider manifest generation')
    contract = {key: ast.literal_eval(fields[key]) for key in ('runtime', 'contracts', 'ci_gate', 'ci_reuse_contexts')}
    contract.update(format_version=1, engine=dict(repository=constants['REPOSITORY'], revision=revision), layouts=layouts)
    for key in ('validator', 'retry_validator', 'route_validator'):
        value = fields[key]
        if not isinstance(value, ast.Dict):
            raise ValueError('Unknown provider validator declaration')
        parts = {ast.literal_eval(k): v for k, v in zip(value.keys, value.values)}
        path = ast.literal_eval(parts['source'])
        if set(parts) != {'source', 'sha256'} or len(value.keys) != 2 or not git_proof.safe_path(path) or not path.startswith('tracker/'):
            raise ValueError('Unknown provider validator')
        expression = ast.parse('digest((SOURCE / ' + repr(path.removeprefix('tracker/')) + ').read_bytes())', mode='eval').body
        if ast.dump(parts['sha256']) != ast.dump(expression):
            raise ValueError('Unknown provider validator digest')
        contract[key] = dict(source=path, sha256=hashlib.sha256(read(request, root, files, path).encode()).hexdigest())
    if contract['runtime'] != {'engine_min_python': '3.11', 'portable_min_python': '3.9'}:
        raise ValueError('Unknown provider runtime')
    return contract, json.dumps(contract, sort_keys=True, indent=2) + '\n'


def published_provider(request, descriptor, require_capability=False):
    selected = descriptor['workflow_provider']
    if (not isinstance(selected, dict) or set(selected) != {'schema_version', 'repository', 'revision'}
            or type(selected['schema_version']) is not int or selected['schema_version'] != 1
            or selected['repository'] != 'https://github.com/MojoTheApe/task-tracker'
            or not rules.SHA.fullmatch(str(selected['revision']))):
        raise ValueError('Use the exact official standalone provider')
    root = 'repos/MojoTheApe/task-tracker'; revision = selected['revision']
    files = git_proof.entries(request, root, rules.tree(request, root, revision))
    versions = re.findall(r'(?m)^version = "(\d+\.\d+\.\d+)"$', read(request, root, files, 'pyproject.toml'))
    if len(versions) != 1:
        raise ValueError('Unknown provider version')
    tag = 'v' + versions[0]
    release = request(root + '/releases/tags/' + tag)
    if (release.get('draft') is not False or release.get('prerelease') is not False or release.get('tag_name') != tag
            or not release.get('published_at') or type(release.get('id')) is not int or release['id'] < 1):
        raise ValueError('Provider is not a published stable release')
    ref = request(root + '/git/ref/tags/' + tag)['object']
    for _ in range(4):
        if ref.get('type') != 'tag': break
        ref = request(root + '/git/tags/' + ref['sha'])['object']
    if ref.get('type') != 'commit' or ref.get('sha') != revision:
        raise ValueError('Published provider tag differs from its selected commit')
    reference = document(read(request, root, files, 'tracker/provider/REFERENCE.json'))
    standard = {key: reference[key] for key in ('repository', 'revision', 'version')}
    standard['repository'] = 'https://github.com/' + standard['repository']
    if descriptor.get('standard') != standard:
        raise ValueError('Retain the official provider standard ancestry')
    contract, raw = provider_export(request, root, files, revision)
    if require_capability and contract['contracts'].get('process_support') != [1]: return None
    return dict(standard=standard, engine=contract['engine'], contract=contract, contract_text=raw,
                helpers=contract['layouts']['separated'], release=release['id'], provider=selected)


def descriptor_without_pins(value):
    result = document(json.dumps(value))
    standard = result.get('standard', {})
    tracker = result.get('tracker', {})
    if not isinstance(standard, dict) or not isinstance(tracker, dict):
        raise ValueError('Invalid shared support descriptor')
    standard.pop('revision', None)
    standard.pop('version', None)
    tracker.pop('revision', None)
    if isinstance(result.get('workflow_provider'), dict):
        result['workflow_provider'].pop('revision', None)
    return result


def pin_workflow(before, after, old, new):
    pattern = r'(?m)^(\s*-?\s*uses:\s*[\"\x27]?MojoTheApe/master-repo@)' + re.escape(old) + r'(?=[\s\"\x27#]|$)'
    replaced, count = re.subn(pattern, lambda m: m[1] + new, before)
    return count == 1 and replaced == after


def instructions(name, text):
    if git_proof.instruction_path(name) or name in ('tracker/AGENT_WORKFLOW.md', 'tracker/task-template.md'):
        return True
    match = re.fullmatch(r'docs/([A-Z_]+\.md)', name)
    return bool(match and text == '# Moved\n\nSee [' + match[1] + '](repository/' + match[1] + ').\n')


def classify(request, config, pr):
    """Content proof only. It grants no ownership, admission or task completion."""
    root = 'repos/' + config['project']['repository']
    if any((pr[side].get('repo') or {}).get('full_name', '').lower() != config['project']['repository'].lower()
           for side in ('base', 'head')):
        return None
    cache = {}
    source = request
    def request(path):
        if path not in cache:
            cache[path] = source(path)
        return cache[path]
    base = rules.merge_base(request, root, pr) if pr.get('merged') else pr['base']['sha']
    head = pr['head']['sha']
    before_tree, after_tree = rules.tree(request, root, base), rules.tree(request, root, head)
    before = git_proof.entries(request, root, before_tree)
    after = git_proof.entries(request, root, after_tree)
    governance = governance_proof(config, before, after, before_tree, after_tree)
    if governance:
        return governance
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    # Avoid upstream lookups for ordinary application work or legacy repositories.
    bridge = {'delivery.json', 'tracker/engine.json', 'tracker/export-contract.json', BASELINE}
    if not changed or not set(changed) & bridge or any(name not in before or name not in after
            for name in ('delivery.json', 'tracker/config.json', BASELINE, '.workflow/layout.json')):
        return None
    possible = bridge | {'.github/workflows/delivery-check.yml'}
    if any(not (name in possible or git_proof.instruction_path(name)
                or name.startswith(('scripts/tracker_', '.workflow/tools/tracker_'))
                or name in ('scripts/task_tracker.py', '.workflow/tools/task_tracker.py',
                            'tracker/AGENT_WORKFLOW.md', 'tracker/task-template.md')
                or re.fullmatch(r'docs/[A-Z_]+\.md', name)) for name in changed):
        return None
    for name in changed:
        if any(row and (row['type'] != 'blob' or row['mode'] != '100644')
               for row in (before.get(name), after.get(name))):
            raise ValueError('Process support requires regular immutable text files')
    old_descriptor = document(read(request, root, before, 'delivery.json'))
    new_descriptor = document(read(request, root, after, 'delivery.json'))
    if digest(descriptor_without_pins(old_descriptor)) != digest(descriptor_without_pins(new_descriptor)):
        return None
    if (before['tracker/config.json'] != after['tracker/config.json']
            or digest(document(read(request, root, before, 'tracker/config.json'))) != digest(config)
            or digest(document(read(request, root, before, '.workflow/layout.json'))) != digest(LAYOUT)
            or before['.workflow/layout.json'] != after['.workflow/layout.json']):
        return None
    new = published(request, new_descriptor, require_capability=True)
    if not new:
        return None
    old = published(request, old_descriptor)
    for rows, selected, descriptor in ((before, old, old_descriptor), (after, new, new_descriptor)):
        if (document(read(request, root, rows, 'tracker/engine.json')) != selected['engine']
                or descriptor.get('tracker', {}).get('revision') != selected['engine']['revision']
                or read(request, root, rows, 'tracker/export-contract.json') != selected['contract_text']):
            raise ValueError('Consumer support pins/manifest differ from the official published pair')
        for name, expected in selected['helpers'].items():
            if hashlib.sha256(read(request, root, rows, name).encode()).hexdigest() != expected:
                raise ValueError('Consumer helper differs from its published canonical bytes: ' + name)
    old_base = document(read(request, root, before, BASELINE))
    new_base = document(read(request, root, after, BASELINE))
    for baseline, selected in ((old_base, old), (new_base, new)):
        if (set(baseline) != {'schema_version', 'standard_revision', 'inputs', 'files'}
                or type(baseline['schema_version']) is not int or baseline['schema_version'] != 1
                or baseline['standard_revision'] != selected['standard']['revision']
                or not isinstance(baseline['inputs'], dict) or not isinstance(baseline['files'], dict)
                or any(not git_proof.safe_path(name) or not isinstance(text, str)
                       for name, text in baseline['files'].items())):
            raise ValueError('Unknown or inconsistent immutable template baseline')
        for name, expected in selected['helpers'].items():
            text = baseline['files'].get(name)
            if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != expected:
                raise ValueError('Template helper ancestry differs from the published canonical closure')
        if selected.get('provider') and baseline['inputs'].get('workflow_provider') != selected['provider']:
            raise ValueError('Template inputs differ from the published provider identity')
    def inputs(value):
        result = document(json.dumps(value))
        if isinstance(result.get('workflow_provider'), dict): result['workflow_provider'].pop('revision', None)
        return result
    if digest(inputs(old_base['inputs'])) != digest(inputs(new_base['inputs'])):
        return None
    def allowed(name, old_text, new_text):
        if name in new['helpers'] or name in old['helpers']:
            return ((old_text is None or name in old['helpers'] and hashlib.sha256(old_text.encode()).hexdigest() == old['helpers'][name])
                    and (new_text is None or name in new['helpers'] and hashlib.sha256(new_text.encode()).hexdigest() == new['helpers'][name]))
        if name == 'tracker/engine.json':
            return old_text is not None and new_text is not None and document(old_text) == old['engine'] and document(new_text) == new['engine']
        if name == 'tracker/export-contract.json':
            return old_text == old['contract_text'] and new_text == new['contract_text']
        if name == 'delivery.json':
            if old_text is None or new_text is None:
                return False
            a, b = document(old_text), document(new_text)
            return (digest(descriptor_without_pins(a)) == digest(descriptor_without_pins(b))
                    and a.get('standard') == old['standard'] and b.get('standard') == new['standard']
                    and a.get('tracker', {}).get('revision') == old['engine']['revision']
                    and b.get('tracker', {}).get('revision') == new['engine']['revision'])
        if name == '.github/workflows/delivery-check.yml':
            if old.get('provider') and new.get('provider'):
                a, b = old['provider']['revision'], new['provider']['revision']
                return old_text is not None and new_text is not None and old_text.count(a) == 1 and old_text.replace(a, b) == new_text
            return old_text is not None and new_text is not None and pin_workflow(old_text, new_text, old['standard']['revision'], new['standard']['revision'])
        return all(text is None or instructions(name, text) for text in (old_text, new_text))
    for name in set(old_base['files']) | set(new_base['files']):
        a, b = old_base['files'].get(name), new_base['files'].get(name)
        if a != b and not allowed(name, a, b):
            return None
    manifest = []
    for name in changed:
        a = read(request, root, before, name) if name in before else None
        b = read(request, root, after, name) if name in after else None
        if name != BASELINE and not allowed(name, a, b):
            return None
        manifest.append(dict(path=name, before=before.get(name), after=after.get(name)))
    content = dict(schema_version=1, kind=KIND, base_tree_sha=before_tree, tree_sha=after_tree,
                   standards=[old['standard'], new['standard']], engines=[old['engine'], new['engine']], files=manifest)
    return dict(content, proof_sha256=digest(content))


def assessment(proof, scopes):
    return dict(schema_version=1, kind=proof['kind'], outcome='process-only',
                proof_sha256=proof['proof_sha256'], issue_scopes=scopes)


def issue_snapshot(issue):
    return dict(number=issue['number'], title=issue.get('title'), body=issue.get('body'),
                labels=sorted(row['name'] for row in issue['labels']
                              if not row['name'].startswith(('status:', 'priority:', 'release:'))))


def checklist_recovery(previous, issue):
    """Fresh legacy review may attest completed ticks, never edited requirements."""
    old = previous.get('process_support_issues', {}).get(str(issue['number']))
    current = issue_snapshot(issue)
    if not old or {k: v for k, v in old.items() if k != 'body'} != {k: v for k, v in current.items() if k != 'body'}:
        return False
    before, after = (old.get('body') or '').splitlines(keepends=True), (current.get('body') or '').splitlines(keepends=True)
    if len(before) != len(after):
        return False
    fence = None
    for a, b in zip(before, after):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$', a)
        if marker:
            delimiter, tail = marker.groups()
            if fence is None:
                fence = (delimiter[0], len(delimiter))
            elif delimiter[0] == fence[0] and len(delimiter) >= fence[1] and not tail.strip():
                fence = None
        if a != b and (fence is not None or not re.match(r'^[-*+]\s+\[ \]\s+', a)
                       or b not in (a.replace('[ ]', '[x]', 1), a.replace('[ ]', '[X]', 1))):
            return False
    return before != after


def legacy_stage_recovery(request, config, state, pr, previous):
    """Retained verified Stage source already integrated unchanged into main."""
    stage = state.get('delivery_workflow', {}).get('stage', {})
    if (stage.get('pr') != pr['number'] or stage.get('state') not in ('passed', 'promoted')
            or stage.get('source_sha') != pr.get('merge_commit_sha')
            or stage.get('policy') != rules.fingerprint(config)
            or stage.get('tree_sha') != previous.get('tree_sha')
            or set(stage.get('issues', [])) != set(previous.get('issues', []))):
        return False
    root = 'repos/' + config['project']['repository']
    main = request(root + '/git/ref/heads/' + quote(config['project']['base_branch'], safe=''))['object']['sha']
    return (rules.tree(request, root, main) == stage['tree_sha']
            and request(root + '/compare/' + stage['source_sha'] + '...' + main).get('status') in ('ahead', 'identical'))


def valid_assessment(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected) | {'report', 'product_isolation'}:
        return False
    isolation = value['product_isolation']
    return (digest({k: value.get(k) for k in expected}) == digest(expected)
            and isinstance(value['report'], str) and bool(value['report'].strip()) and len(value['report']) <= 32768
            and isinstance(isolation, dict) and set(isolation) == {'result', 'evidence'}
            and isolation['result'] == 'isolated' and isinstance(isolation['evidence'], str)
            and bool(isolation['evidence'].strip()) and len(isolation['evidence']) <= 32768)


def assess_review(request, config, pr, scopes, document):
    """Validate an optional full-scope assessment by the genuine normal Reviewer."""
    value = document.get('process_support')
    if value is None:
        return None
    proof = classify(request, config, pr)
    if not proof or not scopes or len(scopes) != 1:
        raise ValueError('Process support needs one complete proven canonical upgrade and reviewed task scope')
    expected = assessment(proof, scopes)
    if not valid_assessment(value, expected):
        raise ValueError('Independently assess the exact support proof and full task scope, including publication requirements')
    return dict(value)


def review_for(state, pr, admission):
    section = state.get('delivery_workflow', {})
    number = pr['number']
    if admission.get('route') == 'promotion':
        stage = section.get('promotions', {}).get(str(number)) or section.get('stage', {})
        number = stage.get('pr')
    return section.get('reviews', {}).get(str(number), {})


def publication_requested(state, numbers, pr, admission):
    """A process receipt must never satisfy an actual product-delivery request."""
    section = state.get('execution') or {}
    rows = [section.get('integration') or {}, section.get('completed', {}).get(str(pr['number']), {})]
    if admission.get('batch_id'):
        rows.append(section.get('batches', {}).get(admission['batch_id'], {}))
    rows.extend(row for row in section.get('batches', {}).values()
                if pr['number'] in row.get('main_prs', []))
    for row in rows:
        request = row.get('delivery_request') or {}
        if request.get('state') != 'requested':
            continue
        linked = set(request.get('issues', row.get('issues', [])))
        subject = request.get('subject', row.get('subject', {})) or {}
        if subject.get('kind') == 'task':
            linked.add(subject['id'])
        if linked.intersection(numbers):
            return True
    return False


def completion(request, config, state, pr, admission, numbers, review=None):
    """Called only after full incumbent admission; never used by check-merge."""
    review = review if review is not None else review_for(state, pr, admission)
    if not review.get('process_support') or publication_requested(state, numbers, pr, admission):
        return None
    if not pr.get('merged') or len(numbers) != 1 or not admission or admission.get('route') == 'process':
        raise ValueError('Support completion requires the actual ordinarily admitted singleton main outcome')
    scopes = {str(n): rules.task_scope(request('repos/' + config['project']['repository'] + '/issues/' + str(n)), config)
              for n in numbers}
    if (set(review.get('issues', numbers)) != set(numbers) or review.get('issue_scopes') != scopes
            or review.get('verdict') != 'approved' or not review.get('reviewer')
            or review.get('reviewer') == review.get('implementer')):
        raise ValueError('Canonical process completion needs unchanged independently reviewed whole task scope')
    proof = classify(request, config, pr)
    if not proof or not valid_assessment(review['process_support'], assessment(proof, scopes)):
        raise ValueError('Reviewed support content differs from the actual complete main outcome')
    root = 'repos/' + config['project']['repository']
    if rules.pr_tree(request, root, pr) != proof['tree_sha']:
        raise ValueError('Actual support merge includes unreviewed content')
    return dict(schema_version=1, kind=proof['kind'], route='process-support', policy=rules.fingerprint(config),
                tree_sha=proof['tree_sha'], main_sha=rules.merge_base(request, root, pr),
                support_proof=proof, proof_sha256=proof['proof_sha256'], review_sha256=digest(review),
                admission=admission, issue_scopes=scopes, assessment=review['process_support'])


def preflight(request, config, state, pr):
    proof = classify(request, config, pr)
    if not proof:
        proof = verification_proof(request, config, pr)
    result = dict(content='proven' if proof else 'not-proven', product_delivery='unknown', applied=False)
    if not proof:
        return result
    from_numbers = re.findall(r'^Tracker issues:\s*(.+)$', pr.get('body') or '', re.M)
    # Reuse the real parser rather than treating a title/description as authority.
    try:
        from . import pr_checks
    except ImportError:
        import tracker_pr_check as pr_checks
    numbers = pr_checks.linked_numbers(pr.get('body'), config) if len(from_numbers) == 1 else []
    scopes = {str(n): rules.task_scope(request('repos/' + config['project']['repository'] + '/issues/' + str(n)), config)
              for n in numbers}
    result.update(proof=proof, review_assessment=assessment(proof, scopes),
                  required_assessments=['report', 'product_isolation.result=isolated', 'product_isolation.evidence'])
    if proof['kind'] == VERIFICATION:
        result['next'] = 'Supplemental independent assessment plus exact successful native workflow_dispatch; --evidence-file previews, --apply records only an already admitted merged singleton.'
        if len(numbers) == 1:
            receipt = state.get('delivery_workflow', {}).get('tasks', {}).get(str(numbers[0]), {})
            if (rules.summary(receipt).get('verified') and receipt.get('scope') == scopes[str(numbers[0])]
                    and receipt.get('verification', {}).get('proof_sha256') == proof['proof_sha256']):
                result['product_delivery'] = 'not-required'
        return result
    if len(numbers) != 1:
        return result
    direction = rules.route(config, pr, request)
    review = review_for(state, pr, {'route': direction})
    if publication_requested(state, numbers, pr, {'route': direction}):
        result['product_delivery'] = 'requested'
    elif (review.get('issue_scopes') == scopes and review.get('verdict') == 'approved'
          and review.get('tree_sha') == proof['tree_sha']
          and (direction == 'promotion' or review.get('head_sha') == pr['head']['sha'] and review.get('base_sha') == pr['base']['sha'])
          and review.get('reviewer') and review.get('reviewer') != review.get('implementer')
          and valid_assessment(review.get('process_support'), assessment(proof, scopes))):
        result['product_delivery'] = 'not-required'
    return result


def manual_workflow(text):
    """Conservative candidate grammar, NOT proof of semantic product isolation.

    This is deliberately not a YAML interpreter. Unsupported syntax is refused.
    Actual imports, scripts, credentials and indirect effects still require the
    separate exact-content independent assessment and a real native success.
    """
    if ('\t' in text or '\r' in text
            or re.search(r'^\s*(?:-\s*)?([\"\x27]?)<<\1\s*:', text, re.M)):
        return None
    blocks, current = {}, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if re.search(r'(?:^|\s)[&*!][A-Za-z_]', line) or re.match(r'^\s*-\s*[\[{?]', line):
            return None
        if not line.startswith(' '):
            match = re.fullmatch(r'(name|on|permissions|concurrency|jobs):(.*)', line)
            if not match or match[1] in blocks:
                return None
            current = match[1]
            blocks[current] = [line]
        elif current:
            blocks[current].append(line)
        else:
            return None
    if not {'on', 'permissions', 'jobs'} <= set(blocks):
        return None
    if (blocks['on'][0] != 'on:' or len(blocks['on']) < 2
            or not re.fullmatch(r'  workflow_dispatch:(?:\s*(?:\{\}|null))?', blocks['on'][1])
            or any(not line.startswith('    ') for line in blocks['on'][2:])
            or blocks['permissions'] != ['permissions:', '  contents: read']
            or blocks['jobs'][0] != 'jobs:'):
        return None
    jobs, selected, literal_indent = {}, None, None
    for line in blocks['jobs'][1:]:
        indent = len(line) - len(line.lstrip())
        if literal_indent is not None and indent > literal_indent:
            continue
        literal_indent = None
        # No quoted/escaped YAML mapping keys. Code inside an explicit scalar
        # is opaque, so ordinary Python string/dictionary keys remain code.
        if re.match(r'^\s*(?:-\s*)?(?:[\"\x27]|[?:](?:\s|$))', line):
            return None
        if line.startswith('  ') and not line.startswith('   '):
            match = re.fullmatch(r'  ([A-Za-z_][A-Za-z0-9_-]*):', line)
            if not match or match[1] in jobs:
                return None
            selected = match[1]; jobs[selected] = {}
        elif line.startswith('    ') and not line.startswith('     '):
            match = re.fullmatch(r'    (name|needs|runs-on|timeout-minutes|env|outputs|steps):(.*)', line)
            if not selected or not match or match[1] in jobs[selected]:
                return None
            jobs[selected][match[1]] = match[2].strip()
        elif not selected or not line.startswith('      '):
            return None
        # Permission overrides/merge keys and masked failures never qualify,
        # even if a successful overall run could hide a skipped/failed check.
        if re.match(r'^\s*(?:-\s*)?([\"\x27]?)(?:permissions|continue-on-error|environment)\1\s*:', line):
            return None
        scalar = re.match(r'^\s*(?:-\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_-]*):\s*[|>][+-]?\s*$', line)
        if scalar:
            literal_indent = scalar.start('key')
    names = []
    for key, job in jobs.items():
        if (not {'runs-on', 'steps'} <= set(job) or job['steps']
                or not re.fullmatch(r'(?:ubuntu|windows|macos)-(?:latest|[0-9]+(?:\.[0-9]+)?)', job['runs-on'])):
            return None
        name = job.get('name', key).strip('"\x27')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _()./-]{0,127}', name) or name in names:
            return None
        names.append(name)
    return sorted(names) if names else None


def verification_proof(request, config, pr):
    """Complete immutable candidate envelope; never a source-admission shortcut."""
    root = 'repos/' + config['project']['repository']
    if pr['base']['ref'] != config['project']['base_branch'] or any(
            (pr[side].get('repo') or {}).get('full_name', '').lower() != config['project']['repository'].lower()
            for side in ('base', 'head')):
        return None
    base = rules.merge_base(request, root, pr) if pr.get('merged') else pr['base']['sha']
    before_tree, tree = rules.tree(request, root, base), rules.tree(request, root, pr['head']['sha'])
    before, after = git_proof.entries(request, root, before_tree), git_proof.entries(request, root, tree)
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    workflows = [p for p in changed if re.fullmatch(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml', p)]
    if (len(workflows) != 1 or not changed or any(p != workflows[0] and not (
            git_proof.instruction_path(p) or re.fullmatch(r'tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_-]+\.py', p))
            for p in changed)):
        return None
    # The current selection must already exist unchanged in both immutable
    # trees. Neither a temporary config nor this candidate selects a new policy.
    if (before.get('tracker/config.json') != after.get('tracker/config.json')
            or document(read(request, root, before, 'tracker/config.json')) != config):
        return None
    manifest = []
    for path in changed:
        for rows in (before, after):
            if path in rows:
                read(request, root, rows, path)
        manifest.append(dict(path=path, before=before.get(path), after=after.get(path)))
    workflow = workflows[0]
    if workflow not in after or workflow in before:
        return None
    names = manual_workflow(read(request, root, after, workflow))
    if not names:
        return None
    if pr.get('merged') and rules.pr_tree(request, root, pr) != tree:
        raise ValueError('Actual verification merge contains unreviewed content')
    content = dict(schema_version=1, kind=VERIFICATION, base_tree_sha=before_tree, tree_sha=tree,
                   workflow=workflow, workflow_blob=after[workflow]['sha'], jobs=names, files=manifest)
    return dict(content, proof_sha256=digest(content))


def verification_requested(state, numbers, pr, admission):
    if publication_requested(state, numbers, pr, admission):
        return True
    # Completed/frozen history remains authoritative even after the live lane
    # was released. Never reinterpret a genuine requested publication as process.
    execution = state.get('execution') or {}
    rows = [entry.get('batch', {}) for entry in execution.get('history', [])]
    rows += [execution.get('integration') or {}]
    rows += list(execution.get('batches', {}).values()) + list(execution.get('completed', {}).values())
    rows += [state.get('delivery_workflow', {}).get('tasks', {}).get(str(n), {}) for n in numbers]
    for row in rows:
        request = row.get('delivery_request') or {}
        linked = set(row.get('issues', [])) | set(request.get('issues', []))
        subject = request.get('subject', row.get('subject', {})) or {}
        if subject.get('kind') == 'task':
            linked.add(subject['id'])
        if request and (not linked or linked.intersection(numbers)):
            return True
    return False


def verified_run(api, proof, source, supplied):
    if (not isinstance(supplied, dict) or set(supplied) != {'id', 'attempt'}
            or any(type(supplied[k]) is not int or supplied[k] < 1 for k in supplied)):
        raise ValueError('Identify the actual native workflow run and attempt')
    run = api.request(api.root + '/actions/runs/' + str(supplied['id']))
    workflow = api.request(api.root + '/actions/workflows/' + str(run.get('workflow_id')))
    if (run.get('id') != supplied['id'] or run.get('run_attempt') != supplied['attempt']
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('event') != 'workflow_dispatch' or run.get('path') != proof['workflow']
            or workflow.get('path') != proof['workflow'] or workflow.get('id') != run.get('workflow_id')
            or run.get('repository', {}).get('full_name') != api.repository
            or run.get('head_repository', {}).get('full_name') != api.repository
            or run.get('head_sha') != source['merge_sha']
            or rules.tree(api.request, api.root, run['head_sha']) != proof['tree_sha']):
        raise ValueError('Process verification requires the exact successful official manual workflow at its admitted main merge')
    jobs, total = [], None
    for page in range(1, 11):
        result = api.request(api.root + f'/actions/runs/{supplied["id"]}/attempts/{supplied["attempt"]}/jobs?per_page=100&page={page}')
        if (type(result.get('total_count')) is not int or not 1 <= result['total_count'] <= 1000
                or not isinstance(result.get('jobs'), list) or total is not None and total != result['total_count']):
            raise ValueError('Incomplete native process job history')
        total = result['total_count']; jobs.extend(result['jobs'])
        if len(jobs) >= total:
            break
        if not result['jobs']:
            raise ValueError('Incomplete native process job history')
    if (any(not isinstance(j, dict) or type(j.get('id')) is not int or j['id'] < 1
            or not isinstance(j.get('name'), str) for j in jobs)
            or len(jobs) != total or len({j.get('id') for j in jobs}) != total
            or sorted(j.get('name', '') for j in jobs) != proof['jobs']
            or any(j.get('status') != 'completed' or j.get('conclusion') != 'success'
                   or j.get('head_sha') != source['merge_sha'] for j in jobs)):
        raise ValueError('Every exact native process verification job must actually succeed')
    return dict(id=supplied['id'], attempt=supplied['attempt'], path=proof['workflow'],
                workflow_blob=proof['workflow_blob'], head_sha=source['merge_sha'],
                jobs=proof['jobs'], result='passed')


def record_verification(api, config, store, number, value, apply=False):
    """Supplemental delivery assessment; does not acquire or rewrite source review."""
    from . import export_contract
    # A newer source checkout cannot activate this capability for an older
    # consumer. Its real current-main selection must identify a published pair
    # authenticating these exact portable semantics before any state mutation.
    main = api.request(api.root + '/git/ref/heads/' + quote(config['project']['base_branch'], safe=''))['object']['sha']
    entries = git_proof.entries(api.request, api.root, rules.tree(api.request, api.root, main))
    selected = published(api.request, document(read(api.request, api.root, entries, 'delivery.json')))
    if (selected['contract']['contracts'].get('isolated_verification') != [1]
            or document(read(api.request, api.root, entries, 'tracker/config.json')) != config
            or document(read(api.request, api.root, entries, 'tracker/engine.json')) != selected['engine']
            or read(api.request, api.root, entries, 'tracker/export-contract.json') != selected['contract_text']
            or export_contract.manifest_bytes(selected['engine']['revision']).decode() != selected['contract_text']):
        raise ValueError('Adopt the exact published compatible isolated-verification support before recording process completion')
    return record_outcome(api, config, store, number, value, apply)


def record_outcome(api, config, store, number, value, apply=False, *,
                   proof_factory=verification_proof, run_factory=verified_run,
                   kind=VERIFICATION, evidence_kind='independent-process-verification',
                   current_check=None, authority=None):
    """Shared post-main guards, called only after the route's own authentication."""
    import copy
    from . import pr_checks
    from .sync import now
    state, sha = store.read(optional=True)
    rules.completion_state(state, config)
    checked = pr_checks.check(api.request, config, number, allow_transition=True, admission=True)
    numbers = checked['issues']; pr = api.request(api.root + f'/pulls/{number}')
    if (not pr.get('merged') or pr['base']['ref'] != config['project']['base_branch'] or len(numbers) != 1):
        raise ValueError('Process verification records only a genuinely admitted merged singleton')
    issue_number = numbers[0]; issue = api.issue(issue_number)
    rules.require_settled_corrections(state, issue_number)
    rules.pause_module().require_unpaused(state, issue_number)
    admission = checked['admission']
    if verification_requested(state, numbers, pr, admission):
        raise ValueError('A genuine product publication request retains its actual delivery obligation')
    row = state.get('delivery_workflow', {}).get('tasks', {}).get(str(issue_number), {})
    source = dict(pr=number, merge_sha=pr.get('merge_commit_sha'), tree_sha=rules.pr_tree(api.request, api.root, pr))
    if (row.get('policy') != rules.fingerprint(config) or row.get('sources') != [source]
            or not row.get('scope_provenance') or not rules.scope_matches(issue, row.get('scope'), config)
            or {x['name'] for x in issue['labels'] if x['name'].startswith('status:')} != {'status:merged'}):
        raise ValueError('Retain the exact admitted main receipt and unchanged originally accepted task scope')
    if api.request(api.root + '/compare/' + source['merge_sha'] + '...' + quote(config['project']['base_branch'], safe='')).get('status') not in ('ahead', 'identical'):
        raise ValueError('Process main merge is no longer included')
    original = review_for(state, pr, admission)
    scopes = {str(issue_number): row['scope']}
    if (original.get('verdict') != 'approved' or original.get('tree_sha') != source['tree_sha']
            or original.get('issue_scopes') != scopes or not original.get('report')
            or not original.get('reviewer') or original.get('reviewer') == original.get('implementer')):
        raise ValueError('Retain the genuine independent source/combined review for this whole outcome')
    from . import acceptance
    if acceptance.criteria(issue.get('body')):
        acceptance.verify(api, config, store, issue, [number])
    proof = proof_factory(api.request, config, pr)
    if not proof:
        raise ValueError('This is not a supported isolated manual verification candidate')
    if current_check is not None:
        current_check(api, config, proof)
    authors = {original.get('implementer')}
    task = (state.get('execution') or {}).get('tasks', {}).get(str(issue_number), {})
    authors.add(task.get('worker'))
    authors.update(w.get('actor') for w in task.get('implementers', []))
    if (not isinstance(value, dict) or set(value) != {'kind', 'reviewer', 'implementer', 'report', 'process_support', 'workflow_run'}
            or value.get('kind') != evidence_kind
            or not isinstance(value.get('reviewer'), str) or not value['reviewer'].strip()
            or len(value['reviewer']) > 256
            or value.get('implementer') != original.get('implementer') or value['reviewer'] in authors
            or not isinstance(value.get('report'), str) or not value['report'].strip() or len(value['report']) > 32768
            or not valid_assessment(value.get('process_support'), assessment(proof, scopes))):
        raise ValueError('An actual independent assessor must verify the exact immutable proof and whole original outcome')
    previous = row.get('verification', {})
    if (kind == RETENTION and row.get('process') and row.get('state') == 'verified'
            and rules.summary(row).get('verified') and previous.get('kind') == kind
            and previous.get('support_proof') == proof and previous.get('supplemental_review') == value):
        # A real verified receipt is durable after temporary artifacts expire.
        # Revalidate authority, accepted scope and current setting, then retain
        # the original evidence verbatim; never replace it with a new run.
        return dict(applied=False, already_recorded=True, issues=numbers, result='process-verified')
    run = run_factory(api, proof, source, value['workflow_run'])
    verification = dict(schema_version=1, kind=kind, route='process-support', policy=row['policy'],
        tree_sha=source['tree_sha'], main_sha=rules.merge_base(api.request, api.root, pr), support_proof=proof,
        proof_sha256=proof['proof_sha256'], review_sha256=digest(value), admission=admission, issue_scopes=scopes,
        assessment=value['process_support'], supplemental_review=copy.deepcopy(value), workflow_run=run)
    if authority is not None:
        verification['operator_authority'] = copy.deepcopy(authority)
    if row.get('process') and row.get('state') == 'verified' and row.get('verification') == verification:
        return dict(applied=False, already_recorded=True, issues=numbers, result='process-verified')
    if row.get('state') != 'pending' or row.get('verification') or row.get('process'):
        raise ValueError('Only the original pending process outcome can be completed; preserve other delivery results')
    if apply:
        if current_check is not None:
            current_check(api, config, proof)
        row.setdefault('history', []).append({k: copy.deepcopy(v) for k, v in row.items() if k != 'history'})
        row.update(state='verified', process=True, verification=verification, verified_at=now())
        store.write(state, sha)
    return dict(applied=apply, issues=numbers, result='process-verified')


def valid_retention_receipt(proof, sources):
    """Portable conservative reader; never executes the source-only operator."""
    canonical = proof.get('support_proof') or {}
    native = proof.get('workflow_run') or {}
    extra = proof.get('supplemental_review') or {}
    authority = proof.get('operator_authority') or {}
    if not all(isinstance(v, dict) for v in (canonical, native, extra, authority)) or len(sources) != 1:
        return False
    artifact = native.get('artifact') or {}
    upload = canonical.get('upload') or {}
    if not isinstance(artifact, dict) or not isinstance(upload, dict) or set(authority) != {'operator', 'selected_provider'}:
        return False
    for key in ('operator', 'selected_provider'):
        identity = authority[key]
        if (not isinstance(identity, dict) or identity.get('repository') != 'MojoTheApe/task-tracker'
                or not isinstance(identity.get('revision'), str) or not rules.SHA.fullmatch(identity['revision'])
                or not re.fullmatch(r'\d+\.\d+\.\d+', str(identity.get('version', '')))
                or type(identity.get('release')) is not int or identity['release'] < 1
                or not re.fullmatch(r'[a-f0-9]{64}', str(identity.get('export_sha256', '')))):
            return False
    if any(not re.fullmatch(r'[a-f0-9]{64}', str(authority['selected_provider'].get(k, '')))
           for k in ('descriptor_sha256', 'config_sha256')):
        return False
    from datetime import datetime
    try:
        created, expires = [datetime.strptime(artifact[k], '%Y-%m-%dT%H:%M:%SZ') for k in ('created_at', 'expires_at')]
        if (type(upload.get('days')) is not int or not 1 <= upload['days'] <= 90
                or abs((expires-created).total_seconds() - upload['days']*86400) > 60):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    origin = artifact.get('workflow_run')
    if (not isinstance(origin, dict) or origin.get('id') != native.get('id')
            or origin.get('head_sha') != native.get('head_sha') or origin.get('head_branch') != native.get('head_branch')
            or origin.get('repository_id') != native.get('repository_id')
            or origin.get('head_repository_id') != native.get('head_repository_id')):
        return False
    return (all(type(native.get(k)) is int and native[k] > 0
                for k in ('id', 'attempt', 'job_id', 'step_number', 'run_number', 'repository_id', 'head_repository_id'))
        and native.get('result') == 'passed' and native.get('head_sha') in (sources[0]['merge_sha'], canonical.get('head_sha'))
        and native.get('event') in ('pull_request', 'push')
        and re.fullmatch(r'[\w.-]+/[\w.-]+', str(native.get('repository', ''))) is not None
        and isinstance(canonical.get('head_sha'), str) and bool(rules.SHA.fullmatch(canonical['head_sha']))
        and native.get('path') == canonical.get('workflow') and native.get('workflow_blob') == canonical.get('workflow_blob')
        and isinstance(native.get('jobs'), list) and bool(native['jobs']) and native['jobs'] == canonical.get('jobs')
        and native.get('step_number') == upload.get('step_number')
        and native.get('tested_tree_sha') == canonical.get('tree_sha') == sources[0]['tree_sha']
        and all(re.fullmatch(r'[a-f0-9]{40}', str(native.get(k, ''))) is not None
                for k in ('checkout_sha', 'checkout_action_sha'))
        and re.fullmatch(r'[a-f0-9]{40}', str(native.get('action_sha', ''))) is not None
        and re.fullmatch(r'[a-f0-9]{64}', str(native.get('upload_log_sha256', ''))) is not None
        and type(artifact.get('id')) is int and artifact['id'] > 0
        and type(artifact.get('size_in_bytes')) is int and artifact['size_in_bytes'] > 0
        and isinstance(upload.get('artifact_name'), str)
        and artifact.get('name') == upload['artifact_name'].replace('${{ github.run_number }}', str(native.get('run_number')))
        and re.fullmatch(r'sha256:[a-f0-9]{64}', str(artifact.get('digest', ''))) is not None
        and all(re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', str(artifact.get(k, '')))
                for k in ('created_at', 'expires_at'))
        and extra.get('kind') == 'independent-artifact-retention'
        and isinstance(extra.get('reviewer'), str) and bool(extra['reviewer'].strip())
        and isinstance(extra.get('implementer'), str) and bool(extra['implementer'].strip())
        and extra['reviewer'] != extra['implementer']
        and extra.get('workflow_run') == {'id': native['id'], 'attempt': native['attempt'], 'artifact_id': artifact['id']}
        and extra.get('process_support') == proof.get('assessment') and digest(extra) == proof.get('review_sha256'))
