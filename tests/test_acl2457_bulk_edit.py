"""ACL-2457: bulk edit of budget, age, gender, languages and placements for selected campaigns.

Graph is `BulkMeta` in memory (transport of `GraphClient`, same shape as ACL-1800's `FakeMeta`); the
write policy runs on the Python line exactly as on the real client. Nothing touches Meta or Multilogin.
"""
from __future__ import annotations

import copy
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import bulk_edit as be  # noqa: E402
from src.fb_api import commands as cmd  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1800_campaign_commands import ACT, ACT_DIGITS, ACCOUNT, make_client  # noqa: E402

C1, C2, C3, C_ABO = "1100001", "1100002", "1100003", "1100004"
S1, S2, S3, S4, S5 = "2100001", "2100002", "2100003", "2100004", "2100005"

BASE_T = {"geo_locations": {"countries": ["US"]}, "age_min": 18, "age_max": 65,
          "targeting_automation": {"advantage_audience": 0}, "locales": [6],
          "flexible_spec": [{"interests": [{"id": "6003", "name": "Cars"}]}],
          "publisher_platforms": ["facebook", "instagram"],
          "facebook_positions": ["feed", "story", "facebook_reels", "instream_video"],
          "instagram_positions": ["stream", "story", "reels"]}


class BulkMeta:
    def __init__(self):
        self.currency = "USD"
        self.objs: dict[str, dict] = {}
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.fail_next: dict[str, str] = {}      # id -> applied | lost | refuse | drop
        self.mutate_on_get: dict[str, tuple[str, object]] = {}
        self.unreadable: set[str] = set()
        self._seed()

    def _seed(self):
        def camp(cid, name, **kw):
            self.objs[cid] = {"id": cid, "_kind": "campaign", "name": name, "account_id": ACT_DIGITS,
                              "status": "PAUSED", "effective_status": "PAUSED", **kw}

        def aset(sid, cid, name, **kw):
            t = copy.deepcopy(BASE_T)
            t.update(kw.pop("targeting", {}))
            self.objs[sid] = {"id": sid, "_kind": "adset", "name": name, "campaign_id": cid,
                              "status": "PAUSED", "targeting": t, **kw}

        camp(C1, "Camp one", daily_budget="5000")
        camp(C2, "Camp two", lifetime_budget="90000")
        camp(C3, "Camp three", daily_budget="2500")
        camp(C_ABO, "Camp ABO")
        aset(S1, C1, "Set 1a")
        aset(S2, C1, "Set 1b", targeting={"age_min": 25})
        aset(S3, C2, "Set 2a", targeting={"genders": [2]})
        aset(S4, C3, "Set 3a")
        aset(S5, C_ABO, "Set abo", daily_budget="1000")

    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def reacquire(self, timeout=60.0):
        return self.state()

    def request(self, method, path, params=None):
        assert method == "GET"
        self.gets.append(path)
        if path == f"/{ACT}":
            return {"status": 200, "body": {"account_id": ACT_DIGITS, "currency": self.currency, "account_status": 1}}
        oid = path.strip("/")
        obj = self.objs.get(oid)
        if obj is None or oid in self.unreadable:
            return {"status": 400, "body": {"error": {"code": 100, "message": "Object does not exist"}}}
        row = {k: v for k, v in obj.items() if not k.startswith("_")}
        if obj["_kind"] == "campaign":
            sets = [copy.deepcopy({k: v for k, v in a.items() if k in ("id", "name", "status", "targeting")})
                    for a in self.objs.values() if a["_kind"] == "adset" and a["campaign_id"] == oid]
            row["adsets"] = {"data": sets}
        if oid in self.mutate_on_get:
            field, value = self.mutate_on_get.pop(oid)
            obj[field] = value
        return {"status": 200, "body": row}

    def write(self, path, params, files=None, filename=""):
        wp.check_object_update("POST", path, params, files)
        self.posts.append((path, copy.deepcopy(params)))
        oid = path.strip("/")
        mode = self.fail_next.pop(oid, None)
        ((field, value),) = params.items()
        if mode == "lost":
            return {"error": "call_unknown"}
        if mode == "refuse":
            return {"status": 400, "body": {"error": {"code": 100, "message": "Invalid parameter"}}}
        if mode == "drop" and field == "targeting":
            value = {**value, "age_min": 18}
        stored = copy.deepcopy(value) if field == "targeting" else str(value)
        self.objs[oid][field] = stored
        if mode == "applied":
            return {"error": "call_unknown"}
        return {"status": 200, "body": {"success": True}}


