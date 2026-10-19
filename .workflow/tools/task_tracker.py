#!/usr/bin/env python3
"""Project-local client for the pinned shared tracker; Python 3.11+ and gh."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    lock = json.loads((root / 'tracker/engine.json').read_text(encoding='utf-8'))
    repo, revision = lock['repository'], lock['revision']
    if repo != 'MojoTheApe/task-tracker' or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Expected the approved shared tracker repository and a pinned full commit SHA')
    override = os.environ.get('TASK_TRACKER_HOME')
    engine = Path(override).resolve() if override else root / '.tracker-runtime' / revision
    if not engine.exists():
        if override: raise ValueError('TASK_TRACKER_HOME does not exist')
        engine.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['gh', 'repo', 'clone', repo, str(engine), '--', '--no-checkout', '--filter=blob:none'], check=True)
        subprocess.run(['git', '-C', str(engine), 'checkout', '--detach', revision], check=True)
    actual = subprocess.check_output(['git', '-C', str(engine), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != revision:
        raise ValueError('Shared tracker checkout does not match tracker/engine.json; use its exact revision')
    if subprocess.check_output(['git', '-C', str(engine), 'status', '--porcelain', '--untracked-files=no'], text=True).strip():
        raise ValueError('Shared tracker checkout has modified tracked files; inspect it before execution')
    # Explicit import path prevents ACL's retained compatibility package shadowing the shared engine.
    code = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from tracker.cli import main; raise SystemExit(main())'
    return subprocess.run([sys.executable, '-c', code, str(engine), '--config', str(root / 'tracker/config.json'), *sys.argv[1:]], cwd=root).returncode


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Tracker client failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
