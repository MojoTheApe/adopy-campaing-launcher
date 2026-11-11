"""ACL-2091 part A: persisted account holds, action context, reason texts, security-page classifier.

Offline: nothing here reaches Meta or a browser.
"""
import importlib
import json
import time
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend_api as api
from src.fb_api import account_blocks as blocks
from src.fb_api import connection
from src.fb_api.graph import (Checkpoint, LoggedOut, PageUnresponsive, PolicyBlocked, RateLimited,
                              classify_stop_url)
from tests.test_acl1760_api_read import ACCOUNT, D, world  # noqa: F401  (fixture)

KEY = "profile-key-1"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": False})
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()
    blocks._TIMED_META.clear()
    yield
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()
    blocks._TIMED_META.clear()


def _stop(cls, **kw):
    return cls("stop", **kw)


# ───────────────────────── holds ─────────────────────────

@pytest.mark.parametrize("stop, kind, code", [
    (Checkpoint("c"), "checkpoint", None),
    (LoggedOut("l", page_seen=True), "logged_out", None),
    (PolicyBlocked("p", code=368), "policy_block", 368),
    (PolicyBlocked("p", code=100, subcode=1404078), "policy_block", 1404078),
    (PolicyBlocked("p", code=100, subcode=2859015), "policy_block", 2859015),
    (PolicyBlocked("p", code=1404163), "policy_block", 1404163),
])
def test_stops_that_need_a_person_become_persisted_holds_without_a_timer(stop, kind, code):
    api._start_cooldown(KEY, stop, "B97")
    assert blocks.is_held(KEY) and api._cooldown_state(KEY) == (0.0, "")
    held = blocks.hold_of(KEY)
    assert held["kind"] == kind and held["code"] == code and held["label"] == "B97"
    msg, err = blocks.refusal(KEY, "B97", "publish")
    assert err == "account_blocked" and "Nothing was created." in msg
    assert "min" not in msg.lower().replace("minutes", "") or kind == "policy_block"   # no "waiting about N min"
    assert "waiting" not in msg.lower()
    if kind == "policy_block":
        assert f"error {code}" in msg


def test_hold_survives_a_reload_of_the_module_and_timed_kinds_do_not():
    api._start_cooldown(KEY, Checkpoint("c"), "B97")
    api._start_cooldown("k-rate", RateLimited("r"), "B98")
    importlib.reload(blocks)
    assert blocks.is_held(KEY)
    assert not blocks.is_held("k-rate")
    assert api._cooldown_state("k-rate")[1] == "rate_limited"   # timed, in memory only


def test_hold_file_holds_no_urls_or_tokens(tmp_path):
    api._start_cooldown(KEY, Checkpoint("https://facebook.com/checkpoint/?token=EAAB123"), "B97")
    raw = blocks._path().read_text(encoding="utf-8")
    assert "EAAB" not in raw and "http" not in raw
    assert set(json.loads(raw)[KEY]) == {"kind", "code", "label", "action", "at", "target_id"}


def test_frozen_tab_is_timed_until_its_replacement_is_spent_then_a_hold_without_expiry():
    api._start_cooldown(KEY, PageUnresponsive("f"), "B97")
    assert not blocks.is_held(KEY) and api._cooldown_state(KEY)[0] > 590      # first freeze: replaceable, timed
    assert api.release_frozen_tab_cooldown(KEY) is True                       # the one replacement
    api._start_cooldown(KEY, PageUnresponsive("f"), "B97")                    # the replacement froze too
    held = blocks.hold_of(KEY)
    assert held and held["kind"] == "page_unresponsive"
    assert api._cooldown_state(KEY) == (0.0, "")                              # no timer: only Retry lifts it
    msg, _ = blocks.refusal(KEY, "B97", "read")
    assert "Press Retry" in msg and "min" not in msg.split("Press Retry")[1]


def test_frozen_tab_is_a_hold_at_once_when_sessions_are_not_managed(monkeypatch):
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": False})
    api._start_cooldown(KEY, PageUnresponsive("f"), "B97")
    assert blocks.is_held(KEY)


