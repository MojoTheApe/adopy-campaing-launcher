#!/usr/bin/env python3
"""Assemble an owned frozen Tracker batch locally; never merge, publish or deploy."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

try:
    from scripts import tracker_batch_policy as proof
    from scripts import tracker_delivery_receipt as receipt
except ModuleNotFoundError:
    import tracker_batch_policy as proof
    import tracker_delivery_receipt as receipt

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "MojoTheApe/adopy-campaing-launcher"
SHA = re.compile(r"[0-9a-f]{40}")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def merge_source(worktree, head):
    """Prepare one new merge and preserve its primary error and owned evidence."""
    def run(*args):
        return subprocess.run(["git", "-C", str(worktree), *args], text=True,
                              capture_output=True, check=True)

    def diagnostic(error):
        return (f"command={error.cmd!r}; exit={error.returncode}; "
                f"stdout={error.stdout!r}; stderr={error.stderr!r}")

    # Re-match ordinary cached stat data; genuine content/mode changes still fail.
    # A refresh failure must not start a merge or abort an unrelated operation.
    try:
        run("update-index", "--refresh")
    except subprocess.CalledProcessError as error:
        raise ValueError("Index refresh failed; worktree and graph retained. " + diagnostic(error)) from error
    try:
        run("-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-edit", head)
    except subprocess.CalledProcessError as error:
        conflict = False
        cleanup = "No abort attempted; unfinished merge state is unknown."
        try:
            marker = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
                                    text=True, capture_output=True)
            if marker.returncode == 1 and not marker.stdout:
                cleanup = "No unfinished merge marker observed; no abort attempted."
            elif marker.returncode == 0 and marker.stdout.strip() == head:
                # Only this exact newly invoked source merge may be aborted.
                try:
                    conflict = bool(run("ls-files", "--unmerged", "-z").stdout)
                    cleanup = ""
                except subprocess.CalledProcessError as probe:
                    cleanup = "Unmerged-index observation failed: " + diagnostic(probe) + ". "
                abort_allowed = True
                if not conflict:
                    # A refusing hook may change only cached stat metadata.
                    # Refresh genuine matching content; never discard edits or
                    # proceed after a failed/unknown refresh. Unmerged entries
                    # retain the ordinary conflict abort path instead.
                    try:
                        run("update-index", "--refresh")
                    except (OSError, subprocess.SubprocessError) as refresh:
                        abort_allowed = False
                        detail = diagnostic(refresh) if isinstance(refresh, subprocess.CalledProcessError) else repr(refresh)
                        cleanup += "Owned index refresh failed; abort not attempted; cleanup is incomplete: " + detail
                if abort_allowed:
                    try:
                        run("merge", "--abort")
                        cleanup += "Owned unfinished merge aborted."
                    except (OSError, subprocess.SubprocessError) as abort:
                        detail = diagnostic(abort) if isinstance(abort, subprocess.CalledProcessError) else repr(abort)
                        cleanup += "Owned merge abort failed; cleanup is incomplete: " + detail
            else:
                cleanup += (f" Merge-marker observation: exit={marker.returncode}; "
                            f"stdout={marker.stdout!r}; stderr={marker.stderr!r}")
        except (OSError, subprocess.SubprocessError) as probe:
            cleanup += " Merge-state observation failed: " + repr(probe)
        reason = ("Merge conflict: return the affected author; do not resolve implementation changes in the batch branch. "
                  if conflict else "Merge failed; source-content conflict is not established. ")
        raise ValueError(reason + "Worktree and prior graph retained. Primary failure: "
                         + diagnostic(error) + ". " + cleanup) from error


def github(path):
    return json.loads(subprocess.check_output(["gh", "api", "repos/" + REPOSITORY + path], text=True))


def execution(root):
    result = subprocess.check_output([sys.executable, str(root / "scripts/acl_task_tracker.py"),
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


def local_objects(root):
    """Supply complete immutable Git objects to the unchanged native proofs."""
    cache = {}

    def raw(*args):
        return subprocess.check_output(["git", "--no-replace-objects", "-C", str(root), *args])

    def request(path):
        if path in cache:
            return cache[path]
        prefix, separator, suffix = path.partition("/git/")
        if not separator or prefix not in ("local", "repos/" + REPOSITORY):
            raise ValueError("Unsupported local Git proof request")
        kind, separator, revision = suffix.partition("/")
        recursive = revision.endswith("?recursive=1")
        sha = revision.removesuffix("?recursive=1")
        if (not separator or not SHA.fullmatch(sha)
                or recursive != (kind == "trees")):
            raise ValueError("Local Git proof needs exact immutable object IDs")
        if kind == "commits":
            headers, _, message = raw("cat-file", "commit", sha).partition(b"\n\n")
            rows = headers.splitlines()
            trees = [row[5:].decode("ascii") for row in rows if row.startswith(b"tree ")]
            if len(trees) != 1 or not SHA.fullmatch(trees[0]):
                raise ValueError("Incomplete local Git commit")
            value = {"sha": sha, "tree": {"sha": trees[0]},
                     "parents": [{"sha": row[7:].decode("ascii")} for row in rows if row.startswith(b"parent ")],
                     "message": message.decode("utf-8")}
        elif kind == "trees":
            if raw("cat-file", "-t", sha) != b"tree\n":
                raise ValueError("Local tree proof needs an actual Git tree")
            rows = raw("ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0")
            if len(rows) > 20001:
                raise ValueError("Local Git tree exceeds the bounded batch proof")
            entries = []
            for row in rows:
                if not row:
                    continue
                metadata, path = row.split(b"\t", 1)
                mode, entry_type, object_sha = metadata.decode("ascii").split()
                entries.append({"path": path.decode("utf-8"), "mode": mode,
                                "type": entry_type, "sha": object_sha})
            value = {"sha": sha, "truncated": False, "tree": entries}
        elif kind == "blobs":
            size = int(raw("cat-file", "-s", sha))
            if size > 1024 * 1024:
                raise ValueError("Local Git blob exceeds the native content bound")
            data = raw("cat-file", "blob", sha)
            value = {"sha": sha, "encoding": "base64", "size": size,
                     "content": base64.b64encode(data).decode("ascii")}
        else:
            raise ValueError("Unsupported local Git object type")
        cache[path] = value
        return value
    return request


def composition(root, head, baseline, sources, *, prefix=None):
    if prefix is None:
        prefix = json.loads((Path(root) / "tracker/config.json").read_text())["project"]["id_prefix"]
    return proof.composition(local_objects(root), "local", head, baseline, sources, prefix=prefix)


def selection_package(request, batch, config, head, before, after):
    """Recognize only the native singleton selection; suspected invalid ones stop."""
    process = proof.rules.process_module()

    def document(files, path, *, optional=False):
        row = files.get(path)
        if row is None and optional:
            return {}
        if not row or row["mode"] != "100644" or row["type"] != "blob":
            raise ValueError("Selection inspection requires existing regular governance files")
        value = json.loads(process.blob(request, "local", row["sha"]),
                           object_pairs_hook=proof.rules.direct_module().unique_object)
        if not isinstance(value, dict):
            raise ValueError("Selection inspection requires unambiguous JSON objects")
        return value

    prior, selected = [document(files, "tracker/config.json") for files in (before, after)]
    descriptors = [document(files, "delivery.json", optional=True) for files in (before, after)]
    templates = [document(files, ".workflow/template-base.json", optional=True) for files in (before, after)]

    def pristine(template, path):
        files = template.get("files", {})
        if not isinstance(files, dict):
            raise ValueError("Invalid retained pristine file mapping")
        if path not in files:
            return {}
        value = json.loads(files[path], object_pairs_hook=proof.rules.direct_module().unique_object)
        if not isinstance(value, dict):
            raise ValueError("Pristine selection JSON must be an unambiguous object")
        return value

    pristine_configs = [pristine(template, "tracker/config.json") for template in templates]
    pristine_descriptors = [pristine(template, "delivery.json") for template in templates]
    suspected = set()
    for key, (_, mirror, _) in receipt.SELECTIONS.items():
        for old, new, field in ((prior, selected, key),
                                (descriptors[0].get("workflow", {}), descriptors[1].get("workflow", {}), mirror),
                                (templates[0].get("inputs", {}), templates[1].get("inputs", {}), mirror),
                                (pristine_configs[0], pristine_configs[1], key),
                                (pristine_descriptors[0].get("workflow", {}), pristine_descriptors[1].get("workflow", {}), mirror)):
            if not isinstance(old, dict) or not isinstance(new, dict):
                raise ValueError("Invalid governance selection mapping")
            if receipt.digest([field in old, old.get(field)]) != receipt.digest([field in new, new.get(field)]):
                suspected.add(key)
    if not suspected:
        return None
    if len(suspected) != 1 or len(batch["members"]) != 1:
        raise ValueError("A policy selection must be one complete singleton; mixed/multiple selections stop")
    key = next(iter(suspected))
    receipt.selection_config(prior)
    receipt.selection_config(selected)
    receipt.selection_delta(prior, selected, key)
    if receipt.digest(prior) != receipt.digest(config):
        raise ValueError("Selection must use the full actual prior Main config")
    member = batch["members"][0]
    if proof.rules.tree(request, "local", head) != proof.rules.tree(request, "local", member["head_sha"]):
        raise ValueError("Selection assembly must retain the exact approved source tree")
    receipt.selection_tree(request, REPOSITORY, batch["main_sha"], head,
                           prior, selected, key, issue=member["issue"])
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    if len(changed) != 6:
        raise ValueError("Selection needs its exact five governance files and its own new fragment")
    return {"mode": "selection-check", "policy": key, "issue": member["issue"],
            "source_head": member["head_sha"], "tree_sha": proof.rules.tree(request, "local", head)}


def fold_batch(destination, batch, config):
    """Fold once after all sources, or validate a proven strict selection only."""
    head = git(destination, "rev-parse", "HEAD")
    request = local_objects(destination)
    # The pre-fold graph must contain every frozen tip and no extra commit.
    proof.composition(request, "local", head, batch["main_sha"],
                      [member["head_sha"] for member in batch["members"]])
    process = proof.rules.process_module()
    before, current = [process.entries(request, "local", proof.rules.tree(request, "local", sha))
                       for sha in (batch["main_sha"], head)]
    selection = selection_package(request, batch, config, head, before, current)
    directory, history = "docs/system/changelog.d/", "docs/system/CHANGELOG.md"
    fragments = sorted(path for path in current if path.startswith(directory)
                       and "/" not in path[len(directory):] and path.endswith(".md")
                       and path[len(directory):].lower() != "readme.md")
    if len(fragments) > 512:
        raise ValueError("Changelog fold exceeds the bounded complete fragment set")
    if git(destination, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Fold needs a clean assembled worktree; existing changes are preserved")
    for name in ("docs", "docs/system", directory.rstrip("/")):
        path = destination / name
        if path.is_symlink() or path.exists() and not path.is_dir():
            raise ValueError("Changelog paths must remain real directories")
    folder = destination / directory
    actual = {p.relative_to(destination).as_posix() for p in folder.glob("*.md")
              if p.name.lower() != "readme.md"}
    if actual != set(fragments):
        raise ValueError("Fold cannot consume untracked or missing fragments")
    total = 0
    for name in fragments + ([history] if fragments else []):
        row, path = current.get(name), destination / name
        if (not row or row["mode"] != "100644" or row["type"] != "blob"
                or path.is_symlink() or not path.is_file()):
            raise ValueError("Changelog fold requires normal tracked regular files")
        data = process.blob(request, "local", row["sha"]).encode("utf-8")
        total += len(data)
        if total > 8 * 1024 * 1024 or path.read_bytes() != data:
            raise ValueError("Fold source differs from its bounded immutable tree")
    # Run this reviewed tool checkout's producer, never a source-supplied script.
    program = Path(__file__).with_name("fold_changelog.py")
    command = [sys.executable, str(program), "--root", str(destination)]
    if selection:
        command.append("--check")
    try:
        subprocess.run(command, text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as error:
        raise ValueError("Changelog generation failed; worktree and prior graph retained. "
                         f"command={error.cmd!r}; exit={error.returncode}; "
                         f"stdout={error.stdout!r}; stderr={error.stderr!r}") from error
    if selection:
        if (git(destination, "rev-parse", "HEAD") != head
                or git(destination, "rev-parse", "HEAD^{tree}") != selection["tree_sha"]
                or git(destination, "status", "--porcelain", "--untracked-files=all")):
            raise ValueError("Selection validation changed the exact approved tree; preserve evidence")
        return selection
    if not fragments:
        return None
    git(destination, "add", "--", history, *fragments)
    git(destination, "-c", "commit.gpgsign=false", "commit", "-m",
        "Fold batch changelog fragments\n\nTracker changelog fold: 1")
    folded = composition(destination, git(destination, "rev-parse", "HEAD"), batch["main_sha"],
                         [member["head_sha"] for member in batch["members"]],
                         prefix=config["project"]["id_prefix"])
    if not folded or folded["parent_sha"] != head or git(destination, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Fold is not the sole clean terminal generated commit; preserve the worktree")
    return {"mode": "terminal-fold", **folded,
            "producer_sha256": hashlib.sha256(program.read_bytes()).hexdigest()}


def remote_head(root, branch):
    rows = git(root, "ls-remote", "--heads", "origin", "refs/heads/" + branch).splitlines()
    if len(rows) > 1:
        raise ValueError("Generated integration ref is ambiguous")
    return rows[0].split()[0] if rows else None


def previous_candidate(root, batch, previous_head, published, *, prefix=None):
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
            composition(root, previous_head, old["main_sha"], included, prefix=prefix)
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
        previous_candidate(root, batch, previous_head, published, prefix=config["project"]["id_prefix"])
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
        merge_source(destination, member["head_sha"])
    if owned_batch(read(root), config, actor, session, claim) != batch:
        raise ValueError("Batch changed during assembly; preserve the local worktree and inspect")
    validate_remote(batch, request)
    if rebuild:
        previous_candidate(root, batch, previous_head, published, prefix=config["project"]["id_prefix"])
    fold = fold_batch(destination, batch, config)
    if owned_batch(read(root), config, actor, session, claim) != batch:
        raise ValueError("Batch changed during folding; preserve the worktree and inspect")
    validate_remote(batch, request)
    composition(destination, git(destination, "rev-parse", "HEAD"), batch["main_sha"],
                [m["head_sha"] for m in batch["members"]], prefix=config["project"]["id_prefix"])
    result["changelog"] = fold
    if fold and fold["mode"] == "terminal-fold":
        result["body"] += "\nChangelog fold: " + fold["head_sha"] + "\nIndependent combined review must include this exact line.\n"
    elif fold:
        result["body"] += "\nCanonical singleton policy selection: check-only changelog validation; exact source tree retained.\n"
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
