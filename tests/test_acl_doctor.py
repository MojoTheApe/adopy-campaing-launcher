"""Fail-closed checks used before a campaign-manager machine is enrolled."""
from __future__ import annotations

import json

import pytest

from scripts import acl_doctor as doctor


@pytest.fixture(autouse=True)
def reset_failure_count():
    doctor._failed = 0
    yield
    doctor._failed = 0


def test_placeholder_account_map_is_a_failure(tmp_path, monkeypatch, capsys) -> None:
    account_map = tmp_path / "accounts.csv"
    account_map.write_text(
        "account_name,ad_account_id,profile_id\nExample,act_1234567890,your-profile-uuid\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "get_ad_account_map_path", lambda: str(account_map))

    doctor.check_account_map()

    assert doctor._failed == 1
    assert "placeholder" in capsys.readouterr().out


def test_every_account_needs_a_multilogin_profile(tmp_path, monkeypatch, capsys) -> None:
    account_map = tmp_path / "accounts.csv"
    account_map.write_text(
        "account_name,ad_account_id,profile_id\nReal,act_987654,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "get_ad_account_map_path", lambda: str(account_map))

    doctor.check_account_map()

    assert doctor._failed == 1
    assert "no Multilogin profile_id" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("engine", "backend", "failures"),
    [
        ("recorded_flow", "rpa", 0),
        ("", "rpa", 1),
        ("llm_agent", "rpa", 1),
        ("recorded_flow", "api", 1),
    ],
)
def test_runtime_safety_allows_only_verified_draft_backend(
    engine, backend, failures, monkeypatch
) -> None:
    monkeypatch.setenv("FB_RPA_ENGINE", engine)
    monkeypatch.setenv("FB_PUBLISH_BACKEND", backend)

    doctor.check_runtime_safety()

    assert doctor._failed == failures


def test_checkout_only_env_is_not_production_ready(tmp_path, monkeypatch) -> None:
    repo_env = tmp_path / "repo.env"
    repo_env.write_text("FB_RPA_ENGINE=recorded_flow\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "get_env_files", lambda: [str(repo_env)])
    monkeypatch.setattr(doctor, "get_machine_env_path", lambda: str(tmp_path / "machine.env"))

    doctor.check_env_location()

    assert doctor._failed == 1


def test_valid_service_account_is_accepted_when_oauth_is_absent(
    tmp_path, monkeypatch
) -> None:
    service = tmp_path / "service.json"
    service.write_text(
        json.dumps(
            {
                # Build the sentinel at runtime so the secret scanner does not
                # mistake this intentionally fake fixture for a committed
                # Google credential JSON file.
                "type": "service" + "_account",
                "client_email": "acl@example.invalid",
                "private_key": "not-a-real-key",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GSHEET_OAUTH_CLIENT", str(tmp_path / "absent.json"))
    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", str(service))

    doctor.check_google_client()

    assert doctor._failed == 0


def test_invalid_google_json_fails_readiness(tmp_path, monkeypatch) -> None:
    invalid = tmp_path / "credentials.json"
    invalid.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GSHEET_OAUTH_CLIENT", str(invalid))
    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", str(tmp_path / "absent.json"))

    doctor.check_google_client()

    assert doctor._failed == 1


def test_machine_local_oauth_is_accepted(tmp_path, monkeypatch) -> None:
    oauth = tmp_path / "ACL" / "oauth_client.json"
    oauth.parent.mkdir()
    oauth.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake.apps.example.invalid",
                    "client_secret": "not-a-real-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "get_google_oauth_client_path", lambda: str(oauth))
    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", str(tmp_path / "absent.json"))

    doctor.check_google_client()

    assert doctor._failed == 0


def test_missing_account_map_file_is_only_a_warning(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(doctor, "get_ad_account_map_path", lambda: str(tmp_path / "absent.csv"))

    doctor.check_account_map()

    out = capsys.readouterr().out
    assert doctor._failed == 0
    assert "[warn] Account map" in out and "Accounts" in out


@pytest.mark.parametrize("provider", ["multilogin", "multilogin_attach"])
def test_missing_multilogin_token_is_only_a_warning(provider, monkeypatch, capsys) -> None:
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A", raising=False)
    monkeypatch.setattr("src.fb_ads_rpa.config.get_browser_provider", lambda: provider)

    doctor.check_multilogin_token()

    assert doctor._failed == 0
    assert "[warn] MULTILOGIN_AUTOMATION_TOKEN_A" in capsys.readouterr().out


def test_dashboard_setup_items_do_not_block_a_fresh_install(tmp_path, monkeypatch, capsys) -> None:
    """Workbooks and the creatives folder are chosen on the dashboard, not before it."""
    gone = str(tmp_path / "absent")
    monkeypatch.setattr(doctor, "get_excel_path", lambda: gone)
    monkeypatch.setattr(doctor, "get_upload_fb_excel_path", lambda: gone)
    monkeypatch.setattr(doctor, "get_creatives_root", lambda: gone)
    monkeypatch.setattr(doctor, "ensure_machine_config", lambda: [])
    monkeypatch.setattr(doctor, "load_env", lambda: [])
    for name in (
        "check_python", "check_playwright", "check_form_parser", "check_account_map",
        "check_env_location", "check_runtime_safety", "check_multilogin_token",
        "check_multilogin", "check_campaign_manager_identity", "check_accounts_sheet",
        "check_google_client", "check_telemetry", "check_event_log",
    ):
        monkeypatch.setattr(doctor, name, lambda: None)

    assert doctor.main() == 0

    out = capsys.readouterr().out
    for label in ("Stats workbook", "Upload FB workbook", "Creatives folder"):
        assert f"[warn] {label}" in out
