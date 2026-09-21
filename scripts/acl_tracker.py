"""Compatibility sync command. Tracker v2 lives in the portable tracker package."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker.github import GitHub
from tracker.model import DEFAULT_CONFIG, load_config
from tracker.sync import sync


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repository", help="Optional repository override")
    parser.add_argument("--gh-command-json", default='["gh"]')
    parser.add_argument("--snapshot", help="Legacy argument; v2 modules come from config", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args(argv)
    try:
        if (Path(__file__).resolve().parents[1] / '.workflow/adoption.json').exists():
            raise ValueError('Legacy board publishing is retired. Use the shared Task Tracker registration; read docs/repository/ADOPTION.md.')
        config = load_config(args.config)
        if args.repository: config["project"]["repository"] = args.repository
        command = json.loads(args.gh_command_json)
        if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x for x in command):
            raise ValueError("Expected a nonempty command array")
        sync(GitHub(config["project"]["repository"],command),config,args.output,allow_empty=args.allow_empty)
        return 0
    except (ValueError,RuntimeError,OSError) as error:
        print(str(error),file=sys.stderr);return 1

if __name__ == "__main__":
    raise SystemExit(main())
