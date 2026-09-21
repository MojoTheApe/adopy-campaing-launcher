#!/usr/bin/env python3
"""PR linkage for the exact adoption bootstrap or the adopted shared policy."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.workflow/tools'))
import tracker_pr_check as shared
from acl_adoption import read_state


def check(request, number, root=ROOT):
    root = Path(root)
    state = read_state(root)
    config = json.loads((root / 'tracker/config.json').read_text())
    repo = config['project']['repository']
    pr = request(f'repos/{repo}/pulls/{number}')
    if state['state'] == 'prepared':
        expected = f"codex/acl-{state['issue'] + config['project']['id_offset']}-master-adoption"
        if (pr['base']['ref'] != state['source_branch'] or pr['head']['ref'] != expected
                or pr['head'].get('repo', {}).get('full_name') != repo
                or shared.linked_numbers(pr.get('body'), config) != [state['issue']]):
            raise ValueError('Prepared adoption validates only its own PR against the original branch')
        base = request(f"repos/{repo}/contents/tracker/config.json?ref={pr['base']['sha']}")
        legacy = json.loads(base64.b64decode(base['content']))
        if legacy != json.loads((root / '.workflow/legacy/tracker-config.json').read_text()):
            raise ValueError('Base Tracker config changed; refresh the audited migration, do not guess')
        result = shared.check(request, legacy, number)
        result['policy'] = 'legacy linkage only; migration admission and activation remain pending'
        return result
    return shared.check(request, config, number)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr', required=True, type=int)
    args = parser.parse_args()

    def request(path):
        result = subprocess.run(['gh', 'api', path], text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    print(json.dumps(check(request, args.pr), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('PR linkage failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
