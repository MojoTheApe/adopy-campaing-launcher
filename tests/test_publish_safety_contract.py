"""Production safety contract shared by RPA, web jobs, persistence and CLI."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from scripts import run_rpa_publish
from src.fb_ads_rpa.draft import CampaignDraft, RpaPublishResult
from src.fb_ads_rpa.rpa_publisher import RpaPublisher
from tests.profile_lease_fakes import fake_profile
from src.web.app import mlx_instance_labels, app
from src.web.app import _enrich_country_overview, templates
from src.web.publish_jobs import (
    _summarize_results,
    _write_draft_status,
    create_job,
)


@pytest.fixture
def mapped_account(monkeypatch):
    monkeypatch.setattr("src.fb_ads_rpa.rpa_publisher.load_ad_account_map", lambda _: {
        "Account": SimpleNamespace(profile_id=fake_profile("Account"))})


def _result(*, success: bool = True, publishable: bool = True) -> dict:
    return {
        "campaign_name": "Campaign A",
        "success": success,
        "publishable": publishable,
        "blocking_failures": [] if publishable else ["audience_location"],
        "warnings": [],
        "error": None,
    }


def test_persistent_summary_fails_closed_without_explicit_publishable() -> None:
    legacy_success = {"campaign_name": "Old producer", "success": True}

    summary = _summarize_results([legacy_success], expected_total=1)

    assert summary["state"] == "blocked"
    assert summary["publishable_count"] == 0
    assert summary["blocked_count"] == 1


def test_result_contract_round_trips_warnings_and_blockers_exactly() -> None:
    result = RpaPublishResult(
        "Campaign A",
        success=True,
        warnings=["warning-one", "warning-two"],
        blocking_failures=["blocker-one", "blocker-two"],
    )

    payload = result.to_job_dict()

    assert payload["warnings"] == ["warning-one", "warning-two"]
    assert payload["blocking_failures"] == ["blocker-one", "blocker-two"]
    assert payload["publishable"] is False


def test_persistent_ready_requires_every_expected_result() -> None:
    assert _summarize_results([_result()], expected_total=1)["state"] == "ready"
    assert _summarize_results([_result()], expected_total=2)["state"] == "blocked"
    assert _summarize_results([_result(publishable=False)], expected_total=1)["state"] == "blocked"


def test_blocked_run_overwrites_an_older_ready_badge(monkeypatch, tmp_path) -> None:
    machine_dir = tmp_path / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(machine_dir))
    _write_draft_status("Account", [_result()], expected_total=1)
    _write_draft_status(
        "Account",
        [_result(publishable=False)],
        expected_total=1,
    )

    data = json.loads((machine_dir / "draft_status.json").read_text(encoding="utf-8"))
    assert data["Account"]["state"] == "blocked"
    assert data["Account"]["publishable_count"] == 0
    assert data["Account"]["blocking_failures"] == ["audience_location"]


def _render_badge(status: dict) -> str:
    report = {
        "date": "2026-09-01",
        "my_cm": "Test Manager",
        "source_excel": "test.xlsx",
        "trend_days": 7,
        "total_combos": 0,
        "countries": [
            {
                "country": "Mexico",
                "trend": {"has_date": False},
                "cm_comparison": [],
                "top_combos": [],
                "targeting": [],
                "creatives": [],
                "close_recs": {},
                "account_rows": [],
                "open_recs": {
                    "Account": [
                        {
                            "offer": "Offer",
                            "campaign_name": "Campaign A",
                            "creatives": [],
                        }
                    ]
                },
            }
        ],
    }
    _enrich_country_overview(report)
    return templates.env.get_template("report.html").render(
        mlx_labels=mlx_instance_labels(),
        request=MagicMock(),
        report=report,
        warnings=[],
        built_at="2026-09-01",
        upload_errors={},
        account_preflight={},
        account_preflight_json={},
        ref_options={},
        ref_options_json={},
        account_alias_map_json={},
        draft_status={"Account": status},
    )


def test_persistent_badge_is_green_only_for_ready_state() -> None:
    ready = _summarize_results([_result()], expected_total=1)
    blocked = _summarize_results([_result(publishable=False)], expected_total=1)

    ready_html = _render_badge(ready)
    blocked_html = _render_badge(blocked)

    assert "✓ Draft ready" in ready_html
    assert "Draft blocked" not in ready_html
    assert "❌ Draft blocked · do not publish" in blocked_html
    assert "✓ Draft ready" not in blocked_html


def test_handoff_does_not_replace_do_not_publish_message(mapped_account) -> None:
    publisher = RpaPublisher.__new__(RpaPublisher)
    publisher.hand_off = True
    publisher.dry_run = False
    publisher.selectors_path = None
    publisher._ensure_browser = MagicMock(return_value=SimpleNamespace(page=MagicMock()))

    blocked = RpaPublishResult(
        campaign_name="Campaign A",
        success=True,
        error="DO NOT PUBLISH AS IS — audience_location failed",
        blocking_failures=["audience_location"],
    )
    flow = MagicMock()
    flow.publish_campaign.return_value = blocked
    draft = CampaignDraft(name="Campaign A", country="MX", account_label="Account")

    with patch("src.fb_ads_rpa.rpa_publisher.AdsManagerFlow", return_value=flow):
        result = publisher.publish_draft(draft)

    assert result.publishable is False
    assert "DO NOT PUBLISH" in (result.error or "")
    assert "Draft ready" not in (result.error or "")
    assert "NOT publishable" in (result.error or "")


def test_rpa_publisher_rejects_active_before_opening_browser(mapped_account) -> None:
    publisher = RpaPublisher.__new__(RpaPublisher)
    draft = CampaignDraft(
        name="Campaign A",
        country="MX",
        account_label="Account",
        status="ACTIVE",
    )

    with pytest.raises(ValueError, match="draft-only"):
        publisher.publish_draft(draft)


def test_rpa_publish_slots_rejects_activate(mapped_account) -> None:
    publisher = RpaPublisher.__new__(RpaPublisher)

    with pytest.raises(ValueError, match="draft-only"):
        publisher.publish_slots([], account_label="Account", country="MX", activate=True)


def test_web_job_store_rejects_legacy_api_backend(monkeypatch) -> None:
    monkeypatch.setattr("src.web.publish_jobs.use_rpa_publish", lambda: False)

    with pytest.raises(RuntimeError, match="legacy Meta API backend"):
        create_job("Account", "MX", [])


async def _post(path: str, data: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.post(path, data=data)


def test_web_endpoint_rejects_client_requested_activation() -> None:
    response = asyncio.run(
        _post(
            "/publish-jobs",
            {"activate": "1", "user_confirmed": "1", "sentinel": "parsed"},
        )
    )

    assert response.status_code == 403
    assert "Live activation is disabled" in response.json()["error"]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (RpaPublishResult("ok", success=True), 0),
        (
            RpaPublishResult(
                "blocked",
                success=True,
                blocking_failures=["audience_location"],
            ),
            2,
        ),
        (RpaPublishResult("failed", success=False, error="boom"), 1),
    ],
)
def test_cli_exit_code_uses_publishable(monkeypatch, result, expected) -> None:
    class FakePublisher:
        def __init__(self, **kwargs):
            pass

        def publish_slots(self, *args, **kwargs):
            return [result]

    monkeypatch.setattr(run_rpa_publish, "RpaPublisher", FakePublisher)

    assert run_rpa_publish.main(["--account", "Account"]) == expected


def test_cli_no_longer_exposes_activate_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        run_rpa_publish.main(["--account", "Account", "--activate"])

    assert exc.value.code == 2
