"""The fleet updater accepts promoted releases, never arbitrary branches.

ACL-1357 (audit S06): the tag's text is not evidence when the GitHub API can
answer. A definite negative from the API (the run does not prove the release,
or does not exist while the repository is visible) is always a refusal. When
there is nothing to check with (no token, 401/403, network, repository not
visible), warn mode falls back to the tag's own record and reports it; enforce
mode refuses.
"""
from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import urllib.error
from types import SimpleNamespace

import pytest

from scripts import acl_update_gate as gate

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(
    reason="Install ACL.bat carries its own copy of this gate; the bootstrap contract is Windows-only")

# Отгружаемая стадия гейта — до того, как autouse-фикстура warn_stage её подменит.
_SHIPPED = gate.GATE_ENFORCE

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


@pytest.fixture(autouse=True)
def warn_stage(monkeypatch):
    """Today's stage, no real telemetry, no real machine .env."""
    monkeypatch.setattr(gate, "GATE_ENFORCE", False)
    monkeypatch.delenv(gate.GATE_MODE_ENV, raising=False)
    monkeypatch.setattr(gate, "_reported", set())
    monkeypatch.setattr(gate, "_machine_env_token", lambda: "")
    reports: list[tuple] = []
    monkeypatch.setattr(gate, "report_unverified", lambda *a: reports.append(a))
    return reports


@pytest.fixture
def api(monkeypatch):
    """A fake GitHub Actions API: run id -> run object (or an exception)."""
    runs: dict[str, object] = {"12345": _good_run()}
    calls: list[str] = []

    def fake_fetch(run_id):
        calls.append(run_id)
        answer = runs.get(run_id, gate.CiRejected(f"does not exist in {gate.RELEASE_REPO} (not a real run)"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gate, "fetch_run", fake_fetch)
    return SimpleNamespace(runs=runs, calls=calls)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_api(monkeypatch, answers: dict):
    """urlopen stand-in: path -> dict (200) or int (HTTP error) or Exception."""
    seen: list[str] = []

    def fake_urlopen(request, timeout):
        path = request.full_url[len(gate.API):]
        seen.append(path)
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "error", {}, io.BytesIO(b""))
        return _Response(json.dumps(answer).encode())

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)
    return seen


RUN_PATH = f"/repos/{gate.RELEASE_REPO}/actions/runs/12345"
REPO_PATH = f"/repos/{gate.RELEASE_REPO}"


# ── проверено ───────────────────────────────────────────────────────────

def test_promoted_release_with_api_verified_ci_is_accepted(monkeypatch, api, warn_stage) -> None:
    _install_fake_git(monkeypatch)

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is True
    assert "acl-release-" in detail and "verified" in detail
    assert gate.UNVERIFIED_NOTE not in detail
    assert api.calls == ["12345"]
    assert warn_stage == []


def test_any_accepting_run_among_several_links_is_enough(monkeypatch, api) -> None:
    api.runs["11111"] = gate.CiUnverified("could not be verified: GitHub answered HTTP 503")
    _install_fake_git(monkeypatch, annotation=_annotation(
        f"https://github.com/{gate.RELEASE_REPO}/actions/runs/11111\n{RUN_URL}"))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is True and gate.UNVERIFIED_NOTE not in detail
    assert api.calls == ["11111", "12345"]


# ── определённое «нет» — отказ в любом режиме ───────────────────────────

def test_fabricated_ci_link_is_rejected(monkeypatch, api, warn_stage) -> None:
    # «success» и ссылка правильного вида на прогон, которого нет.
    _install_fake_git(monkeypatch, annotation=_annotation(
        f"CI success\nhttps://github.com/{gate.RELEASE_REPO}/actions/runs/99999999"))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False
    assert "99999999" in detail and "does not exist" in detail
    assert warn_stage == []


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


def test_definite_negative_outweighs_an_unverifiable_second_link(monkeypatch, api) -> None:
    api.runs["12345"] = _good_run(conclusion="failure")
    api.runs["11111"] = gate.CiUnverified("could not be verified: GitHub answered HTTP 403")
    _install_fake_git(monkeypatch, annotation=_annotation(
        f"success\n{RUN_URL}\nhttps://github.com/{gate.RELEASE_REPO}/actions/runs/11111"))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False and "did not succeed" in detail


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


