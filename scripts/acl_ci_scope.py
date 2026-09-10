"""Select expensive Windows checks conservatively; never skip unknown changes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT_DOCS = {"README.md", "AGENTS.md", "CLAUDE.md", "AGENT_CONTEXT.md", "CHANGELOG.md"}
TRACKER_METADATA = {"tracker/config.json", "tracker/engine.json"}


def classify(paths, event_name):
    if event_name != "pull_request" or not paths:
        return {"windows": True, "reason": "full-base-or-manual-validation"}
    for name in paths:
        document = name in ROOT_DOCS or name.startswith("docs/") and name.endswith(".md")
        if not document and name not in TRACKER_METADATA:
            return {"windows": True, "reason": "product-build-or-unknown-change"}
    return {"windows": False, "reason": "documentation-and-tracker-metadata-only"}


def changed_paths(root, base, head):
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha or "") for sha in (base, head)):
        raise ValueError("Expected exact base/head commits")
    ancestor = subprocess.check_output(["git", "merge-base", base, head], cwd=root, text=True).strip()
    # Disable rename folding so moving/deleting product code cannot look docs-only.
    output = subprocess.check_output(["git", "diff", "--no-renames", "--name-only", "-z", ancestor, head], cwd=root)
    return [p.decode("utf-8", errors="surrogateescape") for p in output.split(b"\0") if p]


def select(root, event_name, event):
    try:
        if event_name != "pull_request":
            return classify([], event_name)
        pr = event["pull_request"]
        return classify(changed_paths(root, pr["base"]["sha"], pr["head"]["sha"]), event_name)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {"windows": True, "reason": "classification-unavailable-run-full"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--event-file", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "")))
    p.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    p.add_argument("--output", type=Path, default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    args = p.parse_args()
    try:
        event = json.loads(args.event_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        event = {}
    result = select(Path(__file__).resolve().parents[1], args.event_name, event)
    if args.output:
        with args.output.open("a", encoding="utf-8") as f:
            f.write("windows=" + str(result["windows"]).lower() + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