def test_token_only_logged_out_is_not_a_hold_and_keeps_the_one_minute_timer():
    api._start_cooldown(KEY, LoggedOut("token invalid", code=190), "B97")
    assert not blocks.is_held(KEY)
    left, kind = api._cooldown_state(KEY)
    assert kind == "logged_out" and 50 < left <= 60
    msg, code = blocks.refusal(KEY, "B97", "read", timed=(left, kind))
    assert "session of B97 expired" in msg and "Nothing was called." in msg


def test_rate_limit_stays_timed_and_is_explained():
    with blocks.problem_action("schedule"):
        api._start_cooldown(KEY, RateLimited("r"), "B97")
    assert not blocks.is_held(KEY)
    left, kind = api._cooldown_state(KEY)
    msg, _ = blocks.refusal(KEY, "B97", "read", timed=(left, kind))
    assert msg.startswith("Meta rate limit on B97, reached during a scheduled read. Waiting about 5 min.")


def test_unreadable_or_corrupt_hold_file_fails_open_and_refuses_nobody():
    blocks._path().parent.mkdir(parents=True, exist_ok=True)
    blocks._path().write_text("{not json", encoding="utf-8")
    assert blocks.is_held(KEY) is False and blocks.refusal(KEY, "B97", "read") is None
    blocks._path().write_text("[1, 2]", encoding="utf-8")
    assert blocks.is_held(KEY) is False
    # a stop on a corrupt file still produces a hold afterwards
    api._start_cooldown(KEY, Checkpoint("c"), "B97")
    assert blocks.is_held(KEY)


def test_an_unwritable_hold_file_keeps_the_old_timer(monkeypatch):
    monkeypatch.setattr(blocks, "_save", lambda holds: False)
    api._start_cooldown(KEY, Checkpoint("c"), "B97")
    assert not blocks.is_held(KEY) and api._cooldown_state(KEY)[1] == "checkpoint"


def test_two_labels_on_one_profile_both_show_the_block_and_clear_lifts_it_for_both():
    api._start_cooldown(KEY, Checkpoint("c"), "B97")
    seen = blocks.view({"B97": KEY, "B98": KEY, "Other": "other-key"})
    assert set(seen) == {"B97", "B98"}
    assert "B97" in seen["B97"]["reason"] and "B98 (same profile)" not in seen["B97"]["reason"]
    assert "(same profile)" in seen["B98"]["reason"]
    assert seen["B97"]["can_retry"] is True and seen["B97"]["needs_confirm"] is False
    assert blocks.clear(KEY, "retry") is True and blocks.view({"B97": KEY, "B98": KEY}) == {}
    assert blocks.clear(KEY, "retry") is False


def test_policy_block_view_asks_for_confirmation_and_1404163_uses_the_catalog_step():
    api._start_cooldown(KEY, PolicyBlocked("p", code=1404163), "B97")
    seen = blocks.view({"B97": KEY})["B97"]
    assert seen["needs_confirm"] is True
    assert "Meta Business Support" in seen["next"]
    api._start_cooldown("k2", PolicyBlocked("p", code=368), "B98")
    assert "Retrying soon risks a checkpoint or a ban." in blocks.view({"B98": "k2"})["B98"]["next"]


# ───────────────────────── action ─────────────────────────

def test_action_names_and_nesting():
    assert blocks.current_action() == "a read"
    with blocks.problem_action("attention"):
        assert blocks.current_action() == "a background read"
        with blocks.problem_action("read", only_if_unset=True):
            assert blocks.current_action() == "a background read"          # a default never overrides
        with blocks.problem_action("publish"):
            assert blocks.current_action() == "Publish"
    assert blocks.current_action() == "a read"
    assert [blocks.ACTION_TEXT[k] for k in ("schedule", "action", "retry", "check_plan", "command")] == [
        "a scheduled read", "a refresh after your action", "Retry", "Check plan", "a campaign change"]
    assert blocks.action_for_trigger("open") == "attention" and blocks.action_for_trigger("schedule:08:00".split(":")[0]) == "schedule"


