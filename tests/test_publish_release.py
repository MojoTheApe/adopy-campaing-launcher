import json

import pytest

from scripts import acl_publish_release as pub


@pytest.fixture
def promotion(monkeypatch):
    state = {"type": "tag", "head": "a" * 40, "master": "a" * 40,
             "note": "success https://github.com/test/source/actions/runs/42",
             "run": {"head_sha": "a" * 40, "status": "completed", "conclusion": "success",
                     "path": ".github/workflows/ci.yml", "head_branch": "master", "event": "push"}}
    def git(*args):
        if args[:2] == ("cat-file", "-t"): return state["type"]
        if args[:2] == ("cat-file", "-p"): return state["note"]
        if args == ("rev-parse", "origin/master"): return state["master"]
        if args == ("rev-parse", "HEAD"): return state["head"]
        return "a" * 40
    monkeypatch.setattr(pub, "_git", git)
    monkeypatch.setattr(pub, "_gh", lambda *args: json.dumps(state["run"]))
    return state


def test_verified_master_promotion(promotion):
    assert pub.verify_promotion("acl-release-2026-09-19", "test/source") == "a" * 40


@pytest.mark.parametrize("key,value", [("type", "commit"), ("master", "b" * 40),
    ("head", "b" * 40), ("note", "success https://github.com/other/repo/actions/runs/42")])
def test_invalid_promotion_is_refused(promotion, key, value):
    promotion[key] = value
    with pytest.raises(ValueError): pub.verify_promotion("acl-release-2026-09-19", "test/source")


@pytest.mark.parametrize("key,value", [("head_sha", "b" * 40), ("conclusion", "failure"),
    ("status", "queued"), ("path", ".github/workflows/tracker.yml"),
    ("head_branch", "feature"), ("event", "pull_request")])
def test_unrelated_or_failed_ci_is_refused(promotion, key, value):
    promotion["run"][key] = value
    with pytest.raises(ValueError): pub.verify_promotion("acl-release-2026-09-19", "test/source")


def test_existing_release_is_never_modified(tmp_path, monkeypatch):
    calls = []
    def gh(*args):
        calls.append(args)
        return '[{"tagName":"acl-release-test"}]'
    monkeypatch.setattr(pub, "_gh", gh)
    with pytest.raises(ValueError, match="already exists"):
        pub.publish("acl-release-test", "test/releases", tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize("upload_fails", [False, True])
def test_release_only_becomes_visible_after_all_assets_upload(tmp_path, monkeypatch, upload_fails):
    (tmp_path / "release.json").write_text(json.dumps({"tag": "acl-release-test", "version": "test",
                                                     "installer": {"name": "Setup.exe"}}))
    for name in ["Setup.exe", "Setup.exe.sha256", "release-notes.md"]:
        (tmp_path / name).write_text("test")
    calls = []
    def gh(*args):
        calls.append(args)
        if args[:2] == ("release", "create") and upload_fails: raise RuntimeError("upload failed")
        return "[]"
    monkeypatch.setattr(pub, "_gh", gh)
    if upload_fails:
        with pytest.raises(RuntimeError): pub.publish("acl-release-test", "test/releases", tmp_path)
        assert len(calls) == 2
    else:
        pub.publish("acl-release-test", "test/releases", tmp_path)
        assert "--draft" in calls[1] and "--draft=false" in calls[2]
        assert "--clobber" not in str(calls)
