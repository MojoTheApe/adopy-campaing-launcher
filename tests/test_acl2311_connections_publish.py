"""ACL-2311: Publish button in the Connections dialog; Connections reachable on a cached page."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "src" / "web" / "static" / "connections.js"
HARNESS = ROOT / "tests" / "acl2311_connections_harness.cjs"
TPL = ROOT / "src" / "web" / "templates" / "report.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(**scenario):
    out = subprocess.run(
        [shutil.which("node"), str(HARNESS), str(JS), json.dumps(scenario)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


@needs_node
def test_publish_renders_per_connection_lines_and_notes():
    r = _run(confirm=True, response={
        "ok": True, "published": True, "library": True, "bi": False,
        "notes": ["This computer has no BI connection to share."]})
    assert "POST /api/connections/publish" in r["calls"]
    assert r["lines"] == ["✓ Team library: published", "✕ BI: not published",
                          "✕ This computer has no BI connection to share."]
    assert r["msg"] == "Published." and r["disabled"] is False


@needs_node
@pytest.mark.parametrize("error", [
    "This computer has no team library token to share. This computer has no BI connection to share.",
    "The ACL connection file has no connection tab. Nothing was published.",
    "Could not write the ACL connection file. You need edit access to it.",
])
def test_publish_errors_are_shown_in_plain_words(error):
    r = _run(confirm=True, response={"ok": False, "error": error})
    assert r["msg"] == error and r["lines"] == []


@needs_node
def test_publish_network_failure_is_plain():
    r = _run(confirm=True, reject=True, response={})
    assert r["msg"] == "Could not reach the dashboard."


@needs_node
def test_nothing_is_written_without_confirmation():
    r = _run(confirm=False, response={"ok": True, "library": True, "bi": True, "notes": []})
    assert "POST /api/connections/publish" not in r["calls"]


def test_report_page_loads_the_static_script_and_has_no_inline_dialog():
    html = TPL.read_text(encoding="utf-8")
    assert '<script src="/static/connections.js"></script>' in html
    assert "cx-box" not in html  # one copy of the dialog: the static file


def test_cached_page_gets_current_connections_button_and_script():
    from src.web import report_store

    stored = report_store.StoredPage("<html><body><h1>old build</h1></body></html>", 0.0, [])
    page = report_store.decorate(stored)
    assert 'id="acl-stored-connections"' in page
    assert 'window.aclConnections.open()' in page
    assert '<script src="/static/connections.js"></script>' in page
    assert page.index("acl-stored-connections") < page.index("old build")


def test_script_does_not_touch_dom_at_load_time():
    src = JS.read_text(encoding="utf-8")
    top = src.split("function open()")[0]
    assert "document." not in top.replace("document.createElement", "")  # only inside functions
