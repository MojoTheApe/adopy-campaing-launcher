"""Что знает о выпуске машина менеджера: release.json, контрольная сумма, заметки."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import acl_release_manifest as rm


def test_version_comes_from_the_release_tag():
    assert rm.version_of("acl-release-2026-09-20") == "2026-09-20"
    for bad in ("v1.2", "acl-release-", "release-2026"):
        with pytest.raises(ValueError):
            rm.version_of(bad)


def test_tracker_line_is_read_exactly_like_the_pr_check():
    assert rm.tracker_issues("Text\n\nTracker issues: ACL-1171, ACL-1192\n") == ["ACL-1171", "ACL-1192"]
    assert rm.tracker_issues("Tracker issues: #241") == ["ACL-1241"]
    assert rm.tracker_issues("Closes #241") == [], "no Tracker issues line — nothing is claimed as fixed"


def test_fixed_issues_group_pull_requests_and_keep_their_titles():
    pulls = [
        {"number": 238, "title": "Verified media reuse", "body": "Tracker issues: ACL-1171, ACL-1192"},
        {"number": 244, "title": "Team library", "body": "Tracker issues: ACL-1241"},
        {"number": 245, "title": "Media follow-up", "body": "Tracker issues: ACL-1192"},
        {"number": 199, "title": "Old PR", "body": "no line"},
    ]
    fixed = {e["issue"]: e for e in rm.fixed_issues(pulls)}
    assert sorted(fixed) == ["ACL-1171", "ACL-1192", "ACL-1241"]
    assert fixed["ACL-1192"]["pull_requests"] == [238, 245]
    assert fixed["ACL-1192"]["notes"] == ["Verified media reuse", "Media follow-up"]


def test_release_json_carries_what_the_update_check_needs(tmp_path):
    installer = tmp_path / "Adopy-Campaign-Launcher-Setup-2026-09-20.exe"
    installer.write_bytes(b"MZ" + b"0" * 1000)
    release = rm.build(tag="acl-release-2026-09-20", installer=installer, repo="adopy/acl-releases",
                       commit="abc123", previous="acl-release-2026-09-03e",
                       pulls=[{"number": 244, "title": "Team library", "body": "Tracker issues: ACL-1241"}])
    assert release["version"] == "2026-09-20" and release["previous_tag"] == "acl-release-2026-09-03e"
    assert release["installer"]["sha256"] == hashlib.sha256(installer.read_bytes()).hexdigest()
    assert release["installer"]["size"] == 1002
    assert release["installer"]["url"] == ("https://github.com/adopy/acl-releases/releases/download/"
                                           "acl-release-2026-09-20/Adopy-Campaign-Launcher-Setup-2026-09-20.exe")
    assert release["fixed"] == [{"issue": "ACL-1241", "pull_requests": [244], "notes": ["Team library"]}]
    text = rm.notes(release)
    assert "ACL-1241: Team library" in text and release["installer"]["sha256"] in text


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def test_the_script_writes_all_three_files_from_a_real_tag_range(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@example.invalid")
    (repo / "a").write_text("1")
    _git(repo, "add", "a")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "tag", "-a", "acl-release-2026-09-03e", "-m", "old")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "a").write_text("2")
    _git(repo, "commit", "-qam", "change")
    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #244 from x/feature")
    _git(repo, "tag", "-a", "acl-release-2026-09-20", "-m", "new")
    installer = tmp_path / "Setup.exe"
    installer.write_bytes(b"MZ")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(rm, "previous_published_release", lambda repo, tag: {"tag": "acl-release-2026-09-03e"})
    monkeypatch.setattr(rm, "_pull_request", lambda source, n: {
        "number": n, "title": "Team library", "body": "Tracker issues: ACL-1241"})
    out = tmp_path / "out"
    assert rm.main(["--tag", "acl-release-2026-09-20", "--installer", str(installer),
                    "--repo", "adopy/acl-releases", "--out", str(out)]) == 0
    release = json.loads((out / "release.json").read_text(encoding="utf-8"))
    assert release["previous_tag"] == "acl-release-2026-09-03e"
    assert release["commit"] == _git(repo, "rev-list", "-n", "1", "acl-release-2026-09-20")
    assert release["fixed"][0]["issue"] == "ACL-1241"
    assert (out / "Setup.exe.sha256").read_text(encoding="utf-8").endswith("  Setup.exe\n")
    assert "ACL-1241" in (out / "release-notes.md").read_text(encoding="utf-8")


def test_recovery_uses_real_stable_publication_and_preserves_orphan_tags(tmp_path, monkeypatch):
    from src.web import release_updates
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@example.invalid")
    (repo / "a").write_text("stable")
    _git(repo, "add", "a"); _git(repo, "commit", "-qm", "stable")
    _git(repo, "tag", "-a", "acl-release-stable", "-m", "published")
    stable_sha = _git(repo, "rev-parse", "HEAD")
    for name in ("orphan", "draft", "recovery"):
        (repo / "a").write_text(name)
        _git(repo, "commit", "-qam", name)
        _git(repo, "tag", "-a", "acl-release-" + name, "-m", name)
    before = _git(repo, "show-ref", "--tags")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ACL_RELEASES_READ_TOKEN", "synthetic-read-token")
    original = subprocess.check_output
    def execute(args, **kwargs):
        if args[:3] == ["gh", "release", "list"]:
            return json.dumps([{"tagName": "acl-release-draft", "isDraft": True, "isPrerelease": False},
                               {"tagName": "acl-release-stable", "isDraft": False, "isPrerelease": False}])
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "check_output", execute)
    monkeypatch.setattr(release_updates, "fetch_latest", lambda channel: (
        {"tag": "acl-release-stable", "commit": stable_sha},
        {"id": 42, "tag_name": "acl-release-stable", "html_url": "https://example.invalid/stable",
         "draft": False, "prerelease": False, "_acl_signature": {"status": "valid"}}))
    baseline = rm.previous_published_release("example/releases", "acl-release-recovery")
    assert baseline["tag"] == "acl-release-stable" and baseline["commit"] == stable_sha
    assert _git(repo, "show-ref", "--tags") == before
    installer = tmp_path / "Setup.exe"
    installer.write_bytes(b"MZ")
    monkeypatch.setattr(rm, "_pull_request", lambda source, number: {})
    out = tmp_path / "out"
    assert rm.main(["--tag", "acl-release-recovery", "--installer", str(installer),
                    "--repo", "example/releases", "--out", str(out)]) == 0
    result = json.loads((out / "release.json").read_text())
    assert result["previous_release"] == baseline and result["previous_tag"] == "acl-release-stable"