class FakeResolver:
    locale_names = {"Spanish": "Spanish (All)", "French": "French (France)"}
    locale_groups = {23: {24: "Spanish (Spain)", 25: "Spanish (Latin America)"}}
    _ids = {"Spanish": 23, "French": 9}

    def __init__(self, client=None):
        pass

    def locale(self, name):
        if name not in self._ids:
            raise be.PlanError(f"Audience language {name!r} was not found by Meta targeting search")
        return self._ids[name]


class World:
    def __init__(self, tmp_path):
        self.meta = BulkMeta()
        self.store = cmd.OperationStore(tmp_path / "history.jsonl")
        self.sessions: list[tuple[str, bool]] = []
        self.sleeps: list[float] = []

    @contextmanager
    def open_session(self, account, commands):
        self.sessions.append((account, commands))
        yield SimpleNamespace(client=make_client(self.meta, commands=commands), act=ACT)

    def preview(self, changes, campaigns, **kw):
        return be.preview(self.store, self.open_session, account=ACCOUNT, actor="Dana", changes=changes,
                          campaigns=campaigns, resolver_factory=FakeResolver, **kw)

    def execute(self, op, key="key-0000001"):
        return cmd.execute(self.store, self.open_session, revision_id=op["revision_id"], idempotency_key=key,
                           spawn=lambda fn: fn(), sleep=self.sleeps.append)

    def final(self, op):
        return cmd.status(self.store, op["op_id"])


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def ids(*xs):
    return [{"id": x} for x in xs]


# ───────────────────────── request parsing ─────────────────────────

def test_changes_keep_only_what_was_asked():
    assert be.parse_changes({"budget": " 25.50 ", "age_min": "21", "gender": "women", "age_max": None,
                             "languages": ["Spanish", "spanish "], "placements": ""}) == {
        "budget": "25.50", "age_min": 21, "gender": "women", "languages": ["Spanish"]}


@pytest.mark.parametrize("bad", [
    {}, None, [], {"budget": ""}, {"budget": "0x10"}, {"budget": "-5"}, {"age_min": 12}, {"age_max": 66},
    {"age_min": 40, "age_max": 30}, {"age_min": True}, {"gender": "other"}, {"languages": "Spanish"},
    {"languages": ["x"]}, {"languages": ["Aa"] * 6}, {"placements": "everything"}, {"name": "x"},
])
def test_bad_changes_are_refused(bad):
    with pytest.raises(cmd.CommandRefused):
        be.parse_changes(bad)


@pytest.mark.parametrize("bad", [None, [], "1100001", [{"id": "12"}], [{"id": "abc"}]])
def test_bad_campaign_lists_are_refused(bad):
    with pytest.raises(cmd.CommandRefused):
        be.parse_campaigns(bad)


def test_too_many_campaigns_refused():
    with pytest.raises(cmd.CommandRefused):
        be.parse_campaigns([str(1100000 + i) for i in range(be.MAX_CAMPAIGNS + 1)])


# ───────────────────────── preview ─────────────────────────

def test_preview_shows_before_after_and_writes_nothing(world):
    pv = world.preview({"budget": "60", "age_min": 21, "gender": "men"}, ids(C1, C2))
    assert pv["state"] == "Preview" and pv["command"] == "bulk_edit"
    assert world.meta.posts == [] and world.sessions == [(ACCOUNT, False)]
    by = {(a["id"], a["field"]): a for a in pv["affected"]}
    assert by[(C1, "daily_budget")]["before_display"] == "50.00 USD daily"
    assert by[(C1, "daily_budget")]["after_display"] == "60.00 USD daily"
    assert by[(C2, "lifetime_budget")]["before_display"] == "900.00 USD lifetime"     # as the campaign has it
    assert by[(C2, "lifetime_budget")]["after_display"] == "60.00 USD lifetime"
    t1 = by[(S1, "targeting")]
    assert t1["before"] == {"age_min": 18, "gender": "all"} and t1["after"] == {"age_min": 21, "gender": "men"}
    assert "min age 18" in t1["before_display"] and "gender Men" in t1["after_display"]
    assert by[(S2, "targeting")]["keys"] == ["age_min", "gender"]
    assert by[(S3, "targeting")]["keys"] == ["age_min", "gender"]


