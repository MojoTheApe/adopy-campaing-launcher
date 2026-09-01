"""Google readers must use the same machine-local OAuth files doctor checks."""
from __future__ import annotations

from pathlib import Path

from config import paths
from src.analysis_agent import gsheet_accounts
from src.analysis_agent import gsheet_targeting
from src.web import ad_copy_gsheet


def _use_empty_checkout(monkeypatch, tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(paths, "PROJECT_ROOT", checkout)
    return checkout


def test_accounts_reader_passes_machine_local_oauth_paths(monkeypatch, tmp_path: Path) -> None:
    local = tmp_path / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(local))
    monkeypatch.delenv("GSHEET_OAUTH_CLIENT", raising=False)
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GSHEET_ACCOUNTS_ID", "sheet-for-test")
    _use_empty_checkout(monkeypatch, tmp_path)
    captured = {}

    def fake_build(credentials, oauth_client, oauth_token):
        captured["args"] = (credentials, oauth_client, oauth_token)
        return None

    monkeypatch.setattr(gsheet_accounts, "_build_sheets_service", fake_build)

    assert gsheet_accounts.fetch_account_rows() == []
    assert captured["args"][1:] == (
        str(local / "oauth_client.json"),
        str(local / "token_accounts.json"),
    )


def test_ad_copy_reader_uses_the_same_machine_local_oauth_paths(
    monkeypatch, tmp_path: Path
) -> None:
    local = tmp_path / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(local))
    monkeypatch.delenv("GSHEET_OAUTH_CLIENT", raising=False)
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    _use_empty_checkout(monkeypatch, tmp_path)
    captured = {}

    def fake_build(oauth_client, oauth_token):
        captured["args"] = (oauth_client, oauth_token)
        return object()

    monkeypatch.setattr(gsheet_accounts, "_build_sheets_service_oauth", fake_build)

    ad_copy_gsheet._build_service()
    assert captured["args"] == (
        str(local / "oauth_client.json"),
        str(local / "token_accounts.json"),
    )


def test_targeting_reader_uses_machine_local_oauth_defaults(
    monkeypatch, tmp_path: Path
) -> None:
    local = tmp_path / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(local))
    monkeypatch.delenv("GSHEET_OAUTH_CLIENT", raising=False)
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    _use_empty_checkout(monkeypatch, tmp_path)
    captured = {}

    def fake_oauth(oauth_client, oauth_token):
        captured["args"] = (oauth_client, oauth_token)
        return None

    monkeypatch.setattr(gsheet_accounts, "_build_sheets_service_oauth", fake_oauth)
    (local / "oauth_client.json").parent.mkdir(parents=True)
    (local / "oauth_client.json").write_text('{"installed": {}}', encoding="utf-8")

    assert gsheet_targeting.load_targeting_profiles(force=True) == {}
    assert captured["args"] == (
        str(local / "oauth_client.json"),
        str(local / "token_accounts.json"),
    )
