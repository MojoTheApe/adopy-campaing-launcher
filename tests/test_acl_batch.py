"""Actual Git assembly preserves approved tips and stops conflicts/foreign owners."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts import acl_batch


@pytest.fixture(params=[2, 3], ids=["v2", "v3-five-reviews"])
def fixture(tmp_path, monkeypatch, request):
    root = tmp_path / "checkout"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Independent source fixture")
    git("config", "user.email", "batch@example.invalid")
    policy = {"schema_version": request.param, "implementation_limit": 5,
              "review_limit": 5 if request.param == 3 else 1, "batch_limit": 5}
    (root / "tracker").mkdir()
    (root / "tracker/config.json").write_text(json.dumps({"execution_policy": policy,
        "delivery_policy": {"schema_version": 1, "staging": {"enabled": True, "branch": "stage", "target": "test stage"},
            "completion": "verified-stable-package", "required_checks": ["ACL verification"], "delivery_target": "test installer"},
        "project": {"repository": acl_batch.REPOSITORY, "base_branch": "main", "id_prefix": "ACL", "id_offset": 1000}}))
    (root / "app.py").write_text("base\n")
    git("add", "."); git("commit", "-qm", "base")
    baseline = git("rev-parse", "HEAD")
    git("branch", "stage")
    sources = []
    for n in (1, 2):
        branch = "codex/acl-" + str(1000+n) + "-source"
        git("checkout", "-qb", branch, baseline)
        (root / ("worker"+str(n)+".txt")).write_text("independent work " + str(n))
        git("add", "."); git("commit", "-qm", "worker " + str(n))
        sources.append({"issue": n, "pr": 10+n, "branch": branch, "head_sha": git("rev-parse", "HEAD")})
    git("checkout", "-q", "main")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(root), str(remote)], check=True)
    git("remote", "add", "origin", "https://github.com/" + acl_batch.REPOSITORY)
    real_git = acl_batch.git
    def local_fetch(where, *args):
        if args[:2] == ("fetch", "origin"):
            return real_git(where, "fetch", str(remote), *args[2:])
        if args[:3] == ("ls-remote", "--heads", "origin"):
            return real_git(where, "ls-remote", "--heads", str(remote), *args[3:])
        return real_git(where, *args)
    monkeypatch.setattr(acl_batch, "git", local_fetch)
    batch = {"id": "a"*32, "actor": "Coordinator-TEST", "session": "coordinator_test", "state": "assembling",
        "branch": "codex/batch-" + "a"*32, "main_sha": baseline, "issues": [1,2], "members": sources, "pr": None}
    section = {"policy": policy, "integration": batch, "review": None}
    if request.param == 3:
        section["source_reviews"] = {str(100+n): {"id": str(n) * 32, "actor": "Reviewer-POOL" + str(n),
            "session": "source_review_" + str(n), "pr": 100+n, "issues": [100+n]} for n in range(1, 6)}
    prs = {m["pr"]: {"state": "open", "merged": False, "draft": False,
        "head": {"sha": m["head_sha"], "ref": m["branch"], "repo": {"full_name": acl_batch.REPOSITORY}}} for m in sources}
    def request(path):
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": subprocess.check_output(["git", "--git-dir", str(remote),
                "rev-parse", "refs/heads/" + path.removeprefix("/git/ref/heads/")], text=True).strip()}}
        return prs[int(path.rsplit("/",1)[1])]
    def assemble(**kwargs):
        destination = kwargs.pop("worktree", tmp_path / "integration")
        return acl_batch.prepare(root, destination, batch["actor"], batch["session"], batch["id"],
            read=lambda _: copy.deepcopy(section), request=request, **kwargs)
    return root, remote, section, prs, assemble


def test_real_two_author_assembly_preserves_exact_source_commits(fixture):
    root, _, section, _, assemble = fixture
    original = acl_batch.git(root, "rev-parse", "main")
    original_owners = copy.deepcopy(section.get("source_reviews"))
    plan = assemble()
    assert not plan["applied"] and "Tracker issues: ACL-1001, ACL-1002" in plan["body"]
    result = assemble(apply=True)
    worktree = Path(result["worktree"])
    assert result["applied"] and (worktree / "worker1.txt").is_file() and (worktree / "worker2.txt").is_file()
    assert acl_batch.git(root, "rev-parse", "main") == original
    for member in section["integration"]["members"]:
        subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", member["head_sha"], "HEAD"], check=True)
        assert acl_batch.git(root, "rev-parse", member["branch"]) == member["head_sha"]
    assert len(acl_batch.git(worktree, "rev-list", "--merges", original + "..HEAD").splitlines()) == 2
    assert section.get("source_reviews") == original_owners


def test_held_integration_review_still_blocks_assembly(fixture):
    root, _, section, _, assemble = fixture
    section["review"] = {"id": "b" * 32, "batch_id": section["integration"]["id"]}
    with pytest.raises(ValueError, match="Finish the active review"):
        assemble(apply=True)
    assert not (root.parent / "integration").exists()


def test_foreign_or_changed_claim_stops_before_any_worktree(fixture):
    root, _, section, _, assemble = fixture
    section["integration"]["session"] = "another_real_session"
    with pytest.raises(ValueError, match="owning Coordinator"):
        acl_batch.prepare(root, root.parent/"integration", "Coordinator-TEST", "coordinator_test", "a"*32,
                          read=lambda _:section, request=lambda _:None, apply=True)
    assert not (root.parent / "integration").exists()


def test_changed_source_does_not_assemble_stale_approval(fixture):
    root, _, _, prs, assemble = fixture
    prs[11]["head"]["sha"] = "b"*40
    with pytest.raises(ValueError, match="Source PR changed"):
        assemble(apply=True)
    assert not (root.parent / "integration").exists()


def test_conflict_aborts_only_own_merge_and_preserves_sources(fixture):
    root, remote, section, prs, assemble = fixture
    for member in section["integration"]["members"]:
        acl_batch.git(root, "checkout", member["branch"])
        (root / "app.py").write_text("different author " + str(member["issue"]) + "\n")
        acl_batch.git(root, "commit", "-am", "author change")
        member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
        prs[member["pr"]]["head"]["sha"] = member["head_sha"]
        acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    with pytest.raises(ValueError, match="Merge conflict"):
        assemble(apply=True)
    worktree = root.parent / "integration"
    assert worktree.is_dir() and not acl_batch.git(worktree, "status", "--porcelain")
    assert (worktree / "worker1.txt").is_file()
    assert not (worktree / "worker2.txt").exists()
    for member in section["integration"]["members"]:
        assert acl_batch.git(root, "rev-parse", member["branch"]) == member["head_sha"]
    assert section["integration"]["state"] == "assembling"


def refresh(fixture):
    root, remote, section, prs, assemble = fixture
    first = assemble(apply=True)
    batch = section["integration"]
    acl_batch.git(root, "push", str(remote), batch["branch"])
    batch["history"] = [{"members": copy.deepcopy(batch["members"]), "main_sha": batch["main_sha"], "pr": 30}]
    member = section["integration"]["members"][0]
    acl_batch.git(root, "checkout", member["branch"])
    (root / "author-fix.txt").write_text("fresh independently approved author change")
    acl_batch.git(root, "add", "."); acl_batch.git(root, "commit", "-m", "author fix")
    member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
    prs[member["pr"]]["head"]["sha"] = member["head_sha"]
    acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    return first


def publish_argv(result, remote):
    argv = result["replacement"]["push_argv"].copy()
    argv[argv.index("origin")] = str(remote)
    return argv


def test_refreshed_author_tip_rebuild_passes_exact_pinned_composition_and_preserves_history(fixture):
    root, remote, section, _, assemble = fixture
    first = refresh(fixture)
    batch = section["integration"]
    with pytest.raises(ValueError, match="outside the approved"):
        acl_batch.composition(root, first["head_sha"], batch["main_sha"], [m["head_sha"] for m in batch["members"]])
    result = assemble(apply=True, rebuild=True, previous_head=first["head_sha"], worktree=root.parent/"fresh")
    worktree = Path(result["worktree"])
    assert (worktree / "author-fix.txt").is_file()
    assert (worktree / "worker2.txt").is_file()
    acl_batch.composition(worktree, result["head_sha"], batch["main_sha"], [m["head_sha"] for m in batch["members"]])
    assert not acl_batch.git(worktree, "branch", "--show-current")
    assert acl_batch.git(root, "rev-parse", batch["branch"]) == first["head_sha"]
    assert acl_batch.git(Path(first["worktree"]), "rev-parse", "HEAD") == first["head_sha"]
    assert not acl_batch.git(Path(first["worktree"]), "status", "--porcelain")
    assert acl_batch.git(root, "rev-parse", result["replacement"]["local_evidence_ref"]) == first["head_sha"]
    subprocess.run(publish_argv(result, remote), check=True)
    def remote_sha(ref):
        return subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", ref], text=True).strip()
    assert remote_sha(batch["branch"]) == result["head_sha"]
    assert remote_sha(result["replacement"]["remote_evidence_ref"]) == first["head_sha"]
    assert remote_sha("main") == remote_sha("stage") == batch["main_sha"]
    for member in batch["members"]:
        assert remote_sha(member["branch"]) == member["head_sha"]


def test_atomic_replacement_refuses_changed_remote_and_does_not_publish_partial_backup(fixture):
    root, remote, section, _, assemble = fixture
    first = refresh(fixture)
    result = assemble(apply=True, rebuild=True, previous_head=first["head_sha"], worktree=root.parent/"fresh")
    batch = section["integration"]
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", "refs/heads/" + batch["branch"], batch["main_sha"]], check=True)
    assert subprocess.run(publish_argv(result, remote), capture_output=True).returncode != 0
    assert subprocess.run(["git", "--git-dir", str(remote), "show-ref", "--verify",
                           result["replacement"]["remote_evidence_ref"]], capture_output=True).returncode != 0
    assert acl_batch.git(root, "rev-parse", result["replacement"]["local_evidence_ref"]) == first["head_sha"]


def test_rebuild_refuses_unapproved_work_on_previous_integration_ref(fixture):
    root, remote, section, _, assemble = fixture
    first = refresh(fixture)
    old_worktree = Path(first["worktree"])
    (old_worktree / "unapproved.txt").write_text("implementation outside source approval")
    acl_batch.git(old_worktree, "add", ".")
    acl_batch.git(old_worktree, "commit", "-qm", "unapproved integration edit")
    previous = acl_batch.git(old_worktree, "rev-parse", "HEAD")
    acl_batch.git(old_worktree, "push", str(remote), section["integration"]["branch"])
    with pytest.raises(ValueError, match="outside the approved"):
        assemble(apply=True, rebuild=True, previous_head=previous, worktree=root.parent/"fresh")
    assert not (root.parent / "fresh").exists()
    assert (old_worktree / "unapproved.txt").is_file()


def test_second_rebuild_keeps_original_local_branch_and_archives_each_previous_remote_head(fixture):
    root, remote, section, prs, assemble = fixture
    first = refresh(fixture)
    second = assemble(apply=True, rebuild=True, previous_head=first["head_sha"], worktree=root.parent/"second")
    subprocess.run(publish_argv(second, remote), check=True)
    batch = section["integration"]
    batch["history"].append({"members": copy.deepcopy(batch["members"]), "main_sha": batch["main_sha"], "pr": 31})
    member = batch["members"][1]
    acl_batch.git(root, "checkout", member["branch"])
    (root / "second-fix.txt").write_text("another author independently approved fix")
    acl_batch.git(root, "add", "."); acl_batch.git(root, "commit", "-qm", "second fix")
    member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
    prs[member["pr"]]["head"]["sha"] = member["head_sha"]
    acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    third = assemble(apply=True, rebuild=True, previous_head=second["head_sha"], worktree=root.parent/"third")
    subprocess.run(publish_argv(third, remote), check=True)
    assert acl_batch.git(root, "rev-parse", batch["branch"]) == first["head_sha"]
    assert acl_batch.git(Path(second["worktree"]), "rev-parse", "HEAD") == second["head_sha"]
    assert (Path(third["worktree"]) / "second-fix.txt").is_file()
    for old, rebuilt in ((first, second), (second, third)):
        assert acl_batch.git(root, "rev-parse", rebuilt["replacement"]["local_evidence_ref"]) == old["head_sha"]
        assert subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse",
                                       rebuilt["replacement"]["remote_evidence_ref"]], text=True).strip() == old["head_sha"]


def test_unpublished_partial_candidate_rebuilds_after_author_reconciliation(fixture):
    root, remote, section, prs, assemble = fixture
    batch = section["integration"]
    for member in batch["members"]:
        acl_batch.git(root, "checkout", member["branch"])
        (root / "app.py").write_text("conflicting " + str(member["issue"]) + "\n")
        acl_batch.git(root, "commit", "-am", "original author change")
        member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
        prs[member["pr"]]["head"]["sha"] = member["head_sha"]
        acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    with pytest.raises(ValueError, match="Merge conflict"):
        assemble(apply=True)
    old = acl_batch.git(root.parent/"integration", "rev-parse", "HEAD")
    batch["history"] = [{"members": copy.deepcopy(batch["members"]), "main_sha": batch["main_sha"], "pr": None}]
    member = batch["members"][1]
    acl_batch.git(root, "checkout", member["branch"])
    (root / "app.py").write_text("conflicting 1\n")
    acl_batch.git(root, "commit", "-am", "author reconciles conflict")
    member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
    prs[member["pr"]]["head"]["sha"] = member["head_sha"]
    acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    result = assemble(apply=True, rebuild=True, previous_head=old, worktree=root.parent/"fresh")
    assert not result["replacement"]["previous_published"]
    subprocess.run(publish_argv(result, remote), check=True)
    assert acl_batch.git(root, "rev-parse", batch["branch"]) == old
    assert (Path(result["worktree"]) / "worker2.txt").is_file()


def test_failed_detached_rebuild_then_author_fix_refresh_retries_unchanged_published_candidate(fixture):
    root, remote, section, prs, assemble = fixture
    first = refresh(fixture)
    batch = section["integration"]
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", "refs/heads/stage", first["head_sha"]], check=True)
    for member in batch["members"]:
        acl_batch.git(root, "checkout", member["branch"])
        (root / "app.py").write_text("conflicting " + str(member["issue"]) + "\n")
        acl_batch.git(root, "commit", "-am", "fresh approved author change")
        member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
        prs[member["pr"]]["head"]["sha"] = member["head_sha"]
        acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    with pytest.raises(ValueError, match="Merge conflict"):
        assemble(apply=True, rebuild=True, previous_head=first["head_sha"], worktree=root.parent/"failed")
    failed_head = acl_batch.git(root.parent/"failed", "rev-parse", "HEAD")
    batch["history"].append({"members": copy.deepcopy(batch["members"]), "main_sha": batch["main_sha"], "pr": None})
    member = batch["members"][1]
    acl_batch.git(root, "checkout", member["branch"])
    (root / "app.py").write_text("conflicting 1\n")
    acl_batch.git(root, "commit", "-am", "author reconciles failed rebuild")
    member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
    prs[member["pr"]]["head"]["sha"] = member["head_sha"]
    acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    result = assemble(apply=True, rebuild=True, previous_head=first["head_sha"], worktree=root.parent/"retry")
    acl_batch.composition(Path(result["worktree"]), result["head_sha"], batch["main_sha"], [m["head_sha"] for m in batch["members"]])
    subprocess.run(publish_argv(result, remote), check=True)
    assert acl_batch.git(root.parent/"failed", "rev-parse", "HEAD") == failed_head
    assert not acl_batch.git(root.parent/"failed", "status", "--porcelain")
    assert subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "stage"], text=True).strip() == first["head_sha"]
    assert acl_batch.git(root, "rev-parse", "main") == batch["main_sha"]


@pytest.mark.parametrize("failure", ["wrong-head", "missing-history", "foreign-local-ref"])
def test_rebuild_requires_exact_previous_manifest_and_ref(fixture, failure):
    root, _, section, _, assemble = fixture
    first = refresh(fixture)
    previous = first["head_sha"]
    if failure == "wrong-head": previous = section["integration"]["main_sha"]
    if failure == "missing-history": section["integration"]["history"] = []
    if failure == "foreign-local-ref":
        acl_batch.git(Path(first["worktree"]), "checkout", "--detach")
        acl_batch.git(root, "update-ref", "refs/heads/" + section["integration"]["branch"], section["integration"]["main_sha"], previous)
    with pytest.raises(ValueError, match="Previous integration head|previous manifest|Local integration branch"):
        assemble(apply=True, rebuild=True, previous_head=previous, worktree=root.parent/"fresh")
    assert not (root.parent / "fresh").exists()


def test_rebuild_refuses_existing_worktree(fixture):
    root, _, section, prs, _ = fixture
    def request(path):
        if path.startswith('/git/'):
            return {"object":{"sha":section['integration']['main_sha']}}
        return prs[int(path.rsplit('/',1)[1])]
    with pytest.raises(ValueError, match="new isolated worktree"):
        acl_batch.prepare(root, root, "Coordinator-TEST", "coordinator_test", "a"*32, rebuild=True, previous_head="b"*40,
            read=lambda _:section, request=request)
