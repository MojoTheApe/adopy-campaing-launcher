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


# ───────────────────────── B1: a person's Publish lifts the hold ─────────────────────────

PID = "550e8400-e29b-41d4-a716-446655440000"


def _rig(monkeypatch, tmp_path, meta):
    from types import SimpleNamespace

    from src.analysis_agent import account_spend as _as
    from src.web import publish_api
    from tests.test_acl1761_api_create import ACT, _PageOverMeta
    from tests.test_acl2262_builder_opens_profile import closed_profile_rig

    closed_profile_rig(monkeypatch, tmp_path, None, PID)
    monkeypatch.setattr(_as, "_profile_key", lambda label: "prof-1")
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: SimpleNamespace(
        ad_account_id=ACT, profile_id=PID, folder_id="", multilogin_instance="A"))
    monkeypatch.setattr(publish_api, "_open_profile", lambda *a: pytest.fail("the cold path must not be used"))
    monkeypatch.setattr(publish_api, "_make_graph", lambda tab, act, *, writes=True: _PageOverMeta(meta, writes))
    return publish_api


def test_a_person_started_publish_lifts_the_auth_hold_and_the_job_runs(monkeypatch, tmp_path):
    publish_api = _rig(monkeypatch, tmp_path, FakeMeta())
    api._start_cooldown("prof-1", g.AuthRequired("a", code=31, subcode=3858385), "KLD_BFL_F91")
    assert blocks.is_held("prof-1")
    pub = publish_api.ApiPublisher()
    pub.open("KLD_BFL_F91")                      # not refused by the hold
    assert not blocks.is_held("prof-1") and pub._client is not None


def test_other_checkpoint_holds_still_refuse_publish_and_need_retry(monkeypatch, tmp_path):
    publish_api = _rig(monkeypatch, tmp_path, FakeMeta())
    api._start_cooldown("prof-1", g.Checkpoint("c"), "KLD_BFL_F91")
    with pytest.raises(publish_api.ApiPublishError):
        publish_api.ApiPublisher().open("KLD_BFL_F91")
    assert blocks.is_held("prof-1")


def test_when_meta_still_asks_the_hold_is_set_again_after_one_refused_call(monkeypatch, tmp_path):
    meta = AuthMeta()
    publish_api = _rig(monkeypatch, tmp_path, meta)
    api._start_cooldown("prof-1", g.AuthRequired("a", code=31, subcode=3858385), "KLD_BFL_F91")
    pub = publish_api.ApiPublisher()
    pub.open("KLD_BFL_F91")
    assert not blocks.is_held("prof-1")
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["failure"]["key"] == "auth_required" and blocks.is_held("prof-1")
    assert sum(1 for p, _ in meta.posts if p.endswith("/ads")) == 1
    n = len(meta.posts)
    assert pub.publish_draft(_second(tmp_path))["not_started"] is True and len(meta.posts) == n


# ───────────────────────── N1-N3 ─────────────────────────

def test_the_read_path_raises_auth_required():
    class T:
        def request(self, method, path, params=None):
            return {"status": 400, "body": {"error": {"code": 31, "error_subcode": 3858385, "message": "m"}}}

        def state(self):
            return {"kind": "ads_manager", "has_token": True}

    c = g.GraphClient(T(), sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(g.AuthRequired):
        c.get("/act_1/campaigns", {})
    assert c.stopped is not None


def test_the_title_alone_with_code_31_is_enough_and_other_titles_are_not():
    assert g.is_auth_required(31, None, "Please authenticate your account")
    assert not g.is_auth_required(31, None, "Something else") and not g.is_auth_required(100, None, "Please authenticate your account")
    meta = FakeMeta()
    c = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    meta.write = lambda *a, **k: {"status": 400, "body": {"error": {
        "code": 31, "message": "m", "error_user_title": "Please authenticate your account"}}}
    with pytest.raises(g.AuthRequired):
        c.post("/act_1/ads", {"name": "x"})


def test_validate_page_raises_auth_required():
    from tests.test_acl1761_api_create import ACT

    class T:
        calls = 0

        def validate_page(self, act, page_id, link):
            T.calls += 1
            return {"status": 400, "body": {"error": {"code": 31, "error_subcode": 3858385, "message": "m"}}}

    c = g.GraphClient(T(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(g.AuthRequired):
        c.validate_page(ACT, "123456789012345")
    with pytest.raises(g.AuthRequired):         # the next campaign does not repeat the call
        c.validate_page(ACT, "123456789012345")
    assert T.calls == 1
