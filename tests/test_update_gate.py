"""The fleet updater accepts promoted releases, never arbitrary branches."""
from __future__ import annotations

from types import SimpleNamespace

from scripts import acl_update_gate as gate


def _install_fake_git(monkeypatch, *, head="a" * 40, candidate="b" * 40, dirty="", tag_type="tag", annotation=None, ancestor_rc=0):
    annotation = annotation or (
        f"object {candidate}\ntype commit\ntag acl-release-2026-09-01\n\n"
        "Build success\nhttps://github.com/org/repo/actions/runs/12345\n"
    )

    def fake_out(*args):
        if args[:2] == ("rev-parse", "HEAD"):
            return head
        if args[:2] == ("rev-parse", f"{gate.STABLE_REF}^{{commit}}"):
            return candidate
        if args[:2] == ("status", "--porcelain"):
            return dirty
        if args[:2] == ("tag", "--points-at"):
            return "acl-release-2026-09-01"
        if args[:2] == ("cat-file", "-t"):
            return tag_type
        if args[:2] == ("cat-file", "-p"):
            return annotation
        return ""

    def fake_run(*args):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return SimpleNamespace(returncode=ancestor_rc, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "_out", fake_out)
    monkeypatch.setattr(gate, "_run", fake_run)


def test_promoted_annotated_fast_forward_is_accepted(monkeypatch) -> None:
    _install_fake_git(monkeypatch)

    ok, detail = gate.validate_candidate("origin/master")

    assert ok is True
    assert "acl-release-" in detail


def test_arbitrary_branch_is_rejected_before_git(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_out", lambda *_args: (_ for _ in ()).throw(AssertionError()))

    ok, detail = gate.validate_candidate("origin/feature/experiment")

    assert ok is False
    assert "origin/master" in detail


def test_lightweight_tag_is_not_a_release(monkeypatch) -> None:
    _install_fake_git(monkeypatch, tag_type="commit")

    ok, detail = gate.validate_candidate("origin/master")

    assert ok is False
    assert "lightweight" in detail


def test_tag_without_successful_ci_url_is_rejected(monkeypatch) -> None:
    candidate = "b" * 40
    _install_fake_git(
        monkeypatch,
        candidate=candidate,
        annotation=f"object {candidate}\ntype commit\ntag acl-release-bad\n\nLooks good\n",
    )

    ok, detail = gate.validate_candidate("origin/master")

    assert ok is False
    assert "CI" in detail


def test_non_fast_forward_is_rejected(monkeypatch) -> None:
    _install_fake_git(monkeypatch, ancestor_rc=1)

    ok, detail = gate.validate_candidate("origin/master")

    assert ok is False
    assert "not a fast-forward" in detail


def test_enrollment_requires_exact_stable_commit(monkeypatch) -> None:
    _install_fake_git(monkeypatch)

    ok, detail = gate.validate_candidate("origin/master", enrolling=True)

    assert ok is False
    assert "equal origin/master" in detail