def test_only_changed_fields_enter_the_plan(world):
    pv = world.preview({"age_min": 25, "gender": "all"}, ids(C1))
    # Set 1b is already min 25 and "all": nothing to do there; Set 1a changes age only.
    assert [(a["id"], a["keys"]) for a in pv["affected"]] == [(S1, ["age_min"])]
    assert {(s["id"], s["reason"]) for s in pv["skipped"]} == {(S2, "Already at the requested values")}


def test_unreadable_campaign_and_abo_budget_are_skipped_with_reasons(world):
    world.meta.unreadable.add(C3)
    pv = world.preview({"budget": "10"}, ids(C1, C3, C_ABO))
    assert [a["id"] for a in pv["affected"]] == [C1]
    reasons = {s["id"]: s["reason"] for s in pv["skipped"]}
    assert reasons[C3].startswith("Could not read this campaign")
    assert "ad sets" in reasons[C_ABO]


def test_archived_campaign_and_adset_are_skipped(world):
    world.meta.objs[C3]["status"] = "ARCHIVED"
    world.meta.objs[S2]["status"] = "ARCHIVED"
    pv = world.preview({"gender": "men"}, ids(C1, C3))
    assert [a["id"] for a in pv["affected"]] == [S1]
    assert {s["id"]: s["reason"] for s in pv["skipped"]} == {C3: "Campaign is archived", S2: "Ad set is archived"}


def test_advantage_audience_adsets_skip_age_and_gender_only(world):
    world.meta.objs[S1]["targeting"]["targeting_automation"] = {"advantage_audience": 1}
    pv = world.preview({"gender": "men"}, ids(C1))
    assert [a["id"] for a in pv["affected"]] == [S2]
    assert "Advantage+" in pv["skipped"][0]["reason"]
    world.meta.objs[S1]["targeting"]["publisher_platforms"] = ["facebook"]
    pv2 = world.preview({"placements": "advantage_plus"}, ids(C1))
    assert sorted(a["id"] for a in pv2["affected"]) == [S1, S2]


def test_adsets_without_readable_targeting_are_skipped(world):
    world.meta.objs[S1]["targeting"] = None
    pv = world.preview({"gender": "women"}, ids(C1))
    assert [a["id"] for a in pv["affected"]] == [S2]
    assert pv["skipped"][0]["reason"] == "Targeting could not be read"


def test_age_that_would_cross_is_skipped_per_adset(world):
    world.meta.objs[S2]["targeting"]["age_max"] = 30      # min 25..30
    pv = world.preview({"age_min": 40}, ids(C1))
    assert [a["id"] for a in pv["affected"]] == [S1]
    assert "minimum above the maximum" in pv["skipped"][0]["reason"]


def test_languages_resolved_and_group_expansion_counts_as_equal(world):
    world.meta.objs[S1]["targeting"]["locales"] = [24, 25]    # Meta expanded "Spanish (All)"
    pv = world.preview({"languages": ["Spanish"]}, ids(C1))
    assert [a["id"] for a in pv["affected"]] == [S2]                # Set 1a is already Spanish
    a = pv["affected"][0]
    assert a["after"] == {"languages": [23]} and "Spanish (All)" in a["after_display"]
    assert a["before"] == {"languages": [6]}


def test_unknown_language_refuses_the_whole_preview(world):
    with pytest.raises(cmd.CommandRefused, match="not found"):
        world.preview({"languages": ["Klingon"]}, ids(C1))
    assert world.meta.posts == []


