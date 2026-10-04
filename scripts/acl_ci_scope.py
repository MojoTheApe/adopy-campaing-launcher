"""Select expensive Windows checks conservatively; never skip unknown changes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

try:
    from scripts import acl_ci_reuse as reuse
except ImportError:  # запуск как python3 scripts/acl_ci_scope.py
    import acl_ci_reuse as reuse

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


def decide(root, event_name, event, *, ref="", workflow="ci.yml", repo="", api=None, run_id=None):
    """Классификация, затем (ACL-1565) переиспользование полного зелёного прогона того же дерева.

    Теги релизов и ручной запуск всегда идут полностью; любая ошибка — тоже."""
    result = select(root, event_name, event)
    if not result["windows"]:
        return result
    if event_name not in ("push", "pull_request") or str(ref).startswith("refs/tags/") or api is None:
        return result
    try:
        tree = reuse.tree_of(root, "HEAD")
        proof = reuse.find_reuse(root, api, repo, workflow, tree, exclude_run_id=run_id) if tree else None
    except Exception:  # noqa: BLE001 — сомнение означает полный прогон
        return dict(result, reuse="lookup-failed-run-full")
    if not proof:
        return dict(result, reuse="no-proven-full-run")
    return {"windows": False, "reason": "reused-green-full-run-of-same-tree",
            "reused_from": proof["run_url"], "tree": proof["tree"], "commit": proof["commit"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--event-file", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "")))
    p.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    p.add_argument("--output", type=Path, default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    p.add_argument("--summary", type=Path, default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None)
    p.add_argument("--workflow", default="ci.yml", choices=sorted(reuse.REQUIRED_JOBS))
    args = p.parse_args()
    try:
        event = json.loads(args.event_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        event = {}
    token = os.environ.get("GITHUB_TOKEN", "")
    api = reuse.github_api(token, os.environ.get("GITHUB_API_URL") or "https://api.github.com") if token else None
    result = decide(Path(__file__).resolve().parents[1], args.event_name, event,
                    ref=os.environ.get("GITHUB_REF", ""), workflow=args.workflow,
                    repo=os.environ.get("GITHUB_REPOSITORY", ""), api=api, run_id=os.environ.get("GITHUB_RUN_ID"))
    if args.output:
        with args.output.open("a", encoding="utf-8") as f:
            f.write("windows=" + str(result["windows"]).lower() + "\n")
            f.write("reused_from=" + result.get("reused_from", "") + "\n")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            if result.get("reused_from"):
                f.write(reuse.summary_line(result["reused_from"], result["tree"]) + "\n")
            else:
                f.write(f"Scope: windows={str(result['windows']).lower()} ({result['reason']}"
                        + (f", {result['reuse']}" if result.get("reuse") else "") + ")\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
