"""ACL-1358 review round 2: scrub cost, protect-after-healthy, installed-app rollback."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import paths, token_vault  # noqa: E402
from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import telemetry_sync  # noqa: E402

SECRET = "FAKE-multilogin-token-acl1358-round2-0001"
GOOGLE = {"refresh_token": "FAKE-google-refresh-round2-0002", "client_id": "cid",
          "client_secret": "FAKE-google-client-secret-round2-0003"}


@pytest.fixture
def fake_dpapi(monkeypatch):
    monkeypatch.setattr(token_vault, "available", lambda: True)
    monkeypatch.setattr(token_vault, "protect_bytes", lambda b: b"SEAL" + bytes(x ^ 0x5A for x in b))

    def unseal(b):
        if not b.startswith(b"SEAL"):
            raise token_vault.VaultError("not ours")
        return bytes(x ^ 0x5A for x in b[4:])

    monkeypatch.setattr(token_vault, "unprotect_bytes", unseal)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    config = tmp_path / "ACL"
    config.mkdir()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    monkeypatch.delenv("GSHEET_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_A", raising=False)
    monkeypatch.delenv(paths.DEFER_PROTECTION_ENV, raising=False)
    token_vault.reset_secret_cache()
    yield config
    token_vault.reset_secret_cache()
    os.environ.pop("MULTILOGIN_AUTOMATION_TOKEN_A", None)


def _plain(machine):
    (machine / ".env").write_text(f'KEEP=1\nMULTILOGIN_AUTOMATION_TOKEN_A="{SECRET}"\n', encoding="utf-8")
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    (machine / "token_telemetry.json").write_text(json.dumps(GOOGLE), encoding="utf-8")


def _protected(machine):
    _plain(machine)
    assert paths.protect_machine_secrets()


# ── B1: the machine's values are collected once, not per string ───────────────

@pytest.fixture
def counted(machine, monkeypatch):
    calls = {"n": 0}
    real = token_vault._collect_secret_values

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(token_vault, "_collect_secret_values", counting)
    monkeypatch.setattr(token_vault.time, "monotonic", lambda: 1000.0)  # frozen: no expiry mid-test
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", SECRET)
    token_vault.reset_secret_cache()
    return calls


def test_2000_sheet_rows_collect_the_secret_values_once(counted):
    rec = {"ts": "t", "run_id": "r", "step": "s", "ok": False, "diag": {"a": "b"}, "note": "n"}
    rows = [telemetry_sync.to_row(rec, "label", "machine", "v1") for _ in range(2000)]
    assert len(rows) == 2000
    assert counted["n"] == 1


def test_an_event_with_100_breadcrumbs_collects_the_secret_values_once(counted):
    event = {"message": "m", "breadcrumbs": {"values": [{"message": f"step {i} {SECRET}"} for i in range(100)]}}
    out = acl_sentry._before_send(event, {})
    assert SECRET not in json.dumps(out)
    assert counted["n"] == 1


def test_token_file_checks_are_bounded_for_many_scrubs(counted, machine, monkeypatch):
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    token_vault.reset_secret_cache()
    stats = {"n": 0}
    real_stat = Path.stat

    def counting_stat(self, *a, **k):
        if self.name in token_vault.TOKEN_FILES:
            stats["n"] += 1
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", counting_stat)
    for _ in range(500):
        rt.scrub("nothing to hide here")
    assert stats["n"] <= 4  # the few token files, once


def test_the_cache_expires_and_sees_a_token_set_later(machine, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(token_vault.time, "monotonic", lambda: now["t"])
    token_vault.reset_secret_cache()
    late = "FAKE-late-token-acl1358-0099"
    assert late not in token_vault.configured_secret_values()
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_B", late)
    assert late not in token_vault.configured_secret_values()  # still cached
    now["t"] += token_vault._SECRETS_TTL_S + 0.1
    assert late in token_vault.configured_secret_values()


# ── B2b: protect only after this version has proven healthy ───────────────────

def test_a_deferred_load_env_leaves_plain_tokens_plain(machine, fake_dpapi, monkeypatch):
    _plain(machine)
    before = (machine / ".env").read_bytes()
    monkeypatch.setenv(paths.DEFER_PROTECTION_ENV, "1")
    paths.load_env()
    assert (machine / ".env").read_bytes() == before
    assert (machine / "token_accounts.json").read_text(encoding="utf-8") == json.dumps(GOOGLE)
    assert not (machine / "protected-backup").exists()


def test_serve_py_defers_protection_and_starts_the_healthy_watcher():
    serve = (ROOT / "serve.py").read_text(encoding="utf-8")
    assert "protect_secrets_when_healthy" in serve
    assert serve.index("DEFER_PROTECTION_ENV") < serve.index("load_env()")


def test_tokens_are_protected_only_after_health_answers(machine, fake_dpapi, monkeypatch):
    _plain(machine)
    monkeypatch.setenv(paths.DEFER_PROTECTION_ENV, "1")
    clock = {"t": 0.0}
    seen_protected = []

    def sleep(seconds):
        clock["t"] += seconds
        seen_protected.append((machine / "token_accounts.json").read_bytes().startswith(token_vault.FILE_MAGIC))

    answers = iter([None, OSError("refused"), {"app": "ACL", "ok": True, "version": "v1"}])

    def fetch(url):
        result = next(answers)
        if isinstance(result, Exception):
            raise result
        return result

    notes = paths.protect_secrets_when_healthy(
        "http://127.0.0.1:9/health", expected_version=lambda: "v1", settle_s=15.0, poll_s=2.0,
        fetch=fetch, sleep=sleep, clock=lambda: clock["t"])
    assert notes
    assert seen_protected and not any(seen_protected)  # plain through the polling and the settle time
    assert (machine / "token_accounts.json").read_bytes().startswith(token_vault.FILE_MAGIC)
    assert not paths.protection_deferred()


def test_an_unhealthy_version_never_protects_the_tokens(machine, fake_dpapi, monkeypatch):
    _plain(machine)
    monkeypatch.setenv(paths.DEFER_PROTECTION_ENV, "1")
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    result = paths.protect_secrets_when_healthy(
        "http://127.0.0.1:9/health", expected_version=lambda: "v1", timeout_s=30,
        fetch=lambda url: None, sleep=sleep, clock=lambda: clock["t"])
    assert result is None
    assert json.loads((machine / "token_accounts.json").read_text(encoding="utf-8")) == GOOGLE
    assert paths.protection_deferred()


# ── B2a: installed app switch ─────────────────────────────────────────────────

def _launcher():
    folder = str(ROOT / "packaging" / "windows")
    sys.path.insert(0, folder)
    try:
        import launcher
    finally:
        sys.path.remove(folder)
    return launcher


def test_the_installed_app_has_an_unprotect_tokens_switch(machine, fake_dpapi, monkeypatch, capsys):
    launcher = _launcher()
    _protected(machine)
    monkeypatch.setattr(launcher, "_local_acl_dir", lambda: machine)
    monkeypatch.setattr(launcher, "_prepare_config_dir", lambda: None)  # would merge the real package copy
    monkeypatch.setattr(launcher.local_dashboard, "find_own", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", ["ACL.exe", "--unprotect-tokens"])
    assert launcher.main() == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "refresh-secret" not in out
    assert json.loads((machine / "token_accounts.json").read_text(encoding="utf-8")) == GOOGLE
    assert "dpapi:v1:" not in (machine / ".env").read_text(encoding="utf-8")


def test_the_unprotect_switch_refuses_while_the_dashboard_runs(machine, fake_dpapi, monkeypatch):
    launcher = _launcher()
    _protected(machine)
    sealed = (machine / "token_accounts.json").read_bytes()
    shown = []
    monkeypatch.setattr(launcher, "_local_acl_dir", lambda: machine)
    monkeypatch.setattr(launcher, "_prepare_config_dir", lambda: None)  # would merge the real package copy
    monkeypatch.setattr(launcher.local_dashboard, "find_own", lambda *_a, **_k: (9090, {"version": "x"}))
    monkeypatch.setattr(launcher, "_show_message", lambda title, text, **k: shown.append(text))
    monkeypatch.setattr(sys, "argv", ["ACL.exe", "--unprotect-tokens"])
    assert launcher.main() == 1
    assert (machine / "token_accounts.json").read_bytes() == sealed
    assert shown and "Stop the dashboard" in shown[0]


# ── N2 / N3 / N4 ──────────────────────────────────────────────────────────────

def test_from_backup_puts_back_only_the_token_lines_of_the_env(machine, fake_dpapi):
    _protected(machine)
    paths.update_machine_env({"KEEP": "changed-after-migration", "OTHER": "new"})
    paths.unprotect_machine_secrets(from_backup=True)
    values = dotenv_values(machine / ".env")
    assert values["MULTILOGIN_AUTOMATION_TOKEN_A"] == SECRET
    assert values["KEEP"] == "changed-after-migration" and values["OTHER"] == "new"


def test_backups_of_a_token_file_outside_the_config_folder_are_kept_apart(machine, fake_dpapi, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere" / "token_accounts.json"
    other.parent.mkdir()
    other_google = {**GOOGLE, "refresh_token": "FAKE-other-refresh-value-0004"}
    other.write_text(json.dumps(other_google), encoding="utf-8")
    monkeypatch.setenv("GSHEET_OAUTH_TOKEN", str(other))
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    assert token_vault.backup_key(other, machine) != token_vault.backup_key(machine / "token_accounts.json", machine)
    paths.protect_machine_secrets()
    other.write_bytes(token_vault.FILE_MAGIC + b"damaged")
    paths.unprotect_machine_secrets()
    assert json.loads(other.read_text(encoding="utf-8")) == other_google
    assert json.loads((machine / "token_accounts.json").read_text(encoding="utf-8")) == GOOGLE


def test_token_files_are_written_and_migrated_under_the_token_lock(machine, fake_dpapi, monkeypatch):
    held = []
    real = paths.machine_file_lock

    @contextlib.contextmanager
    def spy(lock_path, timeout=None):
        held.append(Path(lock_path).name)
        with real(lock_path, timeout):
            yield

    monkeypatch.setattr(paths, "machine_file_lock", spy)
    (machine / "token_accounts.json").write_text(json.dumps(GOOGLE), encoding="utf-8")
    paths.protect_machine_secrets()
    token_vault.write_secret_text(machine / "token_telemetry.json", json.dumps(GOOGLE))
    paths.unprotect_machine_secrets()
    assert held.count(token_vault.TOKEN_LOCK_NAME) >= 3


@pytest.mark.parametrize("answer", [
    {"app": "ACL", "ok": True, "version": ""},            # answers before the version is read
    {"app": "ACL", "ok": True, "version": "v0-other"},    # another build
    {"app": "ACL", "ok": True},                           # no version at all
    {"app": "ACL", "ok": False, "version": "v1"},
    {"app": "someone-else", "ok": True, "version": "v1"},
    {"app": "ACL", "ok": "yes", "version": "v1"},
    "200 OK",
])
def test_a_bare_answer_or_the_wrong_version_never_protects(machine, fake_dpapi, monkeypatch, answer):
    _plain(machine)
    monkeypatch.setenv(paths.DEFER_PROTECTION_ENV, "1")
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    result = paths.protect_secrets_when_healthy(
        "http://127.0.0.1:9/health", expected_version=lambda: "v1", timeout_s=60,
        fetch=lambda url: answer, sleep=sleep, clock=lambda: clock["t"])
    assert result is None
    assert json.loads((machine / "token_accounts.json").read_text(encoding="utf-8")) == GOOGLE


def test_an_unknown_own_version_never_protects(machine, fake_dpapi, monkeypatch):
    _plain(machine)
    clock = {"t": 0.0}
    result = paths.protect_secrets_when_healthy(
        "http://127.0.0.1:9/health", expected_version=lambda: "",
        timeout_s=20, fetch=lambda url: {"app": "ACL", "ok": True, "version": ""},
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
    assert result is None


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:9109/health"),
    ("0.0.0.0", "http://127.0.0.1:9109/health"),
    ("::", "http://[::1]:9109/health"),
    ("::1", "http://[::1]:9109/health"),
    ("localhost", "http://localhost:9109/health"),
])
def test_the_health_url_never_uses_a_wildcard_host(host, expected):
    assert paths.health_url_for(host, 9109) == expected


def test_serve_py_uses_the_loopback_mapped_url():
    serve = (ROOT / "serve.py").read_text(encoding="utf-8")
    assert "health_url_for(args.host, port)" in serve and "expected_version=" in serve


def test_saving_a_token_resets_the_secret_cache(machine, monkeypatch):
    token_vault.reset_secret_cache()
    late = "FAKE-connected-now-token-acl1358-0077"
    assert late not in token_vault.configured_secret_values()  # cached as "nothing"
    paths.update_machine_env({"MULTILOGIN_AUTOMATION_TOKEN_A": late})
    assert late in token_vault.configured_secret_values()