def test_placement_presets(world):
    world.meta.objs[S2]["targeting"].update(publisher_platforms=["facebook"], facebook_positions=["feed"])
    world.meta.objs[S2]["targeting"].pop("instagram_positions")
    pv = world.preview({"placements": "acl_default"}, ids(C1))
    assert [a["id"] for a in pv["affected"]] == [S2]                # Set 1a already has the ACL default
    assert pv["affected"][0]["before"] == {"placements": "custom"}
    pv = world.preview({"placements": "advantage_plus"}, ids(C1))
    assert sorted(a["id"] for a in pv["affected"]) == [S1, S2]


def test_call_limit_refuses_oversized_plans(world, monkeypatch):
    monkeypatch.setenv("ACL_FB_API_MAX_CALLS", "8")
    with pytest.raises(cmd.CommandRefused, match="Select fewer campaigns"):
        world.preview({"gender": "men"}, ids(C1, C2, C3))


def test_no_adsets_skips_targeting_but_not_budget(world):
    world.meta.objs.pop(S4)
    pv = world.preview({"budget": "99", "gender": "men"}, ids(C3))
    assert [a["field"] for a in pv["affected"]] == ["daily_budget"]
    assert pv["skipped"][0]["reason"] == "No ad sets to edit"


# ───────────────────────── execute ─────────────────────────

def test_execute_writes_only_changed_fields_and_reads_back(world):
    before = copy.deepcopy(world.meta.objs[S1]["targeting"])
    pv = world.preview({"budget": "60", "age_min": 21, "gender": "men"}, ids(C1))
    assert world.meta.posts == []
    assert world.execute(pv)["state"] == "Running"
    done = world.final(pv)
    assert done["state"] == "Succeeded", done
    posts = dict(world.meta.posts)
    assert posts[f"/{C1}"] == {"daily_budget": "6000"}
    t = posts[f"/{S1}"]["targeting"]
    assert (t["age_min"], t["genders"]) == (21, [1])
    # Everything the buyer did not touch travels unchanged inside the replaced targeting.
    for key in ("geo_locations", "age_max", "locales", "flexible_spec", "publisher_platforms",
                "facebook_positions", "instagram_positions", "targeting_automation"):
        assert t[key] == before[key]
    assert {a["result"] for a in done["affected"]} == {"saved"}
    assert all(a["saved"] is not None for a in done["affected"])
    assert world.meta.objs[C1]["daily_budget"] == "6000"
    assert sorted(world.sessions) == [(ACCOUNT, False), (ACCOUNT, True)]


def test_gender_all_removes_the_key(world):
    pv = world.preview({"gender": "all"}, ids(C2))          # Set 2a is women -> all
    world.execute(pv)
    (path, params), = world.meta.posts
    assert path == f"/{S3}" and "genders" not in params["targeting"]
    assert world.final(pv)["state"] == "Succeeded"


def test_placements_and_languages_are_patched_and_verified(world):
    pv = world.preview({"placements": "advantage_plus", "languages": ["Spanish", "French"]}, ids(C1))
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Succeeded", done
    for sid in (S1, S2):
        t = world.meta.objs[sid]["targeting"]
        assert "publisher_platforms" not in t and "facebook_positions" not in t and "instagram_positions" not in t
        assert t["locales"] == [23, 9]


def test_acl_default_replaces_extra_positions(world):
    world.meta.objs[S1]["targeting"]["audience_network_positions"] = ["classic"]
    world.meta.objs[S1]["targeting"]["publisher_platforms"] = ["facebook", "instagram", "audience_network"]
    pv = world.preview({"placements": "acl_default"}, ids(C1))
    world.execute(pv)
    t = world.meta.objs[S1]["targeting"]
    assert "audience_network_positions" not in t and t["publisher_platforms"] == ["facebook", "instagram"]


def test_nothing_is_written_without_execute(world):
    world.preview({"budget": "10"}, ids(C1, C2, C3))
    assert world.meta.posts == []


def test_a_refused_campaign_does_not_stop_the_others(world):
    world.meta.fail_next[C1] = "refuse"
    pv = world.preview({"budget": "10"}, ids(C1, C2, C3))
    world.execute(pv)
    done = world.final(pv)
    res = {a["id"]: a["result"] for a in done["affected"]}
    assert res == {C1: "refused", C2: "saved", C3: "saved"}
    assert done["state"] == "Failed" and "2 of 3" in done["message"]
    assert world.meta.objs[C2]["lifetime_budget"] == "1000" and world.meta.objs[C3]["daily_budget"] == "1000"
    assert world.meta.objs[C1]["daily_budget"] == "5000"