def test_the_1732_case_a_background_read_froze_and_a_publish_at_1734_explains_itself(monkeypatch):
    from src.web import publish_api

    key = "k-B97"
    monkeypatch.setattr(publish_api, "_resolve_entry",
                        lambda label: SimpleNamespace(profile_id="p", folder_id="F", multilogin_instance="A",
                                                      ad_account_id="act_123456"))
    from src.analysis_agent import account_spend as _as

    monkeypatch.setattr(_as, "_profile_key", lambda label, *a, **k: key)
    api._FROZEN_RELEASED.add(key)                       # the replacement was already spent
    with blocks.problem_action("attention"):
        api._start_cooldown(key, PageUnresponsive("f"), "B97")
    pub = publish_api.ApiPublisher()
    with pytest.raises(publish_api.ApiPublishError) as err:
        pub.open("B97")
    text = str(err.value)
    assert "stopped answering during a background read" in text and "Nothing was created." in text
    assert err.value.code == "account_blocked"


# ───────────────────────── refusals in the read path ─────────────────────────

def test_a_held_profile_refuses_the_next_read_with_account_blocked_and_never_crashes(world):  # noqa: F811
    world.page.state_value = {"kind": "checkpoint", "has_token": False}
    first = api.read_via_api(ACCOUNT, D, D)
    assert first.state == "failed"
    assert blocks.is_held(api._as._profile_key(ACCOUNT))
    assert api._cooldown_state(api._as._profile_key(ACCOUNT)) == (0.0, "")
    calls = len(world.page.specs)
    again = api.read_via_api(ACCOUNT, D, D)
    assert again.state == "failed" and "Nothing was called." in again.error
    assert "Facebook security check" in again.error and "during Read" in again.error
    assert len(world.page.specs) == calls, "a held profile is not touched"


def test_a_login_page_holds_but_the_wording_is_logged_out(world):  # noqa: F811
    world.page.state_value = {"kind": "login", "has_token": False}
    api.read_via_api(ACCOUNT, D, D)
    held = blocks.hold_of(api._as._profile_key(ACCOUNT))
    assert held["kind"] == "logged_out"
    again = api.read_via_api(ACCOUNT, D, D)
    assert "is logged out of Facebook" in again.error and "press Retry" in again.error


def test_an_old_style_in_memory_cooldown_still_refuses_without_a_hold(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    api._COOLDOWN[key] = (time.monotonic() + 300, "rate_limited")
    again = api.read_via_api(ACCOUNT, D, D)
    assert "Meta rate limit" in again.error and "Nothing was called." in again.error


# ───────────────────────── security-page classifier ─────────────────────────

@pytest.mark.parametrize("path, want", [
    ("/login/", "login"), ("/login.php", "login"), ("/recover/initiate", "login"),
    ("/checkpoint/1234", "checkpoint"), ("/two_step_verification/x", "checkpoint"),
    ("/two_factor/remember_browser", "checkpoint"), ("/hacked-protection/", "checkpoint"),
    ("/hacked-protection/start", "checkpoint"), ("/adsmanager/manage/campaigns", ""), ("", ""),
])
def test_classify_stop_url(path, want):
    assert classify_stop_url(path) == want


def test_all_three_detectors_know_hacked_protection_and_two_factor():
    from src.fb_api import token_capture, warm

    for path in ("/hacked-protection/", "/two_factor/x", "/checkpoint/1"):
        assert isinstance(warm._url_stop("https://www.facebook.com" + path), Checkpoint)
        tab = SimpleNamespace(browser_url=lambda p=path: "https://www.facebook.com" + p)
        tc = token_capture.InPageGraph(tab)
        assert isinstance(tc._timeout_stop(TimeoutError()), Checkpoint)
    # the page script mirrors the Python rule
    src = open(token_capture.__file__, encoding="utf-8").read()
    assert "two_step_verification|two_factor|hacked-protection" in src


def test_page_evidence_marks_logged_out_stops():
    from src.fb_api import warm

    login = warm._url_stop("https://www.facebook.com/login/")
    assert isinstance(login, LoggedOut) and login.page_seen is True
    assert LoggedOut("token").page_seen is False


def test_a_tab_that_answers_ends_a_frozen_hold_but_not_a_checkpoint_hold():
    api._FROZEN_RELEASED.add(KEY)
    api._start_cooldown(KEY, PageUnresponsive("f"), "B97")
    assert blocks.is_held(KEY)
    api.note_tab_recovered(KEY)
    assert not blocks.is_held(KEY) and KEY not in api._FROZEN_RELEASED
    api._start_cooldown("k-cp", Checkpoint("c"), "B98")
    api.note_tab_recovered("k-cp")
    assert blocks.is_held("k-cp")
