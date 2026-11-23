"""ACL-2668 decision 15: an action the buyer starts himself works as Retry on a held profile.

Publish, a campaign command (incl. the shut-off button), Check plan and Open profile probe the profile: a
healthy page lifts the hold and the action goes on; a page that still has the problem keeps the hold and its
reason. Automatic reads keep respecting the hold. Offline: nothing here reaches Meta or a browser.
"""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend_api as api
from src.fb_api import account_blocks as blocks
from src.fb_api import connection
from src.fb_api.graph import Checkpoint, PolicyBlocked
from tests.test_acl1760_api_read import ACCOUNT, D, world  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(blocks, "_PATH_OVERRIDE", tmp_path / "account_holds.json")
    monkeypatch.setattr(connection, "settings", lambda: {"managed_sessions": True, "auto_reopen_manual": False})
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()
    blocks._TIMED_META.clear()
    yield
    api._COOLDOWN.clear()
    api._FROZEN_RELEASED.clear()
    blocks._TIMED_META.clear()


def test_buyer_actions_are_publish_command_check_plan_and_never_automatic_reads():
    for action in ("retry", "read", "publish", "command", "check_plan"):
        with blocks.problem_action(action):
            assert blocks.buyer_started(), action
    for action in ("schedule", "attention", "action"):
        with blocks.problem_action(action):
            assert not blocks.buyer_started(), action
    with blocks.problem_action("command", only_if_unset=True):
        assert not blocks.buyer_started(), "a defaulted action is not a person's press"
    with blocks.problem_action("publish"):
        with blocks.problem_action("action"):                  # the refresh after a publish is automatic
            assert not blocks.buyer_started()


def test_lift_after_probe_only_inside_a_buyer_action():
    blocks.note_stop("k", "B97", Checkpoint("c"))
    assert blocks.lift_after_probe("k") is False and blocks.is_held("k")
    with blocks.problem_action("schedule"):
        assert blocks.lift_after_probe("k") is False and blocks.is_held("k")
    with blocks.problem_action("command"):
        assert blocks.lift_after_probe("k") is True and not blocks.is_held("k")


# ───────────────────────── reads: command / Check plan ─────────────────────────

@pytest.mark.parametrize("action", ["command", "check_plan"])
def test_a_command_or_check_plan_on_a_healthy_page_lifts_the_hold_and_goes_on(world, action):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    api._start_cooldown(key, PolicyBlocked("p", code=368), ACCOUNT)
    with blocks.problem_action("action"):
        assert "Nothing was called." in api.read_via_api(ACCOUNT, D, D).error   # automatic: still refused
    assert blocks.is_held(key)
    with blocks.problem_action(action):
        done = api.read_via_api(ACCOUNT, D, D)
    assert done.state != "failed" and not blocks.is_held(key), done.error