def test_mismatch_is_reported_and_others_continue(world):
    world.meta.fail_next[S1] = "drop"          # Meta answers OK but keeps age 18
    pv = world.preview({"age_min": 21}, ids(C1, C3))
    world.execute(pv)
    done = world.final(pv)
    res = {a["id"]: a for a in done["affected"]}
    assert res[S1]["result"] == "mismatch" and "saved min age 18" in res[S1]["note"]
    assert res[S4]["result"] == "saved"


def test_unknown_write_is_settled_by_readback_and_writing_goes_on(world):
    world.meta.fail_next[C1] = "applied"       # reached Meta, the answer was lost
    pv = world.preview({"budget": "10"}, ids(C1, C2))
    world.execute(pv)
    done = world.final(pv)
    res = {a["id"]: a for a in done["affected"]}
    assert res[C1]["result"] == "saved" and "after a timeout" in res[C1]["note"]
    assert res[C2]["result"] == "saved"
    assert len(world.meta.posts) == 2 and world.sleeps == [cmd.RECONCILE_PAUSE_S]      # nothing was retried


def test_unknown_write_that_cannot_be_settled_stops_the_rest_without_retry(world):
    world.meta.fail_next[C1] = "lost"          # never reached Meta
    pv = world.preview({"budget": "10"}, ids(C1, C2))
    world.execute(pv)
    done = world.final(pv)
    res = {a["id"]: a["result"] for a in done["affected"]}
    assert res == {C1: "unknown", C2: "not_attempted"}
    assert done["state"] == "Outcome unknown" and len(world.meta.posts) == 1


def test_changed_since_preview_writes_nothing(world):
    pv = world.preview({"budget": "10"}, ids(C1, C2))
    world.meta.objs[C2]["lifetime_budget"] = "77700"
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == "Failed" and "changed since the preview" in done["message"]
    assert world.meta.posts == []


def test_adset_targeting_changed_since_preview_writes_nothing(world):
    pv = world.preview({"gender": "men"}, ids(C1))
    world.meta.objs[S2]["targeting"]["genders"] = [2]
    world.execute(pv)
    assert world.final(pv)["state"] == "Failed" and world.meta.posts == []


def test_same_key_is_idempotent_and_preview_is_used_once(world):
    pv = world.preview({"budget": "10"}, ids(C1))
    world.execute(pv)
    world.execute(pv)                           # the same key again: no second write
    assert len(world.meta.posts) == 1
    with pytest.raises(cmd.CommandRefused):
        world.execute(pv, key="key-0000002")


def test_nothing_to_change_cannot_be_confirmed(world):
    pv = world.preview({"budget": "50"}, ids(C1))
    assert pv["affected"] == []
    with pytest.raises(cmd.CommandRefused):
        world.execute(pv)


def test_history_blocked_stops_before_any_post(world, monkeypatch):
    pv = world.preview({"budget": "10"}, ids(C1))
    real = world.store.save
    calls = {"n": 0}

    def flaky(op):
        calls["n"] += 1
        if calls["n"] >= 2:                      # the first save (Running) passes, the "sending" one fails
            raise cmd.HistoryUnavailable("disk full")
        return real(op)

    monkeypatch.setattr(world.store, "save", flaky)
    world.execute(pv)
    assert world.meta.posts == []
    assert world.final(pv)["affected"][0]["result"] == "not_sent"


# ───────────────────────── write policy for the new fields ─────────────────────────

GOOD_T = {"geo_locations": {"countries": ["US"]}, "age_min": 21, "age_max": 40, "genders": [1],
          "locales": [6, 9], "publisher_platforms": ["facebook"], "facebook_positions": ["feed"],
          "flexible_spec": [{"interests": [{"id": "1", "name": "x"}]}], "custom_audiences": [{"id": "5"}]}


