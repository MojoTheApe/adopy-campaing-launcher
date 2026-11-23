"""ACL-2673: the server re-asks GitHub after a 60 s cache window.

The page timer from ACL-2673 is gone (ACL-2668 decision 23): the page checks
on page load and Refresh only; the 60 s cache stays to save GitHub calls."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.web import release_updates as ru

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "report.html"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_RELEASES_REPO", "adopy/acl-releases")
    monkeypatch.setenv("ACL_UPDATE_TOKEN", "read-token")
    monkeypatch.setattr(ru, "installed_version", lambda: "2026-11-14.1636")

    class Rig:
        latest = "2026-11-14.1636"
        calls = 0

    r = Rig()

    def fetch(ch):
        r.calls += 1
        return {"schema": 1, "tag": r.latest, "version": r.latest, "published_at": "2026-11-16T00:00:00Z",
                "installer": {"name": "ACL-Setup.exe", "size": 1000, "sha256": "a" * 64}, "fixed": []}, {}

    monkeypatch.setattr(ru, "fetch_latest", fetch)
    return r


def test_server_rechecks_github_only_after_sixty_seconds(rig):
    ru.check(now=1000.0)
    ru.check(now=1059.0)
    assert rig.calls == 1, "59 s: served from the cache"
    ru.check(now=1061.0)
    assert rig.calls == 2, "61 s: asks GitHub again"


def test_release_published_while_page_is_open_shows_within_two_minutes(rig):
    assert not ru.check(now=1000.0)["available"]
    rig.latest = "2026-11-16.1700"
    state = None
    for t in (1060.0, 1120.0):  # two unforced checks
        state = ru.check(now=t)
    assert state["available"] and state["candidate"]["version"] == "2026-11-16.1700"


def test_template_has_no_periodic_check():
    """ACL-2668 decision 23 replaced the minute timer: checks run on events only."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "aclCheck(false)" not in html
    assert "aclCheck(true);" in html
