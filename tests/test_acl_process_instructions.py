"""Instruction shortcuts require real immutable Git evidence and pre-existing opt-in."""
import json
from pathlib import Path
import subprocess

import pytest

from scripts.acl_ci_scope import select

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'Instruction fixture')
    git('config', 'user.email', 'instructions@example.invalid')
    for name in ('delivery.json', 'tracker/config.json'):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / name).read_bytes())
    (tmp_path / 'AGENTS.md').write_text('# Repository instructions\n')
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/product.py').write_text('# Existing application file\n')
    git('add', '.'); git('commit', '-qm', 'adopted baseline')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)

    def change(name='docs/repository/NEXT.md', content='# Explain the process\n'):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git('add', '.'); git('commit', '-qm', 'instruction candidate')
        return git('rev-parse', 'HEAD')

    def source(head, branch='codex/acl-1751-instructions', target='main'):
        repo = {'full_name': 'MojoTheApe/adopy-campaing-launcher'}
        return {'pull_request': {'base': {'sha': base, 'ref': target, 'repo': dict(repo)},
                                 'head': {'sha': head, 'ref': branch, 'repo': dict(repo)}}}

    def push(head):
        git('update-ref', 'refs/remotes/origin/main', head)
        return {'before': base, 'after': head,
                'repository': {'full_name': 'MojoTheApe/adopy-campaing-launcher'}}
    return tmp_path, base, git, change, source, push


@pytest.mark.parametrize('path', ['AGENTS.md', 'CLAUDE.md', 'docs/repository/NEXT.md', 'docs/repository/nested/NEXT.md'])
def test_normal_instruction_source_and_main_push_skip_only_runtime_jobs(project, path):
    root, _, _, change, source, push = project
    head = change(path)
    for workflow in ('ci.yml', 'windows-installer.yml'):
        result = select(root, 'pull_request', source(head), workflow=workflow)
        assert not result['windows'] and not result['linux']
        assert result['reason'] == 'process-instructions-only' and result['process']['head_sha'] == head
    payload = push(head)
    for workflow in ('ci.yml', 'windows-installer.yml'):
        result = select(root, 'push', payload, ref='refs/heads/main', workflow=workflow)
        assert not result['windows'] and not result['linux']
        assert result['reason'] == 'process-instructions-only'


@pytest.mark.parametrize('path', ['src/app.py', 'docs/runtime/DELIVERY.md', 'docs/system/PROJECT.md',
                                  'README.md', 'scripts/change.py', '.github/workflows/ci.yml', 'config/test.md'])
def test_other_paths_and_mixed_changes_have_no_instruction_shortcut(project, path):
    root, _, _, change, source, push = project
    change('docs/repository/NEXT.md')
    head = change(path)
    for event, payload, ref in [('pull_request', source(head), ''), ('push', push(head), 'refs/heads/main')]:
        result = select(root, event, payload, ref=ref)
        assert result['reason'] != 'process-instructions-only' and 'process' not in result
        assert select(root, event, payload, ref=ref, workflow='windows-installer.yml')['reason'] != 'process-instructions-only'
        if event == 'push':
            assert result['windows'] and result['linux']


def test_policy_adoption_cannot_activate_its_own_shortcut(project):
    root, _, git, _, source, push = project
    for name in ('delivery.json', 'tracker/config.json'):
        path = root / name; data = json.loads(path.read_text())
        (data['workflow'] if name == 'delivery.json' else data).pop('process_instructions')
        path.write_text(json.dumps(data))
    git('add', '.'); git('commit', '-qm', 'old policy')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)
    for name in ('delivery.json', 'tracker/config.json'):
        (root / name).write_bytes((ROOT / name).read_bytes())
    git('add', '.'); git('commit', '-qm', 'adopt process policy')
    head = git('rev-parse', 'HEAD')
    event = source(head); event['pull_request']['base']['sha'] = base
    assert select(root, 'pull_request', event)['windows']
    event = push(head); event['before'] = base
    assert select(root, 'push', event, ref='refs/heads/main')['windows']


@pytest.mark.parametrize('kind', ['executable', 'symlink', 'outside-rename', 'stale-main', 'missing-base', 'zero-base', 'foreign-repo'])
def test_unsafe_git_evidence_never_skips_main_runtime_checks(project, kind):
    root, base, git, change, source, push = project
    if kind == 'outside-rename':
        (root / 'docs/repository').mkdir(parents=True)
        git('mv', 'src/product.py', 'docs/repository/NEXT.md')
        git('commit', '-qm', 'move application to instructions'); head = git('rev-parse', 'HEAD')
    elif kind == 'symlink':
        path = root / 'docs/repository/NEXT.md'; path.parent.mkdir(parents=True)
        path.symlink_to('../../AGENTS.md')
        git('add', '.'); git('commit', '-qm', 'symlink candidate'); head = git('rev-parse', 'HEAD')
    else:
        head = change()
        if kind == 'executable':
            git('update-index', '--chmod=+x', 'docs/repository/NEXT.md')
            git('commit', '-qm', 'executable instruction'); head = git('rev-parse', 'HEAD')
    event = push(head)
    if kind == 'stale-main': git('update-ref', 'refs/remotes/origin/main', base)
    if kind == 'missing-base': event['before'] = 'd' * 40
    if kind == 'zero-base': event['before'] = '0' * 40
    if kind == 'foreign-repo': event['repository']['full_name'] = 'Other/repository'
    for workflow in ('ci.yml', 'windows-installer.yml'):
        result = select(root, 'push', event, ref='refs/heads/main', workflow=workflow)
        assert result['windows'] and result['reason'] != 'process-instructions-only'


