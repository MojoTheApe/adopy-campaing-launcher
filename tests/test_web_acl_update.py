"""The header's Update button: what it offers, and what it refuses.

Updating restarts the dashboard. Every refusal tested here is a case where
clicking would have stopped a server that then never came back, or interrupted
a campaign run halfway through Ads Manager. Those are the failures that cost a
campaign manager their morning, and none of them is visible from the button.

The happy path is tested with the process-exit stubbed out, for the obvious
reason.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.web import acl_update
from src.web.app import app


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.request(method, path, **kwargs)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Never touch this machine's real ACL config dir, and never inherit a cache."""
    monkeypatch.setattr(acl_update, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(acl_update, "schedule_exit", lambda *a, **k: None)
    monkeypatch.setenv(acl_update.SUPERVISED_ENV, "1")
    acl_update.forget_cache()
    yield
    acl_update.forget_cache()


def _offer(**over):
    state = {
        "ok": True,
        "available": True,
        "current": {"sha": "a" * 40, "short": "a" * 12, "tag": "acl-release-2026-09-01"},
        "candidate": {"sha": "b" * 40, "short": "b" * 12, "tag": "acl-release-2026-09-02"},
        "note": "Update available.",
        "supervised": True,
    }
    state.update(over)
    return state


# ── the marker, which is the whole contract with the launcher ──────────────

def test_request_restart_writes_a_marker_the_launcher_looks_for(tmp_path):
    path = acl_update.request_restart(target="acl-release-2026-09-02")

    assert path == tmp_path / acl_update.RESTART_MARKER
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["reason"] == "update"
    assert payload["target"] == "acl-release-2026-09-02"


# ── refusals ───────────────────────────────────────────────────────────────

def test_update_is_refused_when_no_launcher_is_watching(monkeypatch, tmp_path):
    """Started from a terminal, exiting would leave no dashboard and no restart."""
    monkeypatch.delenv(acl_update.SUPERVISED_ENV, raising=False)
    monkeypatch.setattr(acl_update, "check", lambda **k: _offer())

    response = asyncio.run(_request("POST", "/acl/update/apply"))

    assert response.status_code == 409
    assert "launcher" in response.json()["error"]
    assert not (tmp_path / acl_update.RESTART_MARKER).exists()


def test_update_is_refused_while_a_campaign_job_is_running(monkeypatch, tmp_path):
    """A restart mid-run abandons a half-built campaign in Ads Manager."""
    monkeypatch.setattr("src.web.publish_jobs.active_job_count", lambda: 2)
    monkeypatch.setattr(acl_update, "check", lambda **k: _offer())

    response = asyncio.run(_request("POST", "/acl/update/apply"))

    assert response.status_code == 409
    assert "2 campaign job(s)" in response.json()["error"]
    assert not (tmp_path / acl_update.RESTART_MARKER).exists()


def test_update_is_refused_when_the_gate_offers_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr("src.web.publish_jobs.active_job_count", lambda: 0)
    monkeypatch.setattr(
        acl_update,
        "check",
        lambda **k: _offer(available=False, candidate=None, note="ACM is up to date."),
    )

    response = asyncio.run(_request("POST", "/acl/update/apply"))

    assert response.status_code == 400
    assert response.json()["error"] == "ACM is up to date."
    assert not (tmp_path / acl_update.RESTART_MARKER).exists()


# ── the happy path ─────────────────────────────────────────────────────────

def test_accepted_update_leaves_the_marker_and_names_the_release(monkeypatch, tmp_path):
    monkeypatch.setattr("src.web.publish_jobs.active_job_count", lambda: 0)
    monkeypatch.setattr(acl_update, "check", lambda **k: _offer())

    response = asyncio.run(_request("POST", "/acl/update/apply"))

    assert response.status_code == 200
    assert response.json() == {"ok": True, "target": "acl-release-2026-09-02"}
    payload = json.loads((tmp_path / acl_update.RESTART_MARKER).read_text(encoding="utf-8"))
    assert payload["target"] == "acl-release-2026-09-02"


def test_the_header_says_which_release_is_installed():
    """There was nowhere to look, and that cost an afternoon.

    A second campaign manager was asked three times whether he had updated, and
    neither side could tell: restarting the dashboard changes nothing about the
    version, so "I reopened it" and "I updated it" look the same from both ends.
    """
    import re
    from unittest.mock import MagicMock

    from src.web.app import _enrich_country_overview, mlx_instance_labels, templates
    from tests.test_web_drill import _MOCK_REPORT

    report = {**_MOCK_REPORT, "countries": [dict(c) for c in _MOCK_REPORT["countries"]]}
    _enrich_country_overview(report)
    context = dict(
        request=MagicMock(), report=report, warnings=[], built_at="x", upload_errors={},
        account_preflight={}, account_preflight_json={}, ref_options={},
        ref_options_json={}, account_alias_map_json={}, draft_status={},
        mlx_labels=mlx_instance_labels(),
    )
    template = templates.env.get_template("report.html")

    shown = template.render(**context, installed_release={"tag": "acl-release-2026-09-03c", "short": "abc123"})
    assert re.search(r'id="acl-version"[^>]*>acl-release-2026-09-03c<', shown)

    # A checkout with no release tag still has to say something truthful.
    untagged = template.render(**context, installed_release={"tag": "", "short": "abc123def456"})
    assert re.search(r'id="acl-version"[^>]*>abc123def456<', untagged)

    # Templates reload per request, Python only on restart: between the two the
    # header must render rather than take the report down with it.
    stale_server = template.render(**context)
    assert re.search(r'id="acl-version"[^>]*>unknown build<', stale_server)


# ── the check, which runs on every page load ───────────────────────────────

def test_check_never_breaks_the_page_when_git_is_unusable(monkeypatch):
    """No network, no git, a zip download — the dashboard still has to render."""
    monkeypatch.setattr(acl_update, "_git_out", lambda *a, **k: "")

    response = asyncio.run(_request("GET", "/acl/update/check?fetch=0"))

    assert response.status_code == 200
    assert response.json()["available"] is False


def test_check_offers_nothing_for_a_commit_that_is_not_a_release(monkeypatch):
    """A newer commit on main is not the same thing as a promoted release."""
    monkeypatch.setattr(
        acl_update,
        "_git_out",
        lambda *a, **k: "b" * 40 if a[:2] == ("rev-parse", "origin/main^{commit}") else "a" * 40,
    )
    monkeypatch.setattr(acl_update, "_git", lambda *a, **k: pytest.fail("must not fetch"))
    monkeypatch.setattr(
        "scripts.acl_update_gate.validate_candidate",
        lambda *a, **k: (False, "origin/main is not the exact target of an ACL release tag"),
    )

    result = acl_update.check(fetch=False, use_cache=False)

    assert result["available"] is False
    assert "not a promoted release" in result["note"]


def test_check_reports_nothing_to_do_when_head_is_the_channel(monkeypatch):
    monkeypatch.setattr(acl_update, "_git_out", lambda *a, **k: "a" * 40)

    result = acl_update.check(fetch=False, use_cache=False)

    assert result["available"] is False
    assert result["note"] == "ACM is up to date."


@pytest.mark.parametrize("detail,gate,shown", [
    ("acl-release-x (bbbbbbbbbbbb, CI run 1 verified)", "verified", None),
    ("acl-release-x (bbbbbbbbbbbb) - CI not verified: CI run 1 could not be verified: GitHub answered HTTP 403; "
     "accepted on the release tag's record (warn mode)", "unverified", "GitHub answered HTTP 403"),
])
def test_an_update_the_gate_could_not_verify_says_so(monkeypatch, detail, gate, shown):
    """ACL-1357: a machine without a token for the Actions API is visible on the page."""
    monkeypatch.setattr(
        acl_update,
        "_git_out",
        lambda *a, **k: "b" * 40 if a[:2] == ("rev-parse", "origin/main^{commit}") else "a" * 40,
    )
    monkeypatch.setattr("scripts.acl_update_gate.validate_candidate", lambda *a, **k: (True, detail))

    result = acl_update.check(fetch=False, use_cache=False)

    assert result["available"] is True and result["gate"] == gate
    if shown:
        assert "could not verify its CI result" in result["note"] and shown in result["note"]
    else:
        assert "verify" not in result["note"]
