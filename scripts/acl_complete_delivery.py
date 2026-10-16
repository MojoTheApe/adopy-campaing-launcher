#!/usr/bin/env python3
"""Exact authorized release bridge; never manufacture Tracker delivery evidence."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys

REPOSITORY = "MojoTheApe/adopy-campaing-launcher"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def run(*args):
    return subprocess.check_output(list(args), text=True, encoding="utf-8").strip()


def release_tag(request):
    if (not SHA.fullmatch(request.get("merge_sha", ""))
            or not re.fullmatch(r"[0-9a-f]{32}", request.get("batch_id", ""))
            or not request.get("issues") or not request.get("target")):
        raise ValueError("Expected an admitted exact-batch delivery request")
    # Stable across retries and ordered like the maintained dated versions.
    date = datetime.fromisoformat(request["requested_at"].replace("Z", "+00:00")).date().isoformat()
    return "acl-release-" + date + "." + str(int(request["main_pr"]))


def ensure_tag(request, ci_run, *, execute=run):
    tag = release_tag(request)
    sha = request["merge_sha"]
    ci = json.loads(execute("gh", "api", f"repos/{REPOSITORY}/actions/runs/{int(ci_run)}"))
    if (ci_run != request["ci_run"] or ci.get("head_sha") != sha or ci.get("head_branch") != "main"
            or ci.get("path") != ".github/workflows/ci.yml" or ci.get("event") not in {"push", "workflow_dispatch"}
            or ci.get("status") != "completed" or ci.get("conclusion") != "success"):
        raise ValueError("The release tag needs its exact successful native main CI run")
    if execute("git", "rev-parse", "HEAD") != sha or execute("git", "rev-parse", "origin/main") != sha:
        raise ValueError("Release continuation requires the exact current main checkout")
    annotation = json.dumps({"batch_id": request["batch_id"], "issues": request["issues"],
                             "target": request["target"], "policy": request["policy"]}, sort_keys=True)
    note = f"CI success https://github.com/{REPOSITORY}/actions/runs/{int(ci_run)}\n{annotation}"
    refs = execute("git", "ls-remote", "origin", "refs/tags/" + tag)
    if refs:
        execute("git", "fetch", "origin", "refs/tags/" + tag + ":refs/tags/" + tag)
        if (execute("git", "cat-file", "-t", "refs/tags/" + tag) != "tag"
                or execute("git", "rev-parse", tag + "^{commit}") != sha
                or annotation not in execute("git", "cat-file", "-p", "refs/tags/" + tag)):
            raise ValueError("An immutable release tag already exists with another identity")
    else:
        local = execute("git", "tag", "--list", tag)
        if local:
            if (execute("git", "cat-file", "-t", "refs/tags/" + tag) != "tag"
                    or execute("git", "rev-parse", tag + "^{commit}") != sha
                    or annotation not in execute("git", "cat-file", "-p", "refs/tags/" + tag)):
                raise ValueError("Inspect the retained local tag from an uncertain publication")
        else:
            execute("git", "-c", "user.name=ACL delivery", "-c", "user.email=acl-delivery@users.noreply.github.com",
                    "-c", "tag.gpgsign=false", "tag", "-a", tag, sha, "-m", note)
        execute("git", "push", "origin", "refs/tags/" + tag + ":refs/tags/" + tag)
    return tag


def published(tag, repo, *, execute=run):
    # An API outage is never interpreted as a missing release.
    rows = json.loads(execute("gh", "release", "list", "--repo", repo, "--limit", "1000", "--json", "tagName,isDraft,isPrerelease"))
    matches = [r for r in rows if r["tagName"] == tag]
    if not matches:
        if any(not r["isDraft"] and not r["isPrerelease"] for r in rows):
            latest = json.loads(execute("gh", "release", "view", "--repo", repo, "--json", "tagName"))
            manifest = json.loads(execute("gh", "release", "download", latest["tagName"], "--repo", repo,
                                          "--pattern", "release.json", "--output", "-"))
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from src.web.release_updates import is_newer
            if not is_newer(tag.removeprefix("acl-release-"), manifest["version"]):
                raise ValueError("The requested version cannot replace a newer published update channel")
        return False
    if len(matches) != 1 or matches[0]["isDraft"] or matches[0]["isPrerelease"]:
        raise ValueError("An unfinished or ambiguous existing release needs inspection")
    raw = execute("gh", "release", "download", tag, "--repo", repo, "--pattern", "release.json", "--output", "-")
    manifest = json.loads(raw)
    if manifest.get("tag") != tag or manifest.get("commit") != execute("git", "rev-parse", "HEAD"):
        raise ValueError("Existing published release does not match the immutable candidate")
    return True  # Acceptance still downloads, verifies and installs actual bytes.


def producer_artifact(log, run_id):
    """Cloned successful jobs keep the original producer marker on partial retry."""
    names = []
    for line in log.splitlines():
        marker = "TRACKER_DELIVERY_ARTIFACT "
        if marker in line:
            try:
                item = json.loads(line.split(marker, 1)[1])
            except (ValueError, TypeError):
                continue
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and re.fullmatch(r"acl-delivery-evidence-" + str(int(run_id)) + r"-[1-9][0-9]*", name):
                names.append(name)
    if len(names) != 1:
        raise ValueError("A unique authentic native evidence artifact binding is required")
    return names[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("tag")
    tag.add_argument("--request", type=Path, required=True)
    tag.add_argument("--ci-run", type=int, required=True)
    existing = sub.add_parser("published")
    existing.add_argument("--tag", required=True)
    existing.add_argument("--repo", required=True)
    args = parser.parse_args()
    if args.command == "tag":
        print(ensure_tag(json.loads(args.request.read_text()), args.ci_run))
    else:
        print("true" if published(args.tag, args.repo) else "false")


if __name__ == "__main__":
    main()