def test_run_404_while_the_repository_is_visible_is_a_made_up_link(monkeypatch) -> None:
    _install_fake_git(monkeypatch)
    monkeypatch.setattr(gate, "github_token", lambda: "tok")
    seen = _http_api(monkeypatch, {RUN_PATH: 404, REPO_PATH: {"full_name": gate.RELEASE_REPO}})

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False and "does not exist" in detail
    assert seen == [RUN_PATH, REPO_PATH]


def test_non_numeric_run_id_is_rejected_and_never_reaches_the_api(monkeypatch) -> None:
    monkeypatch.setattr(gate.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(gate.CiRejected):
        gate.fetch_run("../../../evil", token="tok")


# ── проверить нечем: warn — текст тега + телеметрия; enforce — отказ ─────

UNVERIFIABLE = [
    pytest.param({RUN_PATH: 404, REPO_PATH: 404}, "HTTP 404", id="repo-not-visible"),
    pytest.param({RUN_PATH: 403}, "HTTP 403", id="pat-without-actions-read"),
    pytest.param({RUN_PATH: 401}, "HTTP 401", id="expired-token"),
    pytest.param({RUN_PATH: 500}, "HTTP 500", id="github-error"),
    pytest.param({RUN_PATH: urllib.error.URLError("offline")}, "URLError", id="offline"),
    pytest.param({RUN_PATH: TimeoutError("slow")}, "TimeoutError", id="timeout"),
    pytest.param({RUN_PATH: 404, REPO_PATH: urllib.error.URLError("x")}, "HTTP 404", id="404-then-offline"),
]


@pytest.mark.parametrize("answers,why", UNVERIFIABLE)
def test_unverifiable_ci_falls_back_to_the_tag_record_and_is_reported(monkeypatch, warn_stage, answers, why) -> None:
    _install_fake_git(monkeypatch)
    monkeypatch.setattr(gate, "github_token", lambda: "tok-SECRET-value")
    _http_api(monkeypatch, answers)

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is True
    assert gate.UNVERIFIED_NOTE in detail and why in detail
    assert "tok-SECRET-value" not in detail
    assert len(warn_stage) == 1
    tag, reason, mode = warn_stage[0]
    assert tag == "acl-release-2026-09-01" and why in reason and mode == "warn"
    assert "tok-SECRET-value" not in reason


@pytest.mark.parametrize("answers,why", UNVERIFIABLE)
def test_unverifiable_ci_is_refused_when_enforced(monkeypatch, warn_stage, answers, why) -> None:
    monkeypatch.setenv(gate.GATE_MODE_ENV, "enforce")
    _install_fake_git(monkeypatch)
    monkeypatch.setattr(gate, "github_token", lambda: "tok")
    _http_api(monkeypatch, answers)

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False and why in detail and "enforced" in detail
    assert warn_stage == []


def test_missing_token_falls_back_without_any_request(monkeypatch, warn_stage) -> None:
    # SSH-remote, GCM с несколькими аккаунтами: учётки для API нет.
    _install_fake_git(monkeypatch)
    monkeypatch.setattr(gate, "github_token", lambda: "")
    monkeypatch.setattr(gate.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request without a token")))

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is True and "no GitHub credential" in detail
    assert warn_stage and "no GitHub credential" in warn_stage[0][1]


def test_missing_token_is_refused_when_enforced_in_code(monkeypatch) -> None:
    monkeypatch.setattr(gate, "GATE_ENFORCE", True)
    _install_fake_git(monkeypatch)
    monkeypatch.setattr(gate, "github_token", lambda: "")

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False and "no GitHub credential" in detail


def test_fallback_still_requires_the_tag_to_record_success(monkeypatch, warn_stage) -> None:
    _install_fake_git(monkeypatch, annotation=_annotation(f"Built\n{RUN_URL}"))
    monkeypatch.setattr(gate, "github_token", lambda: "")

    ok, detail = gate.validate_candidate("origin/main")

    assert ok is False and "does not record success" in detail
    assert warn_stage == []


def test_mode_can_only_be_made_stricter(monkeypatch) -> None:
    assert gate.gate_mode() == "warn"
    monkeypatch.setenv(gate.GATE_MODE_ENV, "enforce")
    assert gate.gate_mode() == "enforce"
    monkeypatch.setenv(gate.GATE_MODE_ENV, "warn")
    monkeypatch.setattr(gate, "GATE_ENFORCE", True)
    assert gate.gate_mode() == "enforce"


def test_unverified_report_is_logged_once_per_process(monkeypatch, caplog) -> None:
    monkeypatch.undo()  # настоящий report_unverified; Sentry выключен в conftest
    monkeypatch.setattr(gate, "_reported", set())
    with caplog.at_level(logging.WARNING, logger="acl.update_gate"):
        gate.report_unverified("acl-release-x", "could not be verified: HTTP 403", "warn")
        gate.report_unverified("acl-release-x", "could not be verified: HTTP 403", "warn")
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and gate.UNVERIFIED_NOTE in lines[0] and "HTTP 403" in lines[0]


# ── запрос к API ────────────────────────────────────────────────────────

def test_real_fetch_sends_token_only_to_the_pinned_repository(monkeypatch) -> None:
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _Response(json.dumps(_good_run()).encode())

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)

    run = gate.fetch_run("12345", token="tok")

    assert run["conclusion"] == "success"
    assert seen["url"] == f"https://api.github.com/repos/{gate.RELEASE_REPO}/actions/runs/12345"
    assert seen["auth"] == "Bearer tok"


# ── откуда берётся токен ────────────────────────────────────────────────

def test_token_lookup_never_prompts(monkeypatch) -> None:
    for name in ("ACL_UPDATE_GATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_ASKPASS", "C:/some/ide-askpass.exe")
    monkeypatch.setenv("SSH_ASKPASS", "C:/some/ssh-askpass.exe")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="protocol=https\nhost=github.com\nusername=x\npassword=stored-token\n")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    assert gate.github_token() == "stored-token"
    assert seen["cmd"] == ["git", "-c", "core.askPass=", "credential", "fill"]
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GCM_INTERACTIVE"] == "never"
    assert "GIT_ASKPASS" not in seen["env"] and "SSH_ASKPASS" not in seen["env"]

    monkeypatch.setenv("ACL_UPDATE_GATE_TOKEN", "explicit")
    assert gate.github_token() == "explicit"


