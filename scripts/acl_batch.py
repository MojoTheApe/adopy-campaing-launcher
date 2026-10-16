#!/usr/bin/env python3
"""Assemble an owned frozen Tracker batch locally; never merge, publish or deploy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

try:
    from scripts import tracker_batch_policy as proof
except ModuleNotFoundError:
    import tracker_batch_policy as proof

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "MojoTheApe/adopy-campaing-launcher"
SHA = re.compile(r"[0-9a-f]{40}")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def github(path):
    return json.loads(subprocess.check_output(["gh", "api", "repos/" + REPOSITORY + path], text=True))


def execution(root):
    result = subprocess.check_output([sys.executable, str(root / "scripts/task_tracker.py"),
                                      "execution", "show"], cwd=root, text=True)
    return json.loads(result)["execution"]


def owned_batch(section, config, actor, session, claim):
    policy = proof.rules.execution_policy(config)
    batch = section.get("integration")
    if (not policy or policy.get("schema_version") not in (2, 3) or section.get("policy") != policy
            or not batch or (batch.get("actor"), batch.get("session"), batch.get("id")) != (actor, session, claim)
            or not re.fullmatch(r"[0-9a-f]{32}", claim)
            or batch.get("branch") != "codex/batch-" + claim
            or batch.get("state") != "assembling" or batch.get("pr") is not None
            or not SHA.fullmatch(batch.get("main_sha", ""))):
        raise ValueError("Inspect the active assembling batch and use its owning Coordinator claim")
    members = batch["members"]
    if (not members or len(members) > policy["batch_limit"]
            or [m["issue"] for m in members] != batch["issues"]
            or len(set(batch["issues"])) != len(members)
            or any(not SHA.fullmatch(m["head_sha"]) for m in members)):
        raise ValueError("Frozen batch membership or source revisions are invalid")
    if section.get("review"):
        raise ValueError("Finish the active review before assembling a candidate")
    return batch


def validate_remote(batch, request):
    if request("/git/ref/heads/main")["object"]["sha"] != batch["main_sha"]:
        raise ValueError("Main changed; refresh approvals and the held batch before rebuilding")
    for member in batch["members"]:
        pr = request("/pulls/" + str(member["pr"]))
        if (pr.get("state") != "open" or pr.get("merged") or pr.get("draft")
                or pr["head"]["sha"] != member["head_sha"] or pr["head"]["ref"] != member["branch"]
                or pr["head"]["repo"]["full_name"].lower() != REPOSITORY.lower()):
            raise ValueError("Source PR changed; retain the claim and return the affected author")


def composition(root, head, baseline, sources):
    def commit(path):
        sha = path.rsplit("/", 1)[1]
        return {"parents": [{"sha": p} for p in git(root, "show", "-s", "--format=%P", sha).split()],
                "message": git(root, "show", "-s", "--format=%B", sha)}
    proof.composition(commit, "local", head, baseline, sources)


def remote_head(root, branch):
    rows = git(root, "ls-remote", "--heads", "origin", "refs/heads/" + branch).splitlines()
    if len(rows) > 1:
        raise ValueError("Generated integration ref is ambiguous")
    return rows[0].split()[0] if rows else None


def previous_candidate(root, batch, previous_head, published):
    if remote_head(root, batch["branch"]) != (previous_head if published else None):
        raise ValueError("Previous integration head changed; inspect it before rebuilding")
    snapshots = [row for row in batch.get("history", []) if "members" in row and "main_sha" in row]
    if not snapshots:
        raise ValueError("Refresh the held batch before rebuilding; its previous manifest must be retained")
    for old in reversed(snapshots):
        if [m["issue"] for m in old["members"]] != batch["issues"]:
            continue
        # A failed detached build never changes the published ref. Its exact
        # prior composition may therefore match an earlier retained manifest.
        # An aborted initial assembly may contain only a subset; every visited
        # node must still be that manifest's baseline, tips or merge commits.
        included = [m["head_sha"] for m in old["members"] if subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", m["head_sha"], previous_head],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0]
        try:
            composition(root, previous_head, old["main_sha"], included)
        except ValueError:
            continue
        break
    else:
        raise ValueError("Previous candidate contains work outside the approved retained same-member manifests")
    local = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "--hash",
                            "refs/heads/" + batch["branch"]], text=True, capture_output=True)
    local_head = local.stdout.strip() if local.returncode == 0 else None
    if not published and local_head != previous_head:
        raise ValueError("Unpublished previous candidate must match the owned local integration branch")
    if local_head and local_head != previous_head:
        # After the first detached rebuild the original local branch deliberately
        # stays at its archived head. Unknown local drift remains an error.
        evidence = "refs/batch-evidence/" + batch["id"] + "/" + local_head
        archived = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "--hash", evidence],
                                  text=True, capture_output=True)
        if archived.returncode != 0 or archived.stdout.strip() != local_head:
            raise ValueError("Local integration branch differs without preserved evidence; inspect it before rebuilding")


def prepare(root, destination, actor, session, claim, *, apply=False, rebuild=False,
            previous_head=None, read=execution, request=github):
    root, destination = Path(root).resolve(), Path(destination).absolute()
    config = json.loads((root / "tracker/config.json").read_text())
    if config["project"]["repository"] != REPOSITORY:
        raise ValueError("Use the ACL checkout")
    batch = owned_batch(read(root), config, actor, session, claim)
    validate_remote(batch, request)
    remote = git(root, "remote", "get-url", "origin").removesuffix(".git")
    if remote not in ("https://github.com/" + REPOSITORY, "git@github.com:" + REPOSITORY):
        raise ValueError("Use the official ACL origin; do not copy historical push destinations")
    if bool(previous_head) != rebuild or rebuild and not SHA.fullmatch(previous_head):
        raise ValueError("Rebuild requires the explicit previous integration head")
    if destination.exists():
        raise ValueError("Use a new isolated worktree path; existing work is preserved")
    ids = [config["project"].get("legacy_task_ids", {}).get(str(n),
           config["project"]["id_prefix"] + "-" + str(n + config["project"].get("id_offset", 0))) for n in batch["issues"]]
    target = config["delivery_policy"]["staging"]["branch"] if config["delivery_policy"]["staging"]["enabled"] else config["project"]["base_branch"]
    result = {"applied": False, "branch": batch["branch"], "base": target, "main_sha": batch["main_sha"],
              "title": "[" + ", ".join(ids) + "] Integrate approved tasks",
              "body": "Tracker issues: " + ", ".join(ids) + "\nTracker batch: " + batch["id"] + "\n\n"
                      + "Frozen approved sources: " + ", ".join("#" + str(m["pr"]) for m in batch["members"])
                      + ". Independent integration review and complete applicable CI are required.\n"}
    if not apply:
        return result
    if git(root, "status", "--porcelain"):
        raise ValueError("Use a clean checkout before assembling the integration worktree")
    git(root, "fetch", "origin", "main", "stage")
    for member in batch["members"]:
        git(root, "fetch", "origin", member["branch"])
        if git(root, "rev-parse", "FETCH_HEAD") != member["head_sha"]:
            raise ValueError("Source branch changed while fetching; retain its history and refresh")
    if owned_batch(read(root), config, actor, session, claim) != batch:
        raise ValueError("Batch changed during preparation; inspect the current ownership")
    validate_remote(batch, request)
    if rebuild:
        published = remote_head(root, batch["branch"]) is not None
        if published:
            git(root, "fetch", "origin", batch["branch"])
            if git(root, "rev-parse", "FETCH_HEAD") != previous_head:
                raise ValueError("Previous integration head changed while fetching")
        previous_candidate(root, batch, previous_head, published)
        evidence = "refs/batch-evidence/" + claim + "/" + previous_head
        existing = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "--hash", evidence],
                                  text=True, capture_output=True)
        if existing.returncode == 0:
            if existing.stdout.strip() != previous_head:
                raise ValueError("Previous local evidence changed; inspect it before rebuilding")
        else:
            git(root, "update-ref", evidence, previous_head, "0" * 40)
        # A detached new candidate leaves the old branch, linked worktree and
        # index intact. Only an explicit leased publication may replace the
        # generated remote ref, atomically retaining remote evidence first.
        git(root, "worktree", "add", "--detach", str(destination), batch["main_sha"])
        remote_evidence = "refs/heads/codex/batch-evidence-" + claim + "-" + previous_head
        reserved = "refs/heads/" + batch["branch"]
        result["replacement"] = {"previous_head": previous_head, "previous_published": published, "local_evidence_ref": evidence,
            "remote_evidence_ref": remote_evidence, "push_argv": ["git", "-C", str(destination), "push", "--atomic",
            "--force-with-lease=" + reserved + ":" + (previous_head if published else ""),
            "--force-with-lease=" + remote_evidence + ":", "origin", evidence + ":" + remote_evidence,
            "HEAD:" + reserved]}
    else:
        git(root, "worktree", "add", "-b", batch["branch"], str(destination), batch["main_sha"])
    for member in batch["members"]:
        try:
            git(destination, "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-edit", member["head_sha"])
        except subprocess.CalledProcessError as error:
            # This helper owns this new worktree. Abort only its unfinished merge;
            # preserve prior assembled commits, source branches and the batch claim.
            git(destination, "merge", "--abort")
            raise ValueError("Merge conflict: the worktree is preserved. Return the affected author; "
                             "do not resolve implementation changes in the batch branch") from error
    if owned_batch(read(root), config, actor, session, claim) != batch:
        raise ValueError("Batch changed during assembly; preserve the local worktree and inspect")
    validate_remote(batch, request)
    if rebuild:
        previous_candidate(root, batch, previous_head, published)
    composition(destination, git(destination, "rev-parse", "HEAD"), batch["main_sha"],
                [m["head_sha"] for m in batch["members"]])
    result.update(applied=True, head_sha=git(destination, "rev-parse", "HEAD"),
                  tree=git(destination, "rev-parse", "HEAD^{tree}"), worktree=str(destination))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--claim", required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="Build a fresh detached candidate and preserve the previous generated ref")
    parser.add_argument("--previous-head", help="Exact old generated integration head; required with --rebuild")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(ROOT, args.worktree, args.actor, args.session, args.claim,
                                 apply=args.apply, rebuild=args.rebuild, previous_head=args.previous_head), indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("Batch assembly stopped: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