def test_a_command_on_a_page_still_on_a_checkpoint_keeps_the_hold_with_its_reason(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    world.page.state_value = {"kind": "checkpoint", "has_token": False}
    api.read_via_api(ACCOUNT, D, D)
    with blocks.problem_action("command"):
        again = api.read_via_api(ACCOUNT, D, D)
    assert again.state == "failed" and blocks.hold_of(key)["kind"] == "checkpoint"
    assert "found during a campaign change" in blocks.refusal(key, ACCOUNT, "read")[0]


def test_campaign_command_sessions_and_the_shut_off_button_are_buyer_actions(monkeypatch):
    from src.web import campaign_commands

    seen = []

    @contextmanager
    def fake_session(account, *, commands=False, photo=False, **_kw):
        seen.append(blocks.buyer_started() and blocks.current_action() == "a campaign change")
        yield SimpleNamespace()

    monkeypatch.setattr(api, "_session", fake_session)
    from src.fb_ads_rpa import operations

    monkeypatch.setattr(api._as, "_profile_key", lambda label, *a, **k: "k-cmd")
    monkeypatch.setattr(operations, "profile_lock", lambda key: nullcontext())
    with campaign_commands.open_session("B97", True):
        pass
    with campaign_commands.open_session("B97", False):          # a preview is the person's press too
        pass
    monkeypatch.setattr(api, "_turn_off_locked", lambda *a, **k: seen.append(blocks.buyer_started()))
    api.turn_off("B97", "campaign", "123456789", requester="me")
    assert seen == [True, True, True]


# ───────────────────────── Publish ─────────────────────────

def _publisher(monkeypatch, key):
    from src.analysis_agent import account_spend as _as
    from src.web import publish_api

    monkeypatch.setattr(publish_api, "_resolve_entry",
                        lambda label: SimpleNamespace(profile_id="p", folder_id="F", multilogin_instance="A",
                                                      ad_account_id="act_123456"))
    monkeypatch.setattr(_as, "_profile_key", lambda label, *a, **k: key)
    monkeypatch.setattr(publish_api, "GraphClient", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(publish_api, "preflight_account", lambda client, act: {})
    monkeypatch.setattr(publish_api, "TargetingResolver", lambda *a, **k: None)
    return publish_api


def test_publish_on_a_held_profile_probes_and_lifts_the_hold_on_a_healthy_page(monkeypatch):
    publish_api = _publisher(monkeypatch, "k-pub")
    blocks.note_stop("k-pub", "B97", Checkpoint("c"), "schedule")

    def open_warm(self, label, pid):
        assert blocks.is_held("k-pub"), "the hold is lifted only after the page answered"
        self._finish_open(label, SimpleNamespace())
        return True

    monkeypatch.setattr(publish_api.ApiPublisher, "_open_warm", open_warm)
    with blocks.problem_action("publish"):
        publish_api.ApiPublisher().open("B97")
    assert not blocks.is_held("k-pub")


def test_publish_that_never_reaches_a_healthy_page_keeps_the_hold(monkeypatch):
    publish_api = _publisher(monkeypatch, "k-pub")
    blocks.note_stop("k-pub", "B97", Checkpoint("c"), "schedule")

    def open_warm(self, label, pid):
        raise publish_api.ApiPublishError("profile window did not come up")

    monkeypatch.setattr(publish_api.ApiPublisher, "_open_warm", open_warm)
    with blocks.problem_action("publish"), pytest.raises(publish_api.ApiPublishError):
        publish_api.ApiPublisher().open("B97")
    assert blocks.is_held("k-pub")


def test_an_automatic_open_of_a_held_profile_is_still_refused(monkeypatch):
    publish_api = _publisher(monkeypatch, "k-pub")
    blocks.note_stop("k-pub", "B97", Checkpoint("c"), "schedule")
    monkeypatch.setattr(publish_api.ApiPublisher, "_open_warm",
                        lambda self, label, pid: pytest.fail("a held profile is not touched automatically"))
    with pytest.raises(publish_api.ApiPublishError) as err:
        publish_api.ApiPublisher().open("B97")
    assert err.value.code == "account_blocked" and blocks.is_held("k-pub")


# ───────────────────────── Open profile ─────────────────────────

def test_open_profile_is_a_buyer_action_and_its_read_passes_a_hold(monkeypatch):
    """ACL-2653 made Open a person's read through any hold; ACL-2668 keeps it in the buyer actions."""
    from src.analysis_agent import account_refresh

    assert "open" in blocks.BUYER_ACTIONS
    with blocks.problem_action(blocks.action_for_trigger("open")):
        assert blocks.buyer_started()
    monkeypatch.setattr(account_refresh, "_profile_held", lambda label: True)
    assert account_refresh._held_blocks("Held", "open", True) is False
    assert account_refresh._held_blocks("Held", "schedule", True) is True


def test_a_person_write_leaves_a_code_31_hold_to_meta_answer(monkeypatch):
    blocks.note_stop("k", "B97", Checkpoint("c"))
    monkeypatch.setattr(blocks, "is_auth_hold", lambda key: True)
    with blocks.problem_action("command"):
        assert blocks.lift_after_probe("k") is False and blocks.is_held("k")
    with blocks.problem_action("retry"):
        assert blocks.lift_after_probe("k") is True