def test_no_askpass_program_is_ever_started_by_real_git(tmp_path, monkeypatch) -> None:
    """Real git, isolated from the machine's config: askpass must not run."""
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    marker = tmp_path / "askpass-was-run"
    askpass = tmp_path / "askpass.sh"
    askpass.write_text(f'#!/bin/sh\necho hit >> "{marker.as_posix()}"\necho fake\n', encoding="utf-8", newline="\n")
    askpass.chmod(0o755)
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    subprocess.run([git, "init", "--quiet", str(repo)], check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ASKPASS", str(askpass))
    monkeypatch.setenv("SSH_ASKPASS", str(askpass))

    # Контроль: без нейтрализации тот же git askpass запускает.
    control = subprocess.run([git, "credential", "fill"], cwd=repo, input="protocol=https\nhost=example.invalid\n\n",
                             capture_output=True, text=True, env={**gate.os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if not marker.exists():
        pytest.skip(f"this git cannot run a shell askpass ({control.returncode})")
    marker.unlink()

    assert gate._git_credential_token(cwd=repo) == ""
    assert not marker.exists()


def test_machine_env_token_unsticks_a_machine_without_git_credential(tmp_path, monkeypatch) -> None:
    monkeypatch.undo()
    env_file = tmp_path / ".env"
    env_file.write_text("ACL_UPDATE_GATE_TOKEN=from-machine-env\n", encoding="utf-8")
    monkeypatch.setattr("config.paths.get_env_files", lambda: [str(env_file)])
    monkeypatch.delenv("ACL_UPDATE_GATE_TOKEN", raising=False)
    monkeypatch.setattr(gate, "_git_credential_token",
                        lambda cwd=None: pytest.fail("the machine .env token comes first"))

    assert gate.github_token() == "from-machine-env"


def test_no_stored_credential_and_no_env_means_no_token(monkeypatch) -> None:
    for name in ("ACL_UPDATE_GATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gate.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=128, stdout="", stderr="terminal prompts disabled"))

    assert gate.github_token() == ""


# ── прежние проверки ────────────────────────────────────────────────────

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


def test_installer_gate_stage_matches_the_python_gate() -> None:
    # Install ACL.bat — самостоятельная копия гейта: включение enforce в
    # acl_update_gate.py без такой же правки в установщике его не ужесточит.
    import re
    from pathlib import Path

    text = (Path(gate.__file__).resolve().parents[1] / "Install ACL.bat").read_text(encoding="ascii")
    found = re.findall(r"(?m)^\$AclGateEnforce = \$(true|false)\r?$", text)
    assert found == [str(_SHIPPED).lower()]
    assert "$enforce = $AclGateEnforce -or" in text
