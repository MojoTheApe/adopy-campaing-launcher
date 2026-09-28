"""The fleet updater accepts promoted releases, never arbitrary branches.

ACL-1357 (audit S06): the tag's text is not evidence. The CI run it links to
must be confirmed through the GitHub API — this repository, ci.yml, main, the
exact tagged commit, success. Every failure to confirm is a refusal.
"""
from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from scripts import acl_update_gate as gate

CANDIDATE = "b" * 40
RUN_URL = f"https://github.com/{gate.RELEASE_REPO}/actions/runs/12345"


def _annotation(body: str, candidate: str = CANDIDATE) -> str:
    return f"object {candidate}\ntype commit\ntag acl-release-2026-09-01\n\n{body}\n"


def _good_run(**overrides) -> dict:
    run = {
        "id": 12345,
        "repository": {"full_name": gate.RELEASE_REPO},
        "path": ".github/workflows/ci.yml",
        "head_sha": CANDIDATE,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    run.update(overrides)
    return run


def _install_fake_git(monkeypatch, *, head="a" * 40, candidate=CANDIDATE, dirty="", tag_type="tag",
                      annotation=None, ancestor_rc=0):
    annotation = annotation or _annotation(f"Build success\n{RUN_URL}", candidate)

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


@pytest.fixture
def api(monkeypatch):
    """A fake GitHub Actions API: run id -> run object (or an exception)."""
    runs: dict[str, object] = {"12345": _good_run()}
    calls: list[str] = []

    def fake_fetch(run_id):
        calls.append(run_id)
        answer = runs.get(run_id, gate.CiUnverified("could not be verified: GitHub answered HTTP 404"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gate, "fetch_run", fake_fetch)
    return SimpleNamespace(runs=runs, calls=calls)


def test_promoted_release_with_api_verified_ci_is_accepted(monkeypatch, api) -> None:
    _install_fake_git(monkeypatch)

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is True
    assert "acl-release-" in detail and "verified" in detail
    assert api.calls == ["12345"]


def test_fabricated_ci_link_is_rejected(monkeypatch, api) -> None:
    # Текст «success» и ссылка правильного вида на несуществующий прогон.
    _install_fake_git(monkeypatch, annotation=_annotation(
        f"CI success\nhttps://github.com/{gate.RELEASE_REPO}/actions/runs/99999999"))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "99999999" in detail and "HTTP 404" in detail


@pytest.mark.parametrize("field,value,reason", [
    ("conclusion", "failure", "did not succeed"),
    ("conclusion", "cancelled", "did not succeed"),
    ("status", "in_progress", "has not completed"),
    ("repository", {"full_name": "attacker/fork"}, "does not belong"),
    ("head_sha", "c" * 40, "tagged commit"),
    ("path", ".github/workflows/tracker-link.yml", "workflow"),
    ("head_branch", "feature/x", "did not run on main"),
    ("event", "pull_request", "push/dispatch"),
    ("id", 777, "not the run the tag links to"),
])
def test_api_result_that_does_not_prove_the_release_is_rejected(monkeypatch, api, field, value, reason) -> None:
    api.runs["12345"] = _good_run(**{field: value})
    _install_fake_git(monkeypatch)

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert reason in detail


def test_link_to_another_repository_is_rejected_without_asking_the_api(monkeypatch, api) -> None:
    _install_fake_git(monkeypatch, annotation=_annotation(
        "success https://github.com/org/repo/actions/runs/12345"))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "another repository" in detail
    assert api.calls == []


def test_tag_without_ci_url_is_rejected(monkeypatch, api) -> None:
    _install_fake_git(monkeypatch, annotation=_annotation("Looks good, success"))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "CI" in detail
    assert api.calls == []


def test_any_accepting_run_among_several_links_is_enough_but_each_is_checked(monkeypatch, api) -> None:
    api.runs["11111"] = _good_run(id=11111, conclusion="failure")
    _install_fake_git(monkeypatch, annotation=_annotation(
        f"https://github.com/{gate.RELEASE_REPO}/actions/runs/11111\n{RUN_URL}"))

    ok, _ = gate.validate_candidate("origin/main")

    assert ok is True
    assert api.calls == ["11111", "12345"]


def test_missing_token_is_not_verified(monkeypatch) -> None:
    _install_fake_git(monkeypatch)
    monkeypatch.setattr(gate, "github_token", lambda: "")
    monkeypatch.setattr(gate.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request without a token")))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "no GitHub credential" in detail


@pytest.mark.parametrize("error", [
    urllib.error.URLError("offline"),
    urllib.error.HTTPError(RUN_URL, 403, "Forbidden", {}, io.BytesIO(b"")),
    urllib.error.HTTPError(RUN_URL, 500, "Server Error", {}, io.BytesIO(b"")),
    TimeoutError("slow"),
])
def test_api_failure_is_not_success(monkeypatch, error) -> None:
    _install_fake_git(monkeypatch)
    monkeypatch.setattr(gate, "github_token", lambda: "tok-SECRET-value")

    def boom(*_a, **_k):
        raise error

    monkeypatch.setattr(gate.urllib.request, "urlopen", boom)

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "could not be verified" in detail
    assert "tok-SECRET-value" not in detail


def test_real_fetch_sends_token_only_to_the_pinned_repository(monkeypatch) -> None:
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return Response(json.dumps(_good_run()).encode())

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)

    run = gate.fetch_run("12345", token="tok")

    assert run["conclusion"] == "success"
    assert seen["url"] == f"https://api.github.com/repos/{gate.RELEASE_REPO}/actions/runs/12345"
    assert seen["auth"] == "Bearer tok"


def test_non_numeric_run_id_never_reaches_the_api(monkeypatch) -> None:
    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(gate.CiUnverified):
        gate.fetch_run("../../../evil", token="tok")


def test_token_sources_never_prompt(monkeypatch) -> None:
    for name in ("ACL_UPDATE_GATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="protocol=https\nhost=github.com\nusername=x\npassword=stored-token\n")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    assert gate.github_token() == "stored-token"
    assert seen["cmd"] == ["git", "credential", "fill"]
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GCM_INTERACTIVE"] == "never"

    monkeypatch.setenv("ACL_UPDATE_GATE_TOKEN", "explicit")
    assert gate.github_token() == "explicit"


def test_no_stored_credential_and_no_env_means_no_token(monkeypatch) -> None:
    for name in ("ACL_UPDATE_GATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=128, stdout="", stderr="terminal prompts disabled"))

    assert gate.github_token() == ""


def test_arbitrary_branch_is_rejected_before_git(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_out", lambda *_args: (_ for _ in ()).throw(AssertionError()))

    ok, detail = gate.validate_candidate("origin/feature/experiment")

    assert ok is False
    assert "origin/main" in detail


def test_lightweight_tag_is_not_a_release(monkeypatch, api) -> None:
    _install_fake_git(monkeypatch, tag_type="commit")

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "lightweight" in detail


def test_tag_on_another_commit_is_rejected(monkeypatch, api) -> None:
    _install_fake_git(monkeypatch, annotation=_annotation(f"success\n{RUN_URL}", "d" * 40))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "does not annotate" in detail


def test_non_fast_forward_is_rejected(monkeypatch, api) -> None:
    _install_fake_git(monkeypatch, ancestor_rc=1)

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "not a fast-forward" in detail


def test_enrollment_requires_exact_stable_commit(monkeypatch, api) -> None:
    _install_fake_git(monkeypatch)

    ok, detail = gate.validate_candidate("origin/main", enrolling=True)

    assert ok is False
    assert "equal origin/main" in detail
