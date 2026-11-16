"""ACL-2655: a release published after the last check is offered on the next page load."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.web import release_updates as ru

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "report.html"


def _manifest(version):
    return {"schema": 1, "tag": version, "version": version, "published_at": "2026-11-16T00:00:00Z",
            "installer": {"name": "ACL-Setup.exe", "size": 1000, "sha256": "a" * 64}, "fixed": []}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_RELEASES_REPO", "adopy/acl-releases")
    monkeypatch.setenv("ACL_UPDATE_TOKEN", "read-token")
    monkeypatch.setattr(ru, "installed_version", lambda: "2026-11-14.1636")

    class Rig:
        latest = "2026-11-14.1636"
        calls = 0
        error: Exception | None = None

    r = Rig()

    def fetch(ch):
        r.calls += 1
        if r.error:
            raise r.error
        return _manifest(r.latest), {}

    monkeypatch.setattr(ru, "fetch_latest", fetch)
    return r


def test_cache_window_is_fifteen_minutes():
    assert ru.CHECK_EVERY_SECONDS == 15 * 60


def test_check_repeats_only_after_the_window(rig):
    ru.check(now=1000.0)
    ru.check(now=1000.0 + 14 * 60)
    assert rig.calls == 1
    ru.check(now=1000.0 + 16 * 60)
    assert rig.calls == 2


def test_forced_check_sees_a_release_published_after_the_last_check(rig):
    assert not ru.check(now=1000.0)["available"]
    rig.latest = "2026-11-16.1651"
    assert not ru.check(now=1060.0)["available"], "cached answer inside the window"
    state = ru.check(now=1120.0, force=True)
    assert state["available"] and state["candidate"]["version"] == "2026-11-16.1651"


@pytest.mark.parametrize("exc", [ru.UpdateError("GitHub rate limit"), OSError("offline"), ValueError("bad json")])
def test_failed_forced_check_keeps_the_known_release_and_never_raises(rig, exc):
    rig.latest = "2026-11-16.1651"
    assert ru.check(now=1000.0)["available"]
    rig.error = exc
    for i in range(3):  # every page load forces a check
        state = ru.check(now=1100.0 + i, force=True)
        assert state["available"] and state["candidate"]["version"] == "2026-11-16.1651"


def test_error_without_any_known_release_does_not_raise(rig):
    rig.error = ru.UpdateError("offline")
    state = ru.check(now=1000.0, force=True)
    assert not state["available"] and not state["ok"]


def test_header_first_check_forces_and_hourly_one_does_not():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "'/acl/update/check' + (force ? '?force=1' : '')" in html
    assert "aclCheck(true);" in html
    assert "setInterval(function () { aclCheck(false); }, 60 * 60 * 1000);" in html
