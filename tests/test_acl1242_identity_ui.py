"""ACL-1242: экран входа Google, подтверждение имени, плашка в шапке (identity.js, Node-харнесс)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "src" / "web" / "static" / "identity.js"
HARNESS = ROOT / "tests" / "acl1242_identity_harness.cjs"
TPL = ROOT / "src" / "web" / "templates" / "report.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

BODY = ("Use your Adopy Google account. ACL reads only your name and email, so your reports and "
        "library edits show who made them. It adds no access to your Google files.")


def _run(**scenario):
    out = subprocess.run(
        [shutil.which("node"), str(HARNESS), str(JS), json.dumps(scenario)],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def _who(state="verified", **extra):
    base = {"ok": True, "state": state, "email": "alice@adopy.com", "name": "Alice A", "cm_name": "",
            "cm_checked": False, "stale_days": 0, "client_available": True}
    base.update(extra)
    return base


@needs_node
def test_none_shows_the_sign_in_screen_without_not_now():
    r = _run(identity=_who("none", email="", name=""))
    first = r["steps"][0]
    assert BODY in first["text"] and "Sign in to Adopy Campaign Manager" in first["text"]
    assert first["buttons"] == ["Sign in with Google"], "«Not now» не должно быть до неудачи (D1)"
    assert r["overlay"] and r["calls"] == ["GET /api/identity"]


@needs_node
@pytest.mark.parametrize("state", ["verified", "stale"])
def test_signed_in_states_show_no_overlay_but_a_chip(state):
    r = _run(identity=_who(state, cm_name="Alice", stale_days=9 if state == "stale" else 0))
    assert r["overlay"] is False
    assert r["steps"][0]["chip"].startswith("Signed in as Alice")
    assert r["steps"][0]["chipTitle"].startswith("alice@adopy.com")
    if state == "stale":
        assert "Google sign-in not checked for 9 days" in r["steps"][0]["chip"]


@needs_node
def test_wrong_domain_offers_another_account_and_not_now():
    r = _run(identity=_who("none"),
             signIn={"ok": False, "reason": "wrong_domain",
                     "message": "That is not an Adopy Google account. Sign in with your @adopy.com account."},
             click=[{"id": "identity-go"}])
    step = r["steps"][-1]
    assert "That is not an Adopy Google account. Sign in with your @adopy.com account." in step["text"]
    assert step["buttons"] == ["Not now", "Try another account"]
    assert r["steps"][0]["buttons"] == ["Sign in with Google"]


@needs_node
def test_offline_failure_is_plain_and_allows_not_now():
    r = _run(identity=_who("none"), reject=True, click=[{"id": "identity-go"}])
    step = r["steps"][-1]
    assert ("Could not reach Google. Check the internet connection and try again. "
            "You can keep working and sign in later.") in step["text"]
    assert step["buttons"] == ["Not now", "Try again"]


@needs_node
def test_not_now_declines_and_closes():
    r = _run(identity=_who("none"), reject=True, other={"ok": True, "identity": _who("declined", email="")},
             click=[{"id": "identity-go"}, {"label": "Not now"}])
    assert "POST /api/identity/decline" in r["calls"]
    assert r["overlay"] is False
    assert r["steps"][-1]["chip"] == "Sign in"


@needs_node
def test_unavailable_has_not_now_at_once_and_a_disabled_sign_in():
    r = _run(identity=_who("unavailable", client_available=False))
    assert ("Google sign-in is not set up in this version. Update ACL, or ask your team lead. "
            "You can keep working.") in r["steps"][0]["text"]
    assert r["steps"][0]["buttons"] == ["Not now", "Sign in with Google"]


@needs_node
def test_declined_returns_once_per_session():
    assert _run(identity=_who("declined", email=""), shown=False)["overlay"] is True
    assert _run(identity=_who("declined", email=""), shown=True)["overlay"] is False


@needs_node
def test_expired_shows_the_expired_text_and_not_now_closes_without_a_call():
    r = _run(identity=_who("expired", cm_name="Alice"), reject=True,
             click=[{"id": "identity-go"}, {"label": "Not now"}])
    assert "Your Google sign-in has expired. Sign in again so your reports show your name." in r["steps"][0]["text"]
    assert not any("decline" in c for c in r["calls"]) and r["overlay"] is False


@needs_node
def test_confirm_step_saves_the_name_and_closes():
    r = _run(identity=_who("none"),
             signIn={"ok": True, "state": "verified", "name_step": "confirm", "suggestions": ["Alice"],
                     "identity": _who("verified")},
             names=[{"ok": True, "identity": _who("verified", cm_name="Alice", cm_checked=True)}],
             click=[{"id": "identity-go"}, {"id": "identity-save"}])
    assert ("Confirm your name as it appears in the CM column of the Stats workbook "
            "(or as BI knows you).") in r["steps"][1]["text"]
    assert any(c.startswith("POST /api/identity/name") and '"name":"Alice"' in c for c in r["calls"])
    assert r["overlay"] is False and r["steps"][-1]["chip"].startswith("Signed in as Alice")


@needs_node
def test_name_not_listed_shows_suggestions_then_allows_override():
    r = _run(identity=_who("none"),
             signIn={"ok": True, "state": "verified", "name_step": "confirm", "suggestions": [],
                     "identity": _who("verified")},
             names=[{"ok": False, "reason": "not_listed", "suggestions": ["Alicia"]},
                    {"ok": True, "identity": _who("verified", cm_name="Alise")}],
             click=[{"id": "identity-go"}, {"id": "identity-save", "input": "Alise"}, {"id": "identity-save"}])
    miss = r["steps"][2]
    assert '"Alise" is not in the campaign manager list. Did you mean: Alicia?' in miss["text"]
    assert "Use this name anyway" in miss["buttons"] and "Alicia" in miss["buttons"]
    assert '"confirm_unlisted":true' in r["calls"][-1]
    assert r["overlay"] is False


@needs_node
def test_who_works_here_names_the_previous_person_and_the_email():
    r = _run(identity=_who("none"),
             signIn={"ok": True, "state": "verified", "name_step": "who", "suggestions": ["Bob"],
                     "identity": _who("verified")},
             click=[{"id": "identity-go"}])
    step = r["steps"][1]
    assert "This computer was set up for Bob. You are signed in as alice@adopy.com. Who works here?" in step["text"]
    assert "Bob" in step["buttons"]


@needs_node
def test_the_overlay_only_talks_to_identity_routes():
    """Оверлей рисует DOM; единственные запросы — /api/identity*."""
    r = _run(identity=_who("none"), reject=True, click=[{"id": "identity-go"}])
    assert all("/api/identity" in c for c in r["calls"])


def test_template_carries_the_new_header_and_script():
    tpl = TPL.read_text(encoding="utf-8")
    assert '<script src="/static/identity.js"></script>' in tpl
    assert 'id="identity-chip"' in tpl and "Viewing</label>" in tpl


def test_connections_dialog_mounts_the_identity_actions():
    src = (ROOT / "src" / "web" / "static" / "connections.js").read_text(encoding="utf-8")
    assert "cx-identity" in src and "mountActions" in src
