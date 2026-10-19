"""Check the explicit process policy and local repository-instruction links."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

try:
    from scripts import tracker_process_instructions as rules
except ImportError:
    import tracker_process_instructions as rules


def check(root):
    root = Path(root).resolve()
    descriptor = json.loads((root / 'delivery.json').read_text(encoding='utf-8'))
    config = json.loads((root / 'tracker/config.json').read_text(encoding='utf-8'))
    selected = rules.policy(config)
    if not selected or descriptor['workflow'].get('process_instructions') != selected:
        raise ValueError('Descriptor and Tracker must explicitly match the process policy')
    if selected['required_checks'] != ['descriptor', 'tracker-link', 'Process instructions']:
        raise ValueError('ACL process admission requires its actual descriptor, linkage and instruction checks')
    files = [root / name for name in ('AGENTS.md', 'CLAUDE.md') if (root / name).exists()]
    # Archived pre-adoption texts retain their original relative references.
    # Validate maintained instructions without rewriting their dated evidence.
    files += sorted(path for path in (root / 'docs/repository').rglob('*.md')
                    if 'history' not in path.relative_to(root / 'docs/repository').parts)
    count = 0
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Process instruction files must be normal files')
        text = path.read_text(encoding='utf-8')
        for target in re.findall(r'(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)', text):
            target = target.strip().split(' "', 1)[0].strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path or not parsed.path.endswith('.md'):
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError(f'{path.relative_to(root)}: missing local instruction link {target}')
            count += 1
    return {'result': 'passed', 'instruction_files': len(files), 'local_markdown_links': count,
            'required_checks': selected['required_checks']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps(check(args.root), indent=2))


if __name__ == '__main__':
    main()
