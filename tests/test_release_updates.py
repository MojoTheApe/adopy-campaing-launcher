"""Updates of the installed ACL.exe from GitHub Releases (ACL-1240).

A local HTTP server plays GitHub: the releases API wants the token, a release
asset answers with a redirect to "storage", and storage must NOT receive the
token — exactly how a private repository's assets behave.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.web import release_updates as ru

INSTALLER = b"MZ" + b"\x00" * 4096


@pytest.fixture(autouse=True)
def reset_update_reservations(monkeypatch):
    from src.web import publish_jobs as jobs
    from src.fb_ads_rpa.operations import browser_operation
    monkeypatch.setattr(jobs, "_update_reserved", False)
    monkeypatch.setattr(jobs, "_jobs", {})
    yield
    browser_operation.release()
    jobs.release_update()
    if ru.apply_lock.locked():
        ru.apply_lock.release()


class FakeGitHub:
    def __init__(self):
        self.version = "2026-09-20"
        self.installer = INSTALLER
        self.published_sha = hashlib.sha256(INSTALLER).hexdigest()
        self.storage_saw_token: list[bool] = []
        self.api_calls = 0
        self.down = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                base = f"http://127.0.0.1:{fake.port}"
                if self.path.startswith("/storage/"):
                    fake.storage_saw_token.append("Authorization" in self.headers)
                    return self._send(200, fake.installer if self.path.endswith(".exe") else fake.release_json())
                if fake.down:
                    return self._send(503)
                if self.headers.get("Authorization") != "Bearer read-token":
                    return self._send(401)
                fake.api_calls += 1
                if self.path == "/repos/adopy/acl-releases/releases/latest":
                    return self._send(200, json.dumps({"assets": [
                        {"name": "release.json", "url": f"{base}/assets/1"},
                        {"name": fake.name(), "url": f"{base}/assets/2"}]}).encode())
                if self.path == "/assets/1":
                    return self._send(302, headers={"Location": f"{base}/storage/release.json"})
                if self.path == "/assets/2":
                    return self._send(302, headers={"Location": f"{base}/storage/{fake.name()}"})
                return self._send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def name(self):
        return f"Adopy-Campaign-Launcher-Setup-{self.version}.exe"

    def release_json(self):
        return json.dumps({"schema": 1, "version": self.version, "tag": f"acl-release-{self.version}",
                           "installer": {"name": self.name(), "size": len(INSTALLER), "sha256": self.published_sha,
                                         "url": "unused"},
                           "fixed": [{"issue": "ACL-1241", "pull_requests": [244], "notes": ["Team library"]}]}).encode()


@pytest.fixture
def github(tmp_path, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(ru, "API", f"http://127.0.0.1:{fake.port}")
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_RELEASES_REPO", "adopy/acl-releases")
    monkeypatch.setenv("ACL_UPDATE_TOKEN", "read-token")
    monkeypatch.setattr(ru, "installed_version", lambda: "2026-09-03e")
    yield fake
    fake.server.shutdown()


def test_a_newer_release_is_offered_with_what_it_fixes(github):
    state = ru.check()
    assert state["available"] and state["channel"] == "release"
    assert state["candidate"]["version"] == "2026-09-20"
    assert state["fixed"][0]["issue"] == "ACL-1241"
    assert "read-token" not in json.dumps(state)
    assert github.storage_saw_token == [False], "the token must not travel to asset storage"


def test_github_is_asked_at_most_once_a_day(github):
    ru.check(now=1000.0)
    calls = github.api_calls
    ru.check(now=1000.0 + 3600)
    assert github.api_calls == calls
    ru.check(now=1000.0 + 25 * 3600)
    assert github.api_calls > calls
    ru.check(now=1000.0 + 25 * 3600 + 60, force=True)
    assert github.api_calls > calls + 2


def test_the_same_version_is_up_to_date(github, monkeypatch):
    monkeypatch.setattr(ru, "installed_version", lambda: "2026-09-20")
    state = ru.check()
    assert not state["available"] and state["note"] == "ACM is up to date."


def test_an_outage_keeps_the_last_known_release_and_says_why(github):
    ru.check(now=1000.0)
    github.down = True
    state = ru.check(now=1000.0 + 25 * 3600)
    assert state["available"], "the release seen yesterday is still offered"
    github.down = True
    ru._state_path().unlink()
    state = ru.check(now=5000.0 + 50 * 3600)
    assert not state["ok"] and "503" in state["note"]


def test_without_a_channel_nothing_is_offered(github, monkeypatch):
    monkeypatch.delenv("ACL_UPDATE_TOKEN")
    state = ru.check()
    assert not state["available"] and "not configured" in state["note"]


def test_a_rejected_token_is_named_plainly(github, monkeypatch):
    monkeypatch.setenv("ACL_UPDATE_TOKEN", "wrong")
    state = ru.check(force=True)
    assert not state["ok"] and "token" in state["note"] and "wrong" not in state["note"]


def test_the_installer_is_kept_only_when_its_sha256_matches(github):
    setup, manifest = ru.download()
    assert setup.read_bytes() == INSTALLER and manifest["version"] == "2026-09-20"
    calls = github.api_calls
    ru.download()  # второй раз уже скачанный и проверенный файл не качается
    assert github.api_calls == calls + 2  # только latest + release.json
    setup.unlink()
    github.installer = INSTALLER + b"tampered"
    with pytest.raises(ru.UpdateError, match="SHA-256"):
        ru.download()
    assert not setup.exists()


@pytest.mark.parametrize("field,value", [
    ("sha256", "not-a-hash"), ("name", "..\\evil.exe"), ("name", "setup.msi"), ("size", 0)])  # linux-ok: a hostile file name rejected by a whitelist, runs on any OS
def test_a_suspicious_release_json_is_refused(field, value):
    good = {"schema": 1, "version": "2026-09-20",
            "installer": {"name": "Setup.exe", "size": 10, "sha256": "a" * 64}}
    ru.parse_release(json.dumps(good).encode())
    good["installer"][field] = value
    with pytest.raises(ru.UpdateError):
        ru.parse_release(json.dumps(good).encode())


def test_the_helper_is_started_only_from_the_installed_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(ru.sys, "frozen", False, raising=False)
    with pytest.raises(ru.UpdateError, match="installed ACL.exe"):
        ru.start_helper(tmp_path / "Setup.exe", {"version": "2026-09-20"})


def test_installed_build_update_route(github, monkeypatch):
    from src.web import app as web
    from src.web import acl_update

    monkeypatch.setattr(web.sys, "frozen", True, raising=False)
    started, exited = [], []
    monkeypatch.setattr(ru, "start_helper", lambda setup, manifest: started.append((setup.name, manifest["version"])))
    monkeypatch.setattr(acl_update, "schedule_exit", lambda *a, **k: exited.append(True))
    import src.web.publish_jobs as jobs

    monkeypatch.setattr(jobs, "active_job_count", lambda: 1)
    busy = asyncio.run(web.acl_update_apply())
    assert busy.status_code == 409 and not started

    monkeypatch.setattr(jobs, "active_job_count", lambda: 0)
    ok = asyncio.run(web.acl_update_apply())
    assert json.loads(ok.body) == {"ok": True, "target": "2026-09-20"}
    assert started == [(github.name(), "2026-09-20")] and exited == [True]


def test_health_names_the_running_version(monkeypatch):
    from src.web import app as web

    monkeypatch.setattr(web, "_installed_release", lambda: {"tag": "2026-09-20"})
    body = json.loads(asyncio.run(web.health()).body)
    # `config` — чей это дешборд (ACL-1309); его проверяет
    # tests/test_the_dashboard_says_whose_it_is.py.
    from config.paths import config_identity

    assert body == {"app": "ACL", "ok": True, "version": "2026-09-20", "config": config_identity()}


def test_an_older_latest_release_is_not_an_update(github):
    github.version = "2026-09-01"
    assert not ru.check(force=True)["available"]
    with pytest.raises(ru.UpdateError):
        ru.download()


@pytest.mark.parametrize("payload", [[], None, {"schema": 1, "version": "v", "installer": []}])
def test_non_object_manifests_fail_cleanly(payload):
    with pytest.raises(ru.UpdateError):
        ru.parse_release(json.dumps(payload).encode())


def test_queued_publish_is_still_busy(monkeypatch):
    from src.web import publish_jobs as jobs
    monkeypatch.setattr(jobs, "_jobs", {"one": {"status": "queued"}})
    assert jobs.active_job_count() == 1


def test_update_reservation_blocks_new_jobs(monkeypatch):
    from src.web import publish_jobs as jobs
    monkeypatch.setattr(jobs, "use_rpa_publish", lambda: True)
    assert jobs.reserve_update()
    with pytest.raises(RuntimeError, match="restarting"):
        jobs.create_job("account", "country", [])
    jobs.release_update()
    assert jobs.create_job("account", "country", [])


def test_duplicate_update_does_not_launch_another_helper(github, monkeypatch):
    from src.web import app as web, acl_update
    monkeypatch.setattr(web.sys, "frozen", True, raising=False)
    started = []
    monkeypatch.setattr(ru, "start_helper", lambda *args: started.append(1))
    monkeypatch.setattr(acl_update, "schedule_exit", lambda: None)
    assert asyncio.run(web.acl_update_apply()).status_code == 200
    assert asyncio.run(web.acl_update_apply()).status_code == 409
    assert started == [1]


def test_failed_helper_releases_reservations(github, monkeypatch):
    from src.web import app as web, publish_jobs as jobs
    monkeypatch.setattr(web.sys, "frozen", True, raising=False)
    def fail(*args): raise ru.UpdateError("helper did not start")
    monkeypatch.setattr(ru, "start_helper", fail)
    assert asyncio.run(web.acl_update_apply()).status_code == 400
    assert not ru.apply_lock.locked()
    assert jobs.reserve_update()


def test_browser_read_is_not_interrupted(github, monkeypatch):
    from src.web import app as web
    from src.fb_ads_rpa.operations import browser_operation
    monkeypatch.setattr(web.sys, "frozen", True, raising=False)
    ready, finish = threading.Event(), threading.Event()
    def reader():
        with browser_operation.reading():
            ready.set()
            finish.wait(15)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert ready.wait(5)
        assert asyncio.run(web.acl_update_apply()).status_code == 409
    finally:
        finish.set()
        thread.join(5)


def test_channel_cache_is_not_reused_for_another_repository(github, monkeypatch):
    assert ru.check(force=True)["available"]
    monkeypatch.setenv("ACL_RELEASES_REPO", "adopy/other-releases")
    assert not ru.check()["available"]


def test_token_is_never_sent_to_an_asset_url_on_another_host(monkeypatch):
    calls = []
    monkeypatch.setattr(ru.urllib.request, "build_opener", lambda *a: calls.append(a))
    with pytest.raises(ru.UpdateError):
        ru._request("https://example.invalid/asset", ru.Channel("owner/repo", "secret"), accept="application/octet-stream")
    assert not calls


@pytest.mark.parametrize("name", ["drive:Setup.exe", "CON.exe", "bad?name.exe"])
def test_windows_unsafe_installer_names_are_rejected(github, name):
    manifest = json.loads(github.release_json())
    manifest["installer"]["name"] = name
    with pytest.raises(ru.UpdateError):
        ru.parse_release(json.dumps(manifest).encode())


def test_update_interface_uses_runtime_results():
    from pathlib import Path
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the dashboard behavior harness")
    subprocess.run([node, str(Path(__file__).with_name("release_updates_ui_harness.cjs"))], check=True, timeout=20)


def test_helper_launch_failure_leaves_the_dashboard_running(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ru.sys, "frozen", True, raising=False)
    def fail(*args, **kwargs):
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert (ru.updates_dir() / "pending.json").exists()
        raise OSError("launch failed")
    monkeypatch.setattr(ru.subprocess, "Popen", fail)
    with pytest.raises(ru.UpdateError, match="still running"):
        ru.start_helper(tmp_path / "Setup.exe", {"version": "2026-09-20"})
    assert not (ru.updates_dir() / "pending.json").exists()