@pytest.mark.parametrize('context', ['manual', 'stage-push', 'batch', 'promotion', 'unknown-source'])
def test_stage_batch_promotion_manual_and_unknown_are_ordinary_checks(project, context):
    root, _, _, change, source, push = project
    head = change()
    event, payload, ref = 'pull_request', source(head), ''
    if context == 'manual': event, payload = 'workflow_dispatch', {}
    elif context == 'stage-push': event, payload, ref = 'push', push(head), 'refs/heads/stage'
    elif context == 'batch': payload = source(head, 'codex/batch-' + 'a' * 32, 'stage')
    elif context == 'promotion': payload = source(head, 'stage', 'main')
    else: payload = source(head, 'unknown')
    result = select(root, event, payload, ref=ref)
    assert result['windows'] and result['linux'] and result['python'] == ['3.11', '3.13']
    assert select(root, event, payload, ref=ref, workflow='windows-installer.yml')['windows']


def test_cheap_check_accepts_real_local_links_and_preserves_archived_text(project):
    from scripts.acl_process_instructions import check
    root, _, _, _, _, _ = project
    docs = root / 'docs/repository'; docs.mkdir(parents=True)
    (docs / 'NEXT.md').write_text('[Reading map](../../AGENTS.md)\n')
    history = docs / 'history'; history.mkdir()
    (history / 'OLD.md').write_text('[Original historical reference](original-unmoved-name.md)\n')
    proof = check(root)
    assert proof['result'] == 'passed' and proof['local_markdown_links'] == 1


@pytest.mark.parametrize('target', ['MISSING.md', '../../../../outside.md'])
def test_cheap_check_rejects_missing_or_escaping_current_instruction_links(project, target):
    from scripts.acl_process_instructions import check
    root, _, _, _, _, _ = project
    docs = root / 'docs/repository'; docs.mkdir(parents=True)
    (docs / 'NEXT.md').write_text('[Required procedure](' + target + ')\n')
    with pytest.raises(ValueError, match='missing local instruction link'):
        check(root)


def test_cheap_check_rejects_descriptor_policy_drift(project):
    from scripts.acl_process_instructions import check
    root, _, _, _, _, _ = project
    path = root / 'delivery.json'; data = json.loads(path.read_text())
    data['workflow']['process_instructions']['required_checks'].append('Invented check')
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='must explicitly match'):
        check(root)


def test_a_different_tested_checkout_cannot_use_the_source_shortcut(project):
    root, _, _, change, source, _ = project
    head = change()
    payload = source(head)
    change('src/unreviewed.py')
    result = select(root, 'pull_request', payload)
    assert result['windows'] and result['linux'] and result['reason'] != 'process-instructions-only'


@pytest.mark.parametrize('side', ['base', 'head'])
def test_foreign_or_unknown_source_repository_does_not_get_the_shortcut(project, side):
    root, _, _, change, source, _ = project
    payload = source(change())
    payload['pull_request'][side]['repo']['full_name'] = 'Other/repository'
    result = select(root, 'pull_request', payload)
    assert result['windows'] and result['linux']
    assert select(root, 'pull_request', payload, workflow='windows-installer.yml')['windows']


def test_relocated_checker_cli_loads_real_project_config_by_default(monkeypatch, capsys):
    # Invoke the actual argument parser without --config; stub only the GH read.
    # The old parents[1] path fails before this first authenticated request.
    import sys
    from types import SimpleNamespace
    from scripts import tracker_pr_check as checker
    repository = json.loads((ROOT / 'tracker/config.json').read_text())['project']['repository']
    prefix = 'repos/' + repository
    pr = {'number': 751, 'state': 'open', 'draft': False, 'merged': False,
          'title': '[ACL-1750] Explain repository instructions', 'body': 'Tracker issues: ACL-1750',
          'base': {'ref': 'stage', 'sha': 'a' * 40, 'repo': {'full_name': repository}},
          'head': {'ref': 'codex/acl-1750-process-instructions', 'sha': 'b' * 40,
                   'repo': {'full_name': repository}}}
    issue = {'number': 750, 'state': 'open', 'title': 'Explain repository instructions',
             'body': '## Description\nExplain the maintained process.\n## Specifications\nVerified instruction scope.',
             'labels': [{'name': name} for name in ['status:review', 'module:platform',
                                                   'type:improvement', 'priority:medium']]}
    calls = []
    def read(argv, **kwargs):
        assert argv[:2] == ['gh', 'api']
        calls.append(argv[2])
        if argv[2] == prefix + '/pulls/751': result = pr
        elif argv[2] == prefix + '/issues/750': result = issue
        else: raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))
    monkeypatch.setattr(sys, 'argv', ['scripts/tracker_pr_check.py', '--pr', '751'])
    monkeypatch.setattr(checker.subprocess, 'run', read)
    checker.main()
    result = json.loads(capsys.readouterr().out)
    assert result['verified'] is True and result['issues'] == [750]
    assert calls == [prefix + '/pulls/751', prefix + '/issues/750']