def test_policy_accepts_lifetime_budget_and_targeting():
    assert wp.check_object_update("POST", f"/{C1}", {"lifetime_budget": "9000"}) == "lifetime_budget"
    assert wp.check_object_update("POST", f"/{S1}", {"targeting": GOOD_T}) == "targeting"
    assert wp.check_object_update("POST", f"/{S1}", {"targeting": json.dumps(GOOD_T)}) == "targeting"


@pytest.mark.parametrize("patch", [
    {"age_min": 12}, {"age_max": 66}, {"age_min": 50, "age_max": 20}, {"age_min": "21"}, {"age_min": True},
    {"genders": [3]}, {"genders": []}, {"genders": ["1"]}, {"locales": []}, {"locales": ["6"]},
    {"locales": [0]}, {"publisher_platforms": ["tiktok"]}, {"facebook_positions": ["marketplace"]},
    {"instagram_positions": []}, {"access_token": "x"}, {"nested": {"execution_options": ["validate_only"]}},
])
def test_policy_refuses_bad_targeting_keys(patch):
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{S1}", {"targeting": {**GOOD_T, **patch}})


@pytest.mark.parametrize("value", [None, {}, [], "", "not json", 5, ["x"]])
def test_policy_refuses_non_object_targeting(value):
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{S1}", {"targeting": value})


def test_policy_still_refuses_targeting_next_to_another_field_and_on_edges():
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{S1}", {"targeting": GOOD_T, "status": "ACTIVE"})
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{ACT}/adsets", {"targeting": GOOD_T})
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{S1}", {"targeting": dict(GOOD_T, blob="x" * 25000)})


def test_creation_policy_unchanged_by_the_new_fields():
    pol = wp.policy_for_page(ACT)
    assert "object_update" not in pol
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{S1}", {"targeting": GOOD_T})


# ───────────────────────── availability and endpoints ─────────────────────────

def test_availability_disables_bulk_on_rpa(monkeypatch):
    from src.web import campaign_commands as cc

    monkeypatch.setattr(cc, "api_enabled", lambda: False)
    av = cc.availability()
    assert av["bulk"] == {"available": False, "reason": "API backend only", "max_campaigns": 0}
    data, code = cc.run_bulk_preview({"account": "A", "changes": {"budget": "1"}, "campaigns": ["1100001"]})
    assert code == 409 and data["error"] == "API backend only"


def test_availability_enables_bulk_on_api_loopback(monkeypatch):
    from src.web import campaign_commands as cc

    monkeypatch.setattr(cc, "api_enabled", lambda: True)
    monkeypatch.setenv("ACL_BIND_HOST", "127.0.0.1")
    av = cc.availability()
    assert av["bulk"]["available"] and 1 <= av["bulk"]["max_campaigns"] <= be.MAX_CAMPAIGNS


