#!/usr/bin/env python3
"""Portable read-only PR linkage check; no shared-repository token is needed in CI."""
import argparse
import json
from pathlib import Path
import re
import subprocess
try:
    from . import delivery_policy
except ImportError:
    try:
        import tracker_delivery_policy as delivery_policy
    except ImportError:
        import delivery_policy


def linked_numbers(body, config):
    lines = re.findall(r'^Tracker issues:\s*([^\n]+)', body or '', re.M | re.I)
    if len(lines) != 1: raise ValueError('PR needs one explicit Tracker issues: line')
    project = config['project']; prefix = project.get('id_prefix', '')
    legacy = {value.upper(): int(number) for number,value in project.get('legacy_task_ids', {}).items()}
    tokens = [s.strip('`') for s in re.split(r'[\s,]+', lines[0].strip()) if s]
    numbers = []
    for value in tokens:
        if re.fullmatch(r'#[1-9]\d*', value): number = int(value[1:])
        elif prefix and re.fullmatch(prefix+r'-[1-9]\d*',value,re.I):
            number = legacy.get(value.upper(), int(value.split('-')[1])-project.get('id_offset',0))
            expected = project.get('legacy_task_ids',{}).get(str(number),f"{prefix}-{number+project.get('id_offset',0)}")
            if expected.casefold()!=value.casefold(): raise ValueError('Unknown task identifier')
        elif re.fullmatch(r'https://github\.com/'+re.escape(project['repository'])+r'/issues/[1-9]\d*',value):
            number = int(value.rsplit('/',1)[1])
        else: raise ValueError('Tracker issues must reference task IDs or Issue URLs from this repository')
        if number <= 0: raise ValueError('Unknown task identifier')
        numbers.append(number)
    if not numbers or len(set(numbers)) != len(numbers): raise ValueError('List each linked task exactly once')
    return numbers


def check(request, config, pr_number, *, allow_transition=False, admission=False, approval_recovery=False):
    repo=config['project']['repository'];root='repos/'+repo
    pr=request(root+f'/pulls/{pr_number}')
    direction = delivery_policy.route(config, pr, request)
    if pr['base'].get('repo',{}).get('full_name',repo).lower()!=repo.lower():
        raise ValueError('PR belongs to a different repository')
    numbers=linked_numbers(pr.get('body'),config)
    delivery_policy.naming(config, pr, numbers, request)
    if direction == 'process' and len(numbers) != 1:
        raise ValueError('Process instructions require one task per source PR')
    stages={s if isinstance(s,str) else s['id']:s if isinstance(s,str) else s['phase'] for s in config['workflow']}
    modules={m['id'] for m in config['modules']}
    for n in numbers:
        issue=request(root+f'/issues/{n}')
        if 'task_context_policy' in config:
            delivery_policy.context_module().requirements(issue.get('body'))
        if 'pull_request' in issue: raise ValueError(f'#{n} is a PR, not a tracked task')
        labels={l['name'] for l in issue['labels']}
        status=[s[7:] for s in labels if s.startswith('status:')]
        allowed={'review','merged'}|({'in_progress'} if pr.get('draft') else set())
        if delivery_policy.policy(config):
            allowed.add('stage')
            if delivery_policy.batch_mode(config):
                allowed.add('approved')
            if direction == 'promotion' and not pr.get('draft'):
                allowed = {'stage', 'merged'}
        if direction == 'process':
            allowed = {'review', 'merged'} | ({'in_progress'} if pr.get('draft') else set())
        resuming_merge = (allow_transition and pr.get('merged') and len(status)==2
                          and {stages.get(s) for s in status} in ({'review','merged'}, {'stage','merged'}, {'review','stage'}, {'approved','stage'}, {'approved','merged'}))
        if direction == 'process':
            resuming_merge = (allow_transition and pr.get('merged') and len(status) == 2
                              and {stages.get(s) for s in status} == {'review', 'merged'})
        resuming_merge = resuming_merge or (approval_recovery and delivery_policy.batch_mode(config)
                                            and direction == 'approval' and len(status) == 2
                                            and {stages.get(s) for s in status} == {'review', 'approved'})
        if not resuming_merge and (len(status)!=1 or stages.get(status[0]) not in allowed):
            raise ValueError(f'#{n}: start and implement the task, then move it to Review before a ready PR')
        closed_by_merge = allow_transition and pr.get('merged') and issue.get('state_reason')=='completed'
        if 'flag:archived' in labels or issue.get('state_reason')=='not_planned' or issue.get('state')=='closed' and stages.get(status[0])!='merged' and not closed_by_merge:
            raise ValueError(f'#{n} is archived or cancelled')
        if 'flag:blocked' in labels: raise ValueError(f'#{n} has an unresolved blocker')
        module=[s[7:] for s in labels if s.startswith('module:')]
        kind=[s[5:] for s in labels if s.startswith('type:')]
        priority=[s[9:] for s in labels if s.startswith('priority:')]
        if len(module)!=1 or module[0] not in modules or len(kind)!=1 or kind[0] not in ('feature','bug','improvement','research','operations'):
            raise ValueError(f'#{n}: classify one valid module and type')
        if len(priority)!=1 or priority[0] not in ('high','medium','low'):
            raise ValueError(f'#{n}: choose one valid priority')
    result = {'repository':repo,'pr':pr_number,'issues':numbers,'verified':True}
    if admission:
        result['admission'] = delivery_policy.admit(request, config, pr, numbers)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pr',type=int,required=True)
    p.add_argument('--config',type=Path,default=Path(__file__).resolve().parents[2]/'tracker/config.json')
    p.add_argument('--admission', action='store_true', help='Verify independent review, required checks and stage before merging')
    args=p.parse_args()
    if args.pr<1: raise ValueError('PR number must be positive')
    def request(path):
        result=subprocess.run(['gh','api',path],text=True,capture_output=True,timeout=90)
        if result.returncode: raise RuntimeError('GitHub read failed; PR linkage is not verified')
        return json.loads(result.stdout)
    print(json.dumps(check(request,json.loads(args.config.read_text()),args.pr,admission=args.admission),indent=2))


if __name__=='__main__':
    try: main()
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as error:
        print(str(error));raise SystemExit(1)
