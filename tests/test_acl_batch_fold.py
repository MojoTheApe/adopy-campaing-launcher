"""Real Git folds and native selection proof preserve the complete frozen package."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts import acl_batch
from tests.test_acl_batch import fixture, publish_argv


def add_fragments(fixture):
    root, remote, section, prs, _ = fixture
    for member in section['integration']['members']:
        acl_batch.git(root, 'checkout', member['branch'])
        folder = root / 'docs/system/changelog.d'
        folder.mkdir(parents=True, exist_ok=True)
        number = 1000 + member['issue']
        (folder / f'ACL-{number}.md').write_text(f'## ACL-{number} — Source {number} — 2026-10-24\n\nComplete work {number}.\n')
        if member['issue'] == 1:
            (folder / 'README.md').write_text('# Fragment instructions\n')
            (root / 'docs/system/CHANGELOG.md').write_text('# System history\n\n## ACL-9 — Earlier — 2026-01-01\n\nRetained.\n')
        acl_batch.git(root, 'add', '.')
        acl_batch.git(root, 'commit', '-qm', 'Reviewed source fragment')
        member['head_sha'] = acl_batch.git(root, 'rev-parse', 'HEAD')
        prs[member['pr']]['head']['sha'] = member['head_sha']
        acl_batch.git(root, 'push', str(remote), member['branch'])
    acl_batch.git(root, 'checkout', 'main')


def test_local_reader_uses_complete_actual_objects_despite_replace_refs(fixture):
    root, _, section, _, _ = fixture
    batch = section['integration']
    baseline = batch['main_sha']
    original_tree = acl_batch.git(root, 'rev-parse', baseline + '^{tree}')
    acl_batch.git(root, 'replace', baseline, batch['members'][0]['head_sha'])
    reader = acl_batch.local_objects(root)
    commit = reader('local/git/commits/' + baseline)
    assert commit['sha'] == baseline and commit['tree']['sha'] == original_tree and commit['parents'] == []
    tree = reader('local/git/trees/' + original_tree + '?recursive=1')
    assert tree['sha'] == original_tree and tree['truncated'] is False
    entries = acl_batch.proof.rules.process_module().entries(reader, 'local', original_tree)
    assert set(entries) == {'app.py', 'tracker/config.json'}
    assert acl_batch.proof.rules.process_module().blob(reader, 'local', entries['app.py']['sha']) == 'base\n'


def test_one_terminal_fold_after_every_tip_and_exact_combined_review_binding(fixture, monkeypatch):
    root, _, section, _, assemble = fixture
    add_fragments(fixture)
    batch = section['integration']
    real_run = subprocess.run
    invocations = []
    def observe(argv, **kwargs):
        if any(str(item).endswith('/fold_changelog.py') for item in argv):
            work = Path(argv[argv.index('--root') + 1])
            for member in batch['members']:
                acl_batch.git(work, 'merge-base', '--is-ancestor', member['head_sha'], 'HEAD')
            invocations.append(argv)
        return real_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', observe)
    result = assemble(apply=True)
    assert len(invocations) == 1 and '--check' not in invocations[0]
    work = Path(result['worktree'])
    fold = result['changelog']
    assert fold['mode'] == 'terminal-fold' and fold['head_sha'] == result['head_sha']
    assert acl_batch.git(work, 'show', '-s', '--format=%P', result['head_sha']) == fold['parent_sha']
    assert len(acl_batch.git(work, 'rev-list', '--no-merges', batch['main_sha'] + '..HEAD',
                             '--not', *(m['head_sha'] for m in batch['members'])).splitlines()) == 1
    assert (work / 'docs/system/CHANGELOG.md').read_text().index('ACL-1002') < (work / 'docs/system/CHANGELOG.md').read_text().index('ACL-1001')
    assert {p.name for p in (work / 'docs/system/changelog.d').iterdir()} == {'README.md'}
    assert 'Changelog fold: ' + result['head_sha'] in result['body'].splitlines()
    native = acl_batch.local_objects(work)
    config = json.loads((root / 'tracker/config.json').read_text())
    pr = {'head': {'sha': result['head_sha']}}
    with pytest.raises(ValueError, match='Independent combined review'):
        acl_batch.proof.require_fold_review(native, config, pr, {'report': 'Reviewed complete sources'})
    assert acl_batch.proof.require_fold_review(native, config, pr,
        {'report': 'Reviewed combined tree\nChangelog fold: ' + result['head_sha']})['head_sha'] == result['head_sha']
    for member in batch['members']:
        acl_batch.git(work, 'merge-base', '--is-ancestor', member['head_sha'], fold['parent_sha'])
        assert acl_batch.git(root, 'rev-parse', member['branch']) == member['head_sha']
    assert acl_batch.git(root, 'rev-parse', 'main') == batch['main_sha']


def test_no_fragments_produces_no_empty_commit(fixture):
    _, _, section, _, assemble = fixture
    result = assemble(apply=True)
    assert result['changelog'] is None
    assert len(acl_batch.git(result['worktree'], 'show', '-s', '--format=%P', result['head_sha']).split()) == 2
    assert 'Changelog fold:' not in result['body']


@pytest.mark.parametrize('case', ['stat-only', 'real-content', 'unmerged-probe'])
def test_owned_merge_cleanup_refreshes_stat_but_preserves_real_edits(fixture, monkeypatch, case):
    root, _, section, _, assemble = fixture
    hooks = Path(acl_batch.git(root, 'rev-parse', '--git-path', 'hooks'))
    if not hooks.is_absolute():
        hooks = root / hooks
    mutation = ("printf 'concurrent fixture edit\\n' > worker1.txt" if case == 'real-content'
                else 'touch -t 200101010000 worker1.txt')
    hook = hooks / 'pre-merge-commit'
    hook.write_text('#!/bin/sh\n' + mutation + "\nprintf 'primary cleanup fixture refusal\\n' >&2\nexit 73\n")
    hook.chmod(0o755)
    actual_run, aborts, refreshes = subprocess.run, [], []

    def observe(argv, **kwargs):
        if argv[3:] == ['merge', '--abort']:
            aborts.append(argv)
        if argv[3:] == ['update-index', '--refresh']:
            refreshes.append(argv)
        if case == 'unmerged-probe' and argv[3:] == ['ls-files', '--unmerged', '-z']:
            raise subprocess.CalledProcessError(128, argv, output='', stderr='unmerged observation unavailable\n')
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'run', observe)
    with pytest.raises(ValueError, match='source-content conflict is not established') as caught:
        assemble(apply=True)
    assert 'primary cleanup fixture refusal' in caught.value.__cause__.stderr
    work = root.parent / 'integration'
    assert acl_batch.git(work, 'rev-parse', 'HEAD') == section['integration']['main_sha']
    marker = Path(acl_batch.git(work, 'rev-parse', '--git-path', 'MERGE_HEAD'))
    if not marker.is_absolute():
        marker = work / marker
    if case == 'unmerged-probe':
        assert (work / 'worker1.txt').read_text() == 'independent work 1'
        assert marker.read_text().strip() == section['integration']['members'][0]['head_sha']
        assert not aborts and len(refreshes) == 1  # Only the successful pre-merge refresh.
        assert 'unmerged observation unavailable' in str(caught.value)
        assert 'cleanup is incomplete' in str(caught.value)
    elif case == 'real-content':
        assert (work / 'worker1.txt').read_text() == 'concurrent fixture edit\n'
        assert marker.read_text().strip() == section['integration']['members'][0]['head_sha']
        assert not aborts and 'abort not attempted' in str(caught.value)
        assert 'cleanup is incomplete' in str(caught.value)
    else:
        assert len(aborts) == 1 and 'Owned unfinished merge aborted' in str(caught.value)
        assert not marker.exists() and not (work / 'worker1.txt').exists()
        assert acl_batch.git(work, 'status', '--porcelain') == ''


def test_missing_approved_tip_stops_before_any_fold(fixture):
    root, _, section, _, _ = fixture
    add_fragments(fixture)
    batch = section['integration']
    partial = root.parent / 'partial'
    acl_batch.git(root, 'worktree', 'add', '--detach', str(partial), batch['main_sha'])
    acl_batch.merge_source(partial, batch['members'][0]['head_sha'])
    before = acl_batch.git(partial, 'rev-parse', 'HEAD')
    with pytest.raises(ValueError, match='every approved source tip'):
        acl_batch.fold_batch(partial, batch, json.loads((root / 'tracker/config.json').read_text()))
    assert acl_batch.git(partial, 'rev-parse', 'HEAD') == before
    assert (partial / 'docs/system/changelog.d/ACL-1001.md').is_file()
    assert (partial / 'docs/system/CHANGELOG.md').read_text().startswith('# System history\n\n## ACL-9')


def test_folded_candidate_rebuild_preserves_old_fold_and_folds_fresh_tips_once(fixture):
    root, remote, section, prs, assemble = fixture
    add_fragments(fixture)
    first = assemble(apply=True)
    batch = section['integration']
    acl_batch.git(Path(first['worktree']), 'push', str(remote), batch['branch'])
    batch['history'] = [{'members': copy.deepcopy(batch['members']), 'main_sha': batch['main_sha']}]
    member = batch['members'][0]
    acl_batch.git(root, 'checkout', member['branch'])
    (root / 'docs/system/changelog.d/ACL-1001.md').write_text('## ACL-1001 — Reviewed correction — 2026-10-24\n\nFull corrected source.\n')
    acl_batch.git(root, 'commit', '-am', 'Author fixes original fragment')
    member['head_sha'] = acl_batch.git(root, 'rev-parse', 'HEAD')
    prs[member['pr']]['head']['sha'] = member['head_sha']
    acl_batch.git(root, 'push', str(remote), member['branch'])
    acl_batch.git(root, 'checkout', 'main')
    result = assemble(apply=True, rebuild=True, previous_head=first['head_sha'], worktree=root.parent/'fresh-fold')
    assert result['changelog']['head_sha'] != first['changelog']['head_sha']
    assert acl_batch.git(first['worktree'], 'rev-parse', 'HEAD') == first['head_sha']
    assert acl_batch.git(root, 'rev-parse', batch['branch']) == first['head_sha']
    assert acl_batch.git(root, 'rev-parse', result['replacement']['local_evidence_ref']) == first['head_sha']
    assert subprocess.run(['git', '-C', result['worktree'], 'merge-base', '--is-ancestor',
                           first['head_sha'], result['head_sha']], capture_output=True).returncode == 1
    assert 'Full corrected source.' in (Path(result['worktree']) / 'docs/system/CHANGELOG.md').read_text()
    subprocess.run(publish_argv(result, remote), check=True)
    assert acl_batch.git(root, 'rev-parse', batch['branch']) == first['head_sha']


@pytest.mark.parametrize('case', ['arbitrary', 'extra-edit', 'second-fold'])
def test_fold_marker_cannot_admit_other_or_stacked_commits(fixture, case):
    root, _, section, _, assemble = fixture
    add_fragments(fixture)
    result = assemble(apply=True)
    work = Path(result['worktree'])
    if case == 'extra-edit':
        (work / 'app.py').write_text('Unreviewed integration implementation\n')
        acl_batch.git(work, 'add', '.')
        acl_batch.git(work, 'commit', '--amend', '--no-edit')
    else:
        (work / 'unknown.txt').write_text('Not a generated fold\n')
        acl_batch.git(work, 'add', '.')
        acl_batch.git(work, 'commit', '-qm', 'Tracker changelog fold: 1' if case == 'second-fold' else 'Extra work')
    with pytest.raises(ValueError):
        acl_batch.composition(work, acl_batch.git(work, 'rev-parse', 'HEAD'), section['integration']['main_sha'],
                              [m['head_sha'] for m in section['integration']['members']])
    assert acl_batch.git(root, 'rev-parse', 'main') == section['integration']['main_sha']


@pytest.mark.parametrize('case', ['invalid', 'symlink', 'ignored-untracked', 'candidate-producer'])
def test_fragment_preflight_and_trusted_producer_preserve_source_evidence(fixture, monkeypatch, case):
    root, remote, section, prs, assemble = fixture
    add_fragments(fixture)
    if case in ('invalid', 'symlink', 'candidate-producer'):
        member = section['integration']['members'][0]
        acl_batch.git(root, 'checkout', member['branch'])
        path = root / 'docs/system/changelog.d/ACL-1001.md'
        if case == 'invalid': path.write_text('Invalid heading\n')
        elif case == 'symlink':
            path.unlink()
            path.symlink_to('../CHANGELOG.md')
        else:
            (root / 'scripts').mkdir()
            (root / 'scripts/fold_changelog.py').write_text('raise RuntimeError("Untrusted source producer executed")\n')
        acl_batch.git(root, 'add', '.')
        acl_batch.git(root, 'commit', '-qm', 'Source boundary fixture')
        member['head_sha'] = acl_batch.git(root, 'rev-parse', 'HEAD')
        prs[member['pr']]['head']['sha'] = member['head_sha']
        acl_batch.git(root, 'push', str(remote), member['branch'])
        acl_batch.git(root, 'checkout', 'main')
    else:
        real_merge = acl_batch.merge_source
        def inject(work, head):
            real_merge(work, head)
            if head == section['integration']['members'][-1]['head_sha']:
                ignored = Path(acl_batch.git(work, 'rev-parse', '--git-path', 'info/exclude'))
                ignored.write_text('docs/system/changelog.d/ACL-777.md\n')
                (work / 'docs/system/changelog.d/ACL-777.md').write_text('## ACL-777 — Untracked — 2026-10-24\n')
        monkeypatch.setattr(acl_batch, 'merge_source', inject)
    if case == 'candidate-producer':
        assert assemble(apply=True)['changelog']['mode'] == 'terminal-fold'
    else:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            assemble(apply=True)
        work = root.parent / 'integration'
        assert (work / 'docs/system/CHANGELOG.md').read_text().startswith('# System history\n\n## ACL-9')
        assert (work / 'docs/system/changelog.d/ACL-1002.md').exists()


def selection_files(config):
    """A complete synthetic retained template, never a production config fallback."""
    text = lambda value: json.dumps(value, indent=2) + '\n'
    workflow = {'execution': config['execution_policy'], 'staging': False,
                'main_branch': 'main', 'stage_branch': 'stage',
                'required_checks': config['delivery_policy']['required_checks']}
    inputs = {'profile': 'package', 'repository': acl_batch.REPOSITORY, 'staging': False}
    for key, (_, mirror, _) in acl_batch.receipt.SELECTIONS.items():
        if key in config:
            workflow[mirror] = config[key]
            inputs[mirror] = config[key]
    descriptor = {'schema_version': 2, 'profile': 'package', 'completion': 'verified-stable-package',
                  'project': {'repository': 'https://github.com/' + acl_batch.REPOSITORY},
                  'standard': {'revision': 'b'*40}, 'workflow': workflow}
    files = {'tracker/config.json': text(config), 'delivery.json': text(descriptor),
             'docs/repository/TRACKER.md': '# Actual selected policies\n' + text(workflow),
             'docs/repository/WORKFLOW.md': '# Workflow\n' + text(config.get('task_context_policy')),
             'docs/repository/DELIVERY.md': '# Delivery\n' + text(config.get('completion_policy')),
             'docs/repository/VERIFICATION.md': '# Verification\n' + text(config.get('ci_retry_policy')),
             'tracker/engine.json': text({'repository': 'MojoTheApe/task-tracker', 'revision': 'c'*40}),
             'docs/system/CHANGELOG.md': '# System history\n\nRetained original history.\n'}
    files['.workflow/template-base.json'] = text({'schema_version': 1, 'standard_revision': 'b'*40,
                                                'inputs': inputs, 'files': copy.deepcopy(files)})
    return files


def selection_source(fixture, *, key='task_context_policy', case=None):
    root, remote, section, prs, _ = fixture
    prior = json.loads((root / 'tracker/config.json').read_text())
    prior['schema_version'] = 2
    prior['delivery_policy']['staging'].update(enabled=False, target='disabled')
    # Existing independent optional policies are retained in the actual B/config.
    if key != 'completion_policy': prior['completion_policy'] = {'schema_version': 1}
    before = selection_files(prior)
    def write(files):
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
    write(before)
    acl_batch.git(root, 'add', '.')
    acl_batch.git(root, 'commit', '-qm', 'Actual prior governance and pristine ancestry')
    baseline = acl_batch.git(root, 'rev-parse', 'HEAD')
    section['integration']['main_sha'] = baseline
    acl_batch.git(root, 'push', str(remote), 'main', 'HEAD:stage')
    branch = 'codex/acl-1001-selection'
    acl_batch.git(root, 'checkout', '-b', branch)
    selected = copy.deepcopy(prior)
    selected[key] = {'schema_version': 1, **({'max_executions': 2} if key == 'ci_retry_policy' else {})}
    if case == 'multiple': selected['ci_retry_policy'] = {'schema_version': 1, 'max_executions': 2}
    if case == 'changed-existing': selected['project']['id_offset'] = 2000
    after = selection_files(selected)
    own = 'docs/system/changelog.d/ACL-1001.md'
    after[own] = '## ACL-1001 — Selected policy — 2026-10-24\n\nFull task record.\n'
    if case == 'missing-fragment': del after[own]
    if case == 'wrong-issue': after[own.replace('1001', '1002')] = after.pop(own).replace('1001', '1002')
    if case == 'extra-runtime': after['app.py'] = 'Unrelated application edit\n'
    if case == 'missing-governance': after['docs/repository/TRACKER.md'] = before['docs/repository/TRACKER.md']
    if case == 'partial-mirror': after['tracker/config.json'] = before['tracker/config.json']
    if case == 'partial-pristine':
        pristine = after['.workflow/template-base.json']
        after = copy.deepcopy(before)
        value = json.loads(after['.workflow/template-base.json'])
        value['files']['tracker/config.json'] = json.loads(pristine)['files']['tracker/config.json']
        after['.workflow/template-base.json'] = json.dumps(value)
        after[own] = '## ACL-1001 — Partial selection — 2026-10-24\n\nMust stop.\n'
    if case == 'changed-pin': after['tracker/engine.json'] = '{"revision":"unreviewed"}\n'
    if case == 'pristine-drift':
        value = json.loads(after['.workflow/template-base.json'])
        value['files']['docs/system/CHANGELOG.md'] = 'Other pristine change'
        after['.workflow/template-base.json'] = json.dumps(value)
    write(after)
    acl_batch.git(root, 'add', '.')
    acl_batch.git(root, 'commit', '-qm', 'Complete reviewed selection source')
    member = {'issue': 1, 'pr': 11, 'branch': branch, 'head_sha': acl_batch.git(root, 'rev-parse', 'HEAD')}
    section['integration'].update(issues=[1], members=[member])
    prs[11]['head'].update(sha=member['head_sha'], ref=branch)
    acl_batch.git(root, 'push', str(remote), branch)
    acl_batch.git(root, 'checkout', 'main')
    return member


@pytest.mark.parametrize('key', ['task_context_policy', 'completion_policy', 'ci_retry_policy'])
def test_canonical_singleton_selection_validates_only_and_retains_exact_source(fixture, key, monkeypatch):
    root, _, section, _, assemble = fixture
    member = selection_source(fixture, key=key)
    real_run = subprocess.run
    invocations = []
    def observe(argv, **kwargs):
        if any(str(item).endswith('/fold_changelog.py') for item in argv): invocations.append(argv)
        return real_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', observe)
    result = assemble(apply=True)
    assert len(invocations) == 1 and '--check' in invocations[0]
    work = Path(result['worktree'])
    assert result['changelog']['mode'] == 'selection-check' and result['changelog']['policy'] == key
    assert result['tree'] == acl_batch.git(root, 'rev-parse', member['head_sha'] + '^{tree}')
    assert (work / 'docs/system/changelog.d/ACL-1001.md').is_file()
    assert (work / 'docs/system/CHANGELOG.md').read_text() == '# System history\n\nRetained original history.\n'
    assert 'Changelog fold:' not in result['body']
    assert acl_batch.git(root, 'rev-parse', 'main') == section['integration']['main_sha']
    assert len(acl_batch.git(work, 'show', '-s', '--format=%P', result['head_sha']).split()) == 2


@pytest.mark.parametrize('case', ['multiple', 'changed-existing', 'missing-fragment', 'wrong-issue',
                                'extra-runtime', 'missing-governance', 'partial-mirror', 'partial-pristine',
                                'changed-pin', 'pristine-drift', 'mixed', 'assembled-drift'])
def test_suspected_invalid_selection_stops_without_folding_or_falling_through(fixture, monkeypatch, case):
    root, _, section, _, assemble = fixture
    selection_source(fixture, case=case)
    if case == 'mixed':
        # A second independent frozen member cannot use the singleton distinction.
        section['integration']['issues'].append(2)
        second = {'issue': 2, 'pr': 12, 'branch': 'codex/acl-1002-source',
                  'head_sha': acl_batch.git(root, 'rev-parse', 'codex/acl-1002-source')}
        section['integration']['members'].append(second)
    if case == 'assembled-drift':
        real_merge = acl_batch.merge_source
        def changed_merge(work, head):
            real_merge(work, head)
            (work / 'app.py').write_text('Unapproved merge resolution\n')
            acl_batch.git(work, 'add', 'app.py')
            acl_batch.git(work, 'commit', '--amend', '--no-edit')
        monkeypatch.setattr(acl_batch, 'merge_source', changed_merge)
    real_run = subprocess.run
    producers = []
    def observe(argv, **kwargs):
        if any(str(item).endswith('/fold_changelog.py') for item in argv): producers.append(argv)
        return real_run(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', observe)
    with pytest.raises(ValueError): assemble(apply=True)
    assert not producers
    work = root.parent / 'integration'
    assert (work / 'docs/system/CHANGELOG.md').read_text() == '# System history\n\nRetained original history.\n'
