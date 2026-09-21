#!/usr/bin/env python3
"""Read-only adoption checks and a guard against premature Tracker mutations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'MojoTheApe/adopy-campaing-launcher'
# These commands cannot change Issues, queue state, labels or delivery receipts.
READ_ONLY_TRACKER = {'guide', 'check-pr', 'check-merge'}
ACTIVATION_EVIDENCE = ('open_prs', 'historical_completion', 'branch_transition',
                       'registry', 'stage', 'ci', 'independent_review')


def read_state(root=ROOT):
    state = json.loads((Path(root) / '.workflow/adoption.json').read_text())
    if state.get('schema_version') != 1 or state.get('state') not in ('prepared', 'active'):
        raise ValueError('Unknown ACL adoption state; inspect the recorded migration')
    if state['state'] == 'active':
        evidence = state.get('evidence', {})
        if any(not isinstance(evidence.get(k), str) or not evidence[k].strip()
               for k in ACTIVATION_EVIDENCE):
            raise ValueError('Active adoption needs the reviewed cutover evidence, not just a flag')
    return state


def guard_tracker(arguments, root=ROOT):
    state = read_state(root)
    command = arguments[0] if arguments else ''
    if state['state'] != 'active' and command not in READ_ONLY_TRACKER and command not in ('', '--help', '-h'):
        raise ValueError('ACL adoption is prepared, not active. Use the original pinned client in the '
                         'master checkout for Issue/queue changes. Read docs/repository/ADOPTION.md.')


def github(path):
    result = subprocess.run(['gh', 'api', path], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def remote_blockers(request=github, root=ROOT):
    """Report live prerequisites without changing GitHub or trusting old checks."""
    state = read_state(root)
    repo = 'repos/' + REPOSITORY
    problems = []
    for number in state['pre_adoption_prs']:
        pr = request(repo + f'/pulls/{number}')
        if pr['state'] == 'open':
            problems.append(f'Pre-adoption PR #{number} is still open')
    source = request(repo + '/branches/' + state['source_branch'])['commit']['sha']
    runs = request(repo + '/actions/runs?head_sha=' + source + '&per_page=100')['workflow_runs']
    for path in ('.github/workflows/ci.yml', '.github/workflows/windows-installer.yml'):
        relevant = [r for r in runs if r.get('path') == path and r.get('head_branch') == state['source_branch']
                    and r.get('event') in ('push', 'workflow_dispatch') and r.get('head_sha') == source]
        latest = max(relevant, key=lambda r: (r['id'], r.get('run_attempt', 1)), default={})
        if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
            problems.append('No successful latest full run on current ' + state['source_branch'] + ': ' + path)
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['audit'])
    parser.add_argument('--remote', action='store_true', help='Read GitHub PR/CI prerequisites; never mutate')
    args = parser.parse_args()
    state = read_state()
    result = {'state': state['state'], 'issue': state['issue'],
              'activation_verified': False,
              'next': 'Follow docs/repository/ADOPTION.md. This audit never authorizes merge, release or cutover.'}
    if args.remote:
        result['remote_blockers'] = remote_blockers()
    print(json.dumps(result, indent=2))
    return 1 if result.get('remote_blockers') else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print('Adoption audit failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
