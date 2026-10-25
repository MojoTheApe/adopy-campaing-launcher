"""ACL-1358: a token that sits in the environment or in a token file never leaves.

Known, obviously fake token values are put where the machine keeps them (the
process environment, the machine .env, the Google token files, plain and in
protected form) and must be absent from everything that is sent or kept for
others: a Sentry event after the before_send scrubber, a telemetry row, a page
capture and a fixture cut from it, and the support bundle denylist.

The values have no token shape on purpose: the pattern scrubbers only know a few
formats, so what is tested is that the machine's own values are hidden by value.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import paths, token_vault  # noqa: E402
from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import page_capture as pc  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import telemetry_sync  # noqa: E402

FAKE_ML = "FAKE-multilogin-token-acl1358-aaaa-0001"
FAKE_REFRESH = "FAKE-google-refresh-acl1358-bbbb-0002"
FAKE_CLIENT_SECRET = "FAKE-google-client-secret-acl1358-0003"
ALL_FAKES = (FAKE_ML, FAKE_REFRESH, FAKE_CLIENT_SECRET)


@pytest.fixture(params=["plain", "protected"])
def machine(request, tmp_path, monkeypatch):
    """A machine with the fake tokens in env, .env and the token files."""
    config = tmp_path / "ACL"
    config.mkdir()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", FAKE_ML)
    google = {"refresh_token": FAKE_REFRESH, "client_id": "cid", "client_secret": FAKE_CLIENT_SECRET}
    (config / ".env").write_text(f'MULTILOGIN_AUTOMATION_TOKEN_A="{FAKE_ML}"\n', encoding="utf-8")
    (config / "token_accounts.json").write_text(json.dumps(google), encoding="utf-8")
    (config / "token_telemetry.json").write_text(json.dumps(google), encoding="utf-8")
    if request.param == "protected":
        monkeypatch.setattr(token_vault, "available", lambda: True)
        monkeypatch.setattr(token_vault, "protect_bytes", lambda b: b"SEAL" + bytes(x ^ 0x5A for x in b))
        monkeypatch.setattr(token_vault, "unprotect_bytes", lambda b: bytes(x ^ 0x5A for x in b[4:]))
        assert paths.protect_machine_secrets()
        assert FAKE_ML not in (config / ".env").read_text(encoding="utf-8")
    token_vault.reset_secret_cache()
    yield config
    token_vault.reset_secret_cache()


def _absent(text: str) -> None:
    for fake in ALL_FAKES:
        assert fake not in text, "a token value reached an export"


def test_the_machine_values_are_known_to_the_scrubbers(machine):
    assert {FAKE_ML, FAKE_REFRESH, FAKE_CLIENT_SECRET} <= token_vault.configured_secret_values()


# ── Sentry ────────────────────────────────────────────────────────────────────

def test_a_sentry_event_carries_no_token_after_before_send(machine):
    event = {
        "message": f"Multilogin refused {FAKE_ML}",
        "exception": {"values": [{"type": "RuntimeError", "value": f"401 for {FAKE_ML} / {FAKE_REFRESH}"}]},
        "breadcrumbs": {"values": [{"message": f"opened with {FAKE_CLIENT_SECRET}"}]},
        "extra": {"detail": f"retry with {FAKE_REFRESH}", "nested": {"list": [f"x {FAKE_ML} y"]}},
        "contexts": {"robot": {"why": f"saw {FAKE_ML}"}},
        "tags": {"reason": "mlx_unauthorized"},
    }
    out = acl_sentry._before_send(event, {})
    assert out is not None
    _absent(json.dumps(out, ensure_ascii=False))
    assert "<hidden>" in json.dumps(out)


# ── telemetry ─────────────────────────────────────────────────────────────────

def test_a_telemetry_record_and_its_sheet_row_carry_no_token(machine, tmp_path):
    journal = tmp_path / "telemetry.jsonl"
    rt.record("profile_open", False, run_id="run1", campaign="C1", path=journal,
              diag={"error": f"401 {FAKE_ML}", "why": [f"{FAKE_REFRESH}"]},
              note=f"token was {FAKE_CLIENT_SECRET}")
    written = journal.read_text(encoding="utf-8")
    _absent(written)
    rec = json.loads(written.splitlines()[0])
    row = telemetry_sync.to_row(rec, "label", "machine", "v1")
    _absent(json.dumps(row))


def test_an_old_journal_line_is_cleaned_again_before_it_is_uploaded(machine):
    old_line = {"ts": "t", "run_id": "r", "step": "s", "ok": False,
                "diag": {"x": f"{FAKE_ML}"}, "note": f"{FAKE_REFRESH}"}
    row = telemetry_sync.to_row(old_line, "label", "machine", "v1")
    _absent(json.dumps(row))


def test_an_upload_error_text_carries_no_token(machine, monkeypatch, tmp_path):
    monkeypatch.setattr(telemetry_sync, "pending", lambda limit=None, since=None: ([["a"]], 1))
    monkeypatch.setattr(telemetry_sync, "config_status", lambda: {
        "configured": True, "transport": "webhook", "webhook": "https://example.invalid/x",
        "tab": "t", "sheet_id": "s", "key_path": "k"})

    def boom(url, tab, rows):
        raise RuntimeError(f"HTTP 401 bearer {FAKE_ML}")

    monkeypatch.setattr(telemetry_sync, "_send_webhook", boom)
    monkeypatch.setattr(telemetry_sync, "read_cursor", lambda: 0)
    result = telemetry_sync.upload()
    assert result["error"]
    _absent(result["error"])


# ── page capture and fixtures ─────────────────────────────────────────────────

class _Page:
    def __init__(self, html):
        self._html = html
        self.url = "https://x.example/p"

    def locator(self, selector):
        return self

    def inner_html(self, *, timeout):
        return self._html


def test_a_page_capture_artifact_carries_no_token(machine, tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "get_runtime_workspace", lambda: tmp_path / "ws")
    pc._taken.clear()
    html = (f"<html><body><script>window.cfg={{auth:'{FAKE_ML}'}}</script>"
            f"<p>session {FAKE_REFRESH}</p></body></html>")
    folder = pc.capture(_Page(html), step="s_open", run_id="runX", campaign="C1",
                        diag={"detail": f"used {FAKE_CLIENT_SECRET}"})
    assert folder
    files = [p for p in Path(folder).rglob("*") if p.is_file()]
    assert files
    for path in files:
        _absent(path.read_text(encoding="utf-8", errors="replace"))


def test_a_fixture_cut_from_a_capture_carries_no_token(machine, tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import scripts.capture_to_fixture as ctf

    capture = tmp_path / "rpa_pages" / "runY" / "01_step"
    capture.mkdir(parents=True)
    (capture / "page.html").write_text("<html><body>x</body></html>", encoding="utf-8")
    (capture / "context.json").write_text(json.dumps({"step": "step", "campaign": ""}), encoding="utf-8")
    monkeypatch.setattr(ctf, "cut", lambda raw, specs, browser_executable=None: [
        {"name": "frag", "found": True, "matches": 1,
         "html": f"<div>token {FAKE_ML} and {FAKE_REFRESH} and {FAKE_CLIENT_SECRET}</div>"}])
    text, _info = ctf.build_fixture(capture, [ctf.Spec(name="frag", selector="div")])
    _absent(text)
    assert "TOKEN_REDACTED" in text


# ── support bundle ────────────────────────────────────────────────────────────

def test_the_support_bundle_denylist_hides_every_token(machine):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "acl_diagnostics_1358", ROOT / "packaging" / "windows" / "diagnostics.py")
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    values = diagnostics.local_values_to_redact(machine)
    cleaned = diagnostics.sanitize_text(" ".join(ALL_FAKES), values)
    _absent(cleaned)
