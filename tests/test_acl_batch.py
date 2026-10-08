"""Actual Git assembly preserves approved tips and stops conflicts/foreign owners."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts import acl_batch


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Independent source fixture")
    git("config", "user.email", "batch@example.invalid")
    policy = {"schema_version": 2, "implementation_limit": 5, "review_limit": 1, "batch_limit": 5}
    (root / "tracker").mkdir()
    (root / "tracker/config.json").write_text(json.dumps({"execution_policy": policy,
        "project": {"repository": acl_batch.REPOSITORY, "id_prefix": "ACL", "id_offset": 1000}}))
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
        return real_git(where, *args)
    monkeypatch.setattr(acl_batch, "git", local_fetch)
    batch = {"id": "a"*32, "actor": "Coordinator-TEST", "session": "coordinator_test", "state": "assembling",
        "branch": "codex/batch-" + "a"*32, "main_sha": baseline, "issues": [1,2], "members": sources, "pr": None}
    section = {"policy": policy, "integration": batch, "review": None}
    prs = {m["pr"]: {"state": "open", "merged": False, "draft": False,
        "head": {"sha": m["head_sha"], "ref": m["branch"], "repo": {"full_name": acl_batch.REPOSITORY}}} for m in sources}
    def request(path):
        if path == "/git/ref/heads/main": return {"object": {"sha": baseline}}
        return prs[int(path.rsplit("/",1)[1])]
    def assemble(**kwargs):
        return acl_batch.prepare(root, tmp_path / "integration", batch["actor"], batch["session"], batch["id"],
            read=lambda _: copy.deepcopy(section), request=request, **kwargs)
    return root, remote, section, prs, assemble


def test_real_two_author_assembly_preserves_exact_source_commits(fixture):
    root, _, section, _, assemble = fixture
    original = acl_batch.git(root, "rev-parse", "main")
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


def test_refreshed_author_tip_appends_without_resetting_existing_batch(fixture):
    root, remote, section, prs, assemble = fixture
    first = assemble(apply=True)
    member = section["integration"]["members"][0]
    acl_batch.git(root, "checkout", member["branch"])
    (root / "author-fix.txt").write_text("fresh independently approved author change")
    acl_batch.git(root, "add", "."); acl_batch.git(root, "commit", "-m", "author fix")
    member["head_sha"] = acl_batch.git(root, "rev-parse", "HEAD")
    prs[member["pr"]]["head"]["sha"] = member["head_sha"]
    acl_batch.git(root, "push", str(remote), member["branch"])
    acl_batch.git(root, "checkout", "main")
    result = assemble(apply=True, resume=True)
    worktree = Path(result["worktree"])
    assert (worktree / "author-fix.txt").is_file()
    assert (worktree / "worker2.txt").is_file()
    subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", first["head_sha"], "HEAD"], check=True)


def test_resume_refuses_unrelated_existing_worktree(fixture):
    root, _, section, prs, _ = fixture
    def request(path):
        if path.startswith('/git/'):
            return {"object":{"sha":section['integration']['main_sha']}}
        return prs[int(path.rsplit('/',1)[1])]
    with pytest.raises(ValueError, match="owned batch branch"):
        acl_batch.prepare(root, root, "Coordinator-TEST", "coordinator_test", "a"*32, resume=True,
            read=lambda _:section, request=request)