def test_endpoint_preview_and_execute_roundtrip(world, monkeypatch, tmp_path):
    from src.web import campaign_commands as cc

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "api_enabled", lambda: True)
    monkeypatch.setenv("ACL_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(cc, "_sessions", lambda: world.open_session)
    monkeypatch.setattr(cmd, "_spawn_thread", lambda fn: fn())
    monkeypatch.setattr(cc, "_refresh_after_write", lambda op, sess: None)
    data, code = cc.run_bulk_preview({"account": ACCOUNT, "actor": "Dana", "campaigns": [C1],
                                      "changes": {"budget": "12"}})
    assert code == 200 and data["state"] == "Preview"
    out, code = cc.run_execute({"revision_id": data["revision_id"], "idempotency_key": "key-endpoint-1"})
    assert code in (200, 202)
    assert world.meta.objs[C1]["daily_budget"] == "1200"
    data, code = cc.run_bulk_preview({"account": ACCOUNT, "campaigns": [], "changes": {"budget": "12"}})
    assert code == 400


# ───────────────────────── the in-page policy line (node) ─────────────────────────

_NODE_TARGETING = r"""
const TOKENV = __TOKEN__;
const calls = [];
const fakeFetch = async (url, init) => { calls.push({ url: String(url), body: String(init.body || '') });
  return { status: 200, text: async () => JSON.stringify({ success: true }), headers: { get: () => null } }; };
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams',
             __HELPER__)(G, loc, doc, { getEntriesByType: () => [] }, fakeFetch, URL, URLSearchParams);
const h = G[__NAME__];
const P = (path, params) => h.call({ method: 'POST', path: path, params: params });
const T = __GOOD__;
(async () => {
  const out = {};
  out.good = await P('/2100001', { targeting: T });
  out.good_str = await P('/2100001', { targeting: JSON.stringify(T) });
  out.life = await P('/1100001', { lifetime_budget: '9000' });
  out.age = await P('/2100001', { targeting: Object.assign({}, T, { age_min: 12 }) });
  out.cross = await P('/2100001', { targeting: Object.assign({}, T, { age_min: 50, age_max: 20 }) });
  out.gender = await P('/2100001', { targeting: Object.assign({}, T, { genders: [3] }) });
  out.locales = await P('/2100001', { targeting: Object.assign({}, T, { locales: ['6'] }) });
  out.platform = await P('/2100001', { targeting: Object.assign({}, T, { publisher_platforms: ['tiktok'] }) });
  out.nested = await P('/2100001', { targeting: Object.assign({}, T, { x: { access_token: 'y' } }) });
  out.empty = await P('/2100001', { targeting: {} });
  out.two = await P('/2100001', { targeting: T, status: 'ACTIVE' });
  out.edge = await P('/act_1234567890/adsets', { targeting: T });
  out.big = await P('/2100001', { targeting: Object.assign({}, T, { blob: 'x'.repeat(25000) }) });
  out.calls = calls.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_page_helper_targeting_policy_in_node():
    import shutil
    import subprocess
    import tempfile

    from src.fb_api import token_capture as tc
    from tests.test_acl1800_campaign_commands import TOKEN

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_bulk_test"
    text = (_NODE_TARGETING.replace("__TOKEN__", json.dumps(TOKEN)).replace("__NAME__", json.dumps(name))
            .replace("__GOOD__", json.dumps(GOOD_T))
            .replace("__HELPER__", json.dumps(tc.helper_source(name, wp.policy_for_commands(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    for k in ("good", "good_str", "life"):
        assert out[k]["body"] == {"success": True}, (k, out[k])
    for k in ("age", "cross", "gender", "locales", "platform", "nested", "empty", "big"):
        assert out[k] == {"error": "write_refused", "why": "targeting"}, (k, out[k])
    assert out["two"] == {"error": "write_refused", "why": "key"} or out["two"]["why"] in ("key", "targeting")
    assert out["edge"] == {"error": "write_refused", "why": "path"}
    assert out["calls"] == 3


# ───────────────────────── the page helper acm_bulk.js (node) ─────────────────────────

_NODE_HELPER = r"""
const fs = require('fs');
const root = { document: undefined };
const load = (f) => new Function('window', 'globalThis', fs.readFileSync(f, 'utf8') + '\n')(root, root);
// acm_commands.js picks `window` when defined, else globalThis; give it a root with fetch/crypto stubs.
global.window = root; root.fetch = () => Promise.reject(new Error('no network in this test'));
const src = fs.readFileSync(process.argv[2], 'utf8').replace("typeof window !== 'undefined' ? window : globalThis", 'root');
new Function('root', src)(root);
const src2 = fs.readFileSync(process.argv[3], 'utf8').replace("typeof window !== 'undefined' ? window : globalThis", 'root');
new Function('root', src2)(root);
const B = root.acmCommands.bulk, out = {};
out.rpa = B.state({ available: false, code: 'rpa_backend', reason: 'Needs ACL_FB_BACKEND=api', bulk: { available: false, reason: 'API backend only', max_campaigns: 0 } });
out.old_server = B.state({ available: true });
out.ok_state = B.state({ available: true, bulk: { available: true, reason: '', max_campaigns: 6 } });
const avail = { available: true, bulk: { available: true, max_campaigns: 2 } };
const row = (id, acc, extra) => Object.assign({ level: 'campaign', id: id, account: acc, key: 'campaign:' + id }, extra || {});
out.plan_ok = B.plan([row(1, 'A'), row(2, 'A')], avail).ok;
out.plan_two_accounts = B.plan([row(1, 'A'), row(2, 'B')], avail).reason;
out.plan_cap = B.plan([row(1, 'A'), row(2, 'A'), row(3, 'A')], avail).reason;
out.plan_busy = B.plan([row(1, 'A', { busy: true })], avail).reason;
out.plan_none = B.plan([], avail).reason;
out.plan_off = B.plan([row(1, 'A')], { available: false, code: 'rpa_backend' }).reason;
out.c_empty = B.buildChanges({}, 'USD').error;
out.c_ok = B.buildChanges({ budget: ' 12.5 ', age_min: '21', age_max: '', gender: 'men', languages: 'Spanish, french ,Spanish', placements: 'advantage_plus' }, 'USD').changes;
out.c_age = B.buildChanges({ age_min: '12' }, 'USD').error;
out.c_cross = B.buildChanges({ age_min: '50', age_max: '20' }, 'USD').error;
out.c_budget = B.buildChanges({ budget: '1.234' }, 'USD').error;
out.c_zero_dec = B.buildChanges({ budget: '12.5' }, 'JPY').error;
out.c_lang = B.buildChanges({ languages: 'a' }, 'USD').error;
out.c_preset = B.buildChanges({ placements: 'everything' }, 'USD').error;
const op = { account: 'Acc', currency: 'USD', affected: [
  { campaign_id: '1', campaign_name: 'C1', id: '1', level: 'campaign', field: 'daily_budget', before_display: '50.00 USD daily', after_display: '60.00 USD daily', result: 'saved' },
  { campaign_id: '1', campaign_name: 'C1', id: '9', level: 'adset', name: 'S1', field: 'targeting', before_display: 'gender All', after_display: 'gender Men', result: 'refused', note: 'Meta refused: x' },
  { campaign_id: '2', campaign_name: 'C2', id: '2', level: 'campaign', field: 'lifetime_budget', before_display: 'a', after_display: 'b', result: 'saved' } ],
  skipped: [{ campaign_id: '3', campaign_name: 'C3', id: '3', level: 'campaign', name: 'C3', field: 'budget', reason: 'Budget is set on the ad sets, not on the campaign' }] };
const m = B.model(op);
out.model = { title: m.title, changes: m.changes, campaigns: m.campaigns, skipped: m.skippedCount, can: m.canConfirm,
  states: m.groups.map((g) => [g.id, g.state]), labels: m.groups[0].lines.map((l) => l.label), res: m.groups[0].lines[1].resultText };
out.model_empty = B.model({ affected: [], skipped: [] }).canConfirm;
process.stdout.write(JSON.stringify(out));
"""


def test_bulk_helper_logic_in_node(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = tmp_path / "bulk_helper.js"
    script.write_text(_NODE_HELPER, encoding="utf-8")
    static = ROOT / "src" / "web" / "static"
    done = subprocess.run([node, str(script), str(static / "acm_commands.js"), str(static / "acm_bulk.js")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["rpa"] == {"enabled": False, "reason": "API backend only", "max": 0}
    assert out["old_server"]["enabled"] is False and out["ok_state"] == {"enabled": True, "reason": "", "max": 6}
    assert out["plan_ok"] is True
    assert out["plan_two_accounts"] == "Select campaigns from one account"
    assert out["plan_cap"] == "At most 2 campaigns per bulk edit"
    assert out["plan_busy"] == "The selected campaigns are busy with another command"
    assert out["plan_none"] == "Select campaigns first" and out["plan_off"] == "API backend only"
    assert out["c_empty"] == "Fill in at least one field to change."
    assert out["c_ok"] == {"budget": "12.5", "age_min": 21, "gender": "men",
                           "languages": ["Spanish", "french"], "placements": "advantage_plus"}
    assert "13 to 65" in out["c_age"] and "above the maximum" in out["c_cross"]
    assert "two decimals" in out["c_budget"] and "whole" in out["c_zero_dec"]
    assert "2 to 60" in out["c_lang"] and "preset" in out["c_preset"]
    assert out["model"] == {"title": "Bulk edit: 3 changes on 2 campaigns", "changes": 3, "campaigns": 2, "skipped": 1,
                            "can": True, "states": [["1", "partial"], ["2", "saved"], ["3", "skipped"]],
                            "labels": ["Daily budget", 'Ad set "S1"'], "res": "Meta refused"}
    assert out["model_empty"] is False
