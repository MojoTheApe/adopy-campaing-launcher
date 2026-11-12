"""ACL-2595: Meta 31/3858385 "Please authenticate your account" stops the account and says what to do."""
from __future__ import annotations

import pytest

from src.analysis_agent import account_spend_api as api
from src.fb_api import account_blocks as blocks
from src.fb_api import graph as g
from src.fb_api import plain_errors as pe
from tests.test_acl1761_api_create import FakeMeta, _draft, _publisher

WANT = ("Meta asks to confirm this account (security check). Open Ads Manager in this profile and complete "
        "the check, then publish again. Existing ads keep running.")


class AuthMeta(FakeMeta):
    """Meta refuses the first ad write with 31/3858385 (the owner's live case)."""

    def write(self, path, params, **kw):
        if path.endswith("/ads"):
            self.posts.append((path, dict(params)))
            return {"status": 400, "body": {"error": {
                "code": 31, "error_subcode": 3858385, "message": "Error validating the ad",
                "error_user_title": "Please authenticate your account",
                "error_user_msg": "You won't be able to create or modify ads until you've authenticated.",
                "fbtrace_id": "AbCdEfGhIjK"}}}
        return super().write(path, params, **kw)


@pytest.fixture(autouse=True)
def _holds(tmp_path, monkeypatch):
    monkeypatch.setattr(blocks, "_PATH_OVERRIDE", tmp_path / "holds.json")
    monkeypatch.setattr(api, "_replacement_spent", lambda key: True)
    api._COOLDOWN.clear()
    yield
    api._COOLDOWN.clear()


def _second(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.name = d.ad_sets[0].name = "MX Leads 2"
    return d


def test_graph_classifies_31_3858385_as_an_account_stop():
    meta = AuthMeta()
    c = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(g.AuthRequired) as e:
        c.post("/act_1/ads", {"name": "x"})
    assert e.value.kind == "checkpoint" and (e.value.code, e.value.subcode) == (31, 3858385)
    assert c.stopped is e.value
    with pytest.raises(g.AuthRequired):         # no further call of any kind
        c.post("/act_1/ads", {"name": "y"})
    assert len(meta.posts) == 1


def test_other_31_codes_stay_ordinary_refusals():
    assert not g.is_auth_required(31, 1) and not g.is_auth_required(100, 3858385)


def test_later_campaigns_of_the_account_are_not_started(tmp_path):
    meta = AuthMeta()
    pub = _publisher(meta)
    first = pub.publish_draft(_draft(tmp_path, ads=1))
    assert first["success"] is False and first["failure"]["key"] == "auth_required"
    assert isinstance(pub.stop, g.AuthRequired)
    posts, gets = len(meta.posts), len(meta.gets)
    second = pub.publish_draft(_second(tmp_path))
    assert second["not_started"] is True
    assert second["error"] == pe.AUTH_NOT_STARTED
    assert "not started: account needs authentication" in second["error"].lower()
    assert (len(meta.posts), len(meta.gets)) == (posts, gets), "no write and no extra read for the blocked account"


def test_the_result_card_says_what_to_do_and_keeps_the_raw_text_in_details(tmp_path):
    pub = _publisher(AuthMeta())
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["error"].startswith(WANT)
    assert "Meta refused this step" not in res["error"]
    assert res["failure"]["line"] == WANT
    assert "Please authenticate your account" in str(res["failure"]["details"])
    assert "authenticate" not in res["error"].lower()
    # what was already created stays and is named
    assert any("Left in Meta" in x for x in res["failure"]["left"])


def test_the_block_is_persisted_and_shown_in_the_account_panel(tmp_path):
    pub = _publisher(AuthMeta())
    pub.publish_draft(_draft(tmp_path, ads=1))
    stop = pub.stop
    api._start_cooldown("prof-1", stop, "KLD_BFL_F91")        # what publish_draft does for every stop
    held = blocks.hold_of("prof-1")
    assert held and held["kind"] == "checkpoint" and held["code"] == 31
    shown = blocks.view({"KLD_BFL_F91": "prof-1"})["KLD_BFL_F91"]
    assert f"{shown['reason']} {shown['next']}" == WANT
    msg, code = blocks.refusal("prof-1", "KLD_BFL_F91", "publish")
    assert code == "account_blocked" and msg.startswith(WANT)
    # a plain checkpoint keeps its own text
    api._start_cooldown("prof-2", g.Checkpoint("c"), "B97")
    assert "Solve the checkpoint" in blocks.view({"B97": "prof-2"})["B97"]["next"]


def test_publish_draft_persists_the_hold_itself(tmp_path, monkeypatch):
    from src.web import publish_api

    seen = []
    monkeypatch.setattr(publish_api, "_start_cooldown", lambda label, stop: seen.append((label, stop)))
    pub = _publisher(AuthMeta())
    pub.publish_draft(_draft(tmp_path, ads=1))
    assert seen and isinstance(seen[0][1], g.AuthRequired)


def test_another_account_in_the_same_job_is_not_affected(tmp_path):
    blocked = _publisher(AuthMeta())
    blocked.publish_draft(_draft(tmp_path, ads=1))
    ok = _publisher(FakeMeta())
    res = ok.publish_draft(_second(tmp_path))
    assert ok.stop is None and res["not_started"] is False and res["success"] is True
