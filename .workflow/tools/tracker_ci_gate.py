#!/usr/bin/env python3
"""Read-only CI planning/gating using exported helpers and a same-repo gh token.

No private Tracker engine, runtime cache, state writes or admission transitions.
Run from the project root; author collection still uses the pinned full client.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import tracker_ci_evidence as ci
import tracker_delivery_policy as rules
from tracker_delivery_receipt import github


def read_state(request, repository):
    root = 'repos/' + repository
    rows = request(root + '/git/matching-refs/heads/tracker-state')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('CI needs an unambiguous native state ref list')
    exact = [row for row in rows if row.get('ref') == 'refs/heads/tracker-state']
    if not exact:
        return {'schema_version': 1, 'active': None, 'run': None, 'todo_order': [], 'todo_added': {}}
    if len(exact) != 1:
        raise ValueError('CI needs exactly one official tracker state ref')
    target = exact[0].get('object', {})
    if (not isinstance(target, dict) or target.get('type') != 'commit'
            or not isinstance(target.get('sha'), str) or not ci.SHA.fullmatch(target['sha'])):
        raise ValueError('CI state ref must identify an immutable commit')
    row = request(root + '/contents/.tracker-state.json?ref=' + target['sha'])
    value = json.loads(rules.file_bytes(request, root, row), object_pairs_hook=ci.unique_object)
    if rules.state_schema(value) not in (1, 2) or not isinstance(value.get('todo_order'), list):
        raise ValueError('CI shared state is invalid; inspect the maintained state before retrying')
    return value


def run(args, transport=github, environ=None):
    env = os.environ if environ is None else environ
    config = json.loads(args.config.read_text(), object_pairs_hook=ci.unique_object)
    repository = config['project']['repository']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
        raise ValueError('CI config must identify its official repository')
    if env.get('GITHUB_ACTIONS') == 'true' and env.get('GITHUB_REPOSITORY') != repository:
        raise ValueError('CI config must match its actual Actions repository')
    root = 'repos/' + repository
    def request(path):
        if not isinstance(path, str) or not path.startswith(root + '/'):
            raise ValueError('CI transport is restricted to reads in this repository')
        return transport(path)  # GET only; never forward a method/payload or log output.
    def read_log(job):
        return request(root + f'/actions/jobs/{int(job)}/logs')
    context = args.context
    if context == 'auto':
        if args.pr:
            pr = request(root + f'/pulls/{args.pr}')
            direction = rules.route(config, pr, request)
            context = ('integration' if rules.batch_mode(config) and direction in ('stage', 'batch')
                       else 'promotion' if direction == 'promotion' else 'source')
        else:
            context = 'main'
    if args.pr < 0 or not ci.SHA.fullmatch(args.head) or not args.pr and context != 'main':
        raise ValueError('CI needs the actual PR/head; main may resolve PR zero')
    if args.action == 'gate' and (args.run is None or args.run < 1):
        raise ValueError('CI gate needs its actual native run ID')
    state = read_state(request, repository)
    positional = (request, read_log, config, state, args.profile, args.pr, args.head)
    result = ci.plan(*positional, context) if args.action == 'plan' else ci.gate(*positional, args.run, context)
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8') as output:
            output.write('missing=' + json.dumps(result['missing'], separators=(',', ':')) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'gate'))
    parser.add_argument('--config', type=Path, default=Path('tracker/config.json'))
    parser.add_argument('--profile', required=True)
    parser.add_argument('--pr', type=int, default=0)
    parser.add_argument('--head', required=True)
    parser.add_argument('--run', type=int)
    parser.add_argument('--context', choices=('auto', 'source', 'integration', 'main', 'promotion'), default='auto')
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args), indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Native state/log content and raw transport errors stay private.
        print('CI verification unavailable: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
