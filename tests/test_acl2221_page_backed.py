"""Page-backed Instagram at the actual creator and closed transport seams."""
import json

import pytest

from src.fb_api import creator as cr, graph as g, instagram as ig, write_policy as wp
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run
from tests.test_acl1819_preflight import _by

IG = "17841400000000777"
PAGE = "111"
EDGE = f"/{PAGE}/page_backed_instagram_accounts"


class PageMeta(FakeMeta):
    def __init__(self, *, linked=False, existing=False, mode="ok", edge="empty"):
        super().__init__(instagram="one" if linked else "none")
        self.pb = IG if existing else ""
        self.mode, self.edge = mode, edge
        self.pinned = ""

    def pin_page(self, page):
        self.pinned = page
        return self.state()

    def request(self, method, path, params):
        if path == EDGE:
            self.gets.append((path, params))
            if self.edge == "denied":
                return {"status": 400, "body": {"error": {"code": 100, "message": "permission"}}}
            body = {"data": [{"id": self.pb}] if self.pb else []}
            if self.edge == "partial":
                body["paging"] = {"next": "more"}
            if self.edge == "malformed":
                body = {"data": [{"id": "bad"}]}
            return {"status": 200, "body": body}
        return super().request(method, path, params)

    def write(self, path, params, files=None, **kw):
        if path == EDGE:
            wp.check_instagram_prepare(kw.get("instagram_page", ""), "POST", path, params, files, self.pinned)
            self.posts.append((path, params))
            if self.mode == "unknown":
                return {"error": "call_unknown"}
            if self.mode != "missing":
                self.pb = IG
            return {"status": 200, "body": {"id": "17841400000000888" if self.mode == "mismatch" else IG}}
        return super().write(path, params, files=files, **kw)


def dry(meta, draft):
    client = g.GraphClient(meta, min_interval_s=0, max_calls=200, sleep=lambda _: None)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client))
    return creator.plan_only(cr.plan_from_draft(draft))


def test_actual_check_allows_page_preparation_without_writes(tmp_path):
    meta = PageMeta()
    out = dry(meta, _draft(tmp_path, ads=1))
    assert not out.error, out.error
    assert _by(out)["instagram_identity"]["status"] == "warning"
    assert any("Facebook Page" in w for w in out.warnings)
    assert meta.posts == []


def test_actual_publish_prepares_before_campaign_then_reuses(tmp_path):
    meta = PageMeta()
    draft = _draft(tmp_path, ads=1)
    out, _ = _run(meta, draft)
    assert not out.error, out.error
    assert meta.posts[0] == (EDGE, {})
    creative = next(o for o in meta.objects.values() if o["_kind"] == "creative")
    assert creative["object_story_spec"]["instagram_user_id"] == IG
    assert out.readback["ok"]
    again, _ = _run(meta, draft)
    assert not again.error, again.error
    assert sum(p == EDGE for p, _ in meta.posts) == 1


@pytest.mark.parametrize("edge", ["partial", "malformed", "denied"])
def test_unknown_edge_does_not_allow_creation(tmp_path, edge):
    meta = PageMeta(edge=edge)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error and not meta.posts


@pytest.mark.parametrize("mode", ["unknown", "missing", "mismatch"])
def test_uncertain_creation_is_never_replayed_across_new_creators(tmp_path, mode):
    meta = PageMeta(mode=mode)
    draft = _draft(tmp_path, ads=1)
    out, _ = _run(meta, draft)
    assert out.error and [p for p, _ in meta.posts] == [EDGE]
    # A different later invocation cannot trust a mismatched returned id either.
    meta.pb = ""
    later, _ = _run(meta, draft)
    assert later.error and [p for p, _ in meta.posts] == [EDGE]


def test_existing_linked_identity_takes_priority(tmp_path):
    meta = PageMeta(linked=True, existing=True)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert not out.error and all(p != EDGE for p, _ in meta.posts)


def test_invalid_media_stops_before_identity_write(tmp_path):
    meta = PageMeta()
    draft = _draft(tmp_path, ads=1)
    draft.ad_sets[0].ads[0].creative_path = str(tmp_path / "absent.png")
    out, _ = _run(meta, draft)
    assert out.error and not meta.posts


@pytest.mark.parametrize("page,path,params,files,pinned", [
    (PAGE, EDGE, {}, None, PAGE),
    (PAGE, EDGE + "?x=1", {}, None, PAGE),
    (PAGE, EDGE, {"access_token": "synthetic"}, None, PAGE),
    (PAGE, EDGE, {}, {"file": b"x"}, PAGE),
    (PAGE, EDGE, {}, None, "222"),
    ("bad", "/bad/page_backed_instagram_accounts", {}, None, "bad"),
])
def test_closed_python_policy(page, path, params, files, pinned):
    allowed = path == EDGE and page == pinned == PAGE and not params and not files
    if allowed:
        wp.check_instagram_prepare(page, "POST", path, params, files, pinned)
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check_instagram_prepare(page, "POST", path, params, files, pinned)


def test_preparation_reserves_budget_before_any_write(tmp_path):
    meta = PageMeta()
    client = g.GraphClient(meta, allow_writes=True, min_interval_s=0, max_calls=200, sleep=lambda _: None)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client))
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    # Force the actual prepare boundary to have fewer calls than a complete run needs.
    original = creator._resolve_name
    def resolve(*args):
        result = original(*args)
        client.max_calls = client.calls + 1
        return result
    creator._resolve_name = resolve
    out = creator.run(plan)
    assert out.error and not meta.posts


def test_facebook_only_never_reads_or_prepares_instagram(tmp_path):
    meta = PageMeta()
    draft = _draft(tmp_path, ads=1)
    plan = cr.plan_from_draft(draft)
    plan.ad_sets[0].placements = {"mode": "manual", "facebook_positions": ["feed"], "instagram_positions": []}
    client = g.GraphClient(meta, allow_writes=True, min_interval_s=0, max_calls=200, sleep=lambda _: None)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client))
    out = creator.run(plan)
    assert not out.error, out.error
    assert all(p != EDGE for p, _ in meta.posts + meta.gets)


def test_new_process_reconciles_unknown_result_only_by_same_page_read(tmp_path):
    meta = PageMeta(mode="unknown")
    draft = _draft(tmp_path, ads=1)
    first, _ = _run(meta, draft)
    assert first.error and len(meta.posts) == 1
    meta.pb = IG
    later, _ = _run(meta, draft)
    assert not later.error, later.error
    assert sum(p == EDGE for p, _ in meta.posts) == 1


def test_exclusive_intent_blocks_a_second_profile_before_dispatch(tmp_path, monkeypatch):
    first, second = PageMeta(), PageMeta()
    original = first.write
    seen = []
    def interleaved(*args, **kw):
        seen.append(ig.prepare_identity(g.GraphClient(second, allow_writes=True,
            min_interval_s=0, sleep=lambda _: None), PAGE))
        return original(*args, **kw)
    monkeypatch.setattr(first, "write", interleaved)
    out, _ = _run(first, _draft(tmp_path, ads=1))
    assert not out.error, out.error
    assert seen[0].status == ig.UNKNOWN and second.posts == []


def test_transport_python_and_isolated_javascript_enforce_same_page(tmp_path):
    import shutil
    import subprocess
    from src.fb_api import token_capture as tc
    from tests.test_acl1761_api_create import StubTab
    from tests.test_acl1896_instagram_identity import _NODE
    cases = {
        "good": {"path": EDGE, "params": {}, "instagram_page": PAGE},
        "foreign": {"path": "/222/page_backed_instagram_accounts", "params": {}, "instagram_page": "222"},
        "unflagged": {"path": EDGE, "params": {}},
        "params": {"path": EDGE, "params": {"name": "x"}, "instagram_page": PAGE},
        "files": {"path": EDGE, "params": {}, "files": {"file": "x"}, "instagram_page": PAGE},
        "query": {"path": EDGE + "?x=1", "params": {}, "instagram_page": PAGE},
        "slash": {"path": EDGE + "/", "params": {}, "instagram_page": PAGE},
        "form": {"path": EDGE, "params": {}, "form_page": PAGE, "instagram_page": PAGE},
        "active": {"path": EDGE, "params": {}, "children_active": True, "instagram_page": PAGE},
    }
    harness = _NODE.replace("{ method: 'POST', path: c.path, params: c.params }", "{ method: 'POST', ...c }")
    for mode in ("creation", "unbound", "readonly", "commands"):
        policy = (wp.policy_for_page(ACT, PAGE) if mode == "creation" else
                  wp.policy_for_page(ACT) if mode == "unbound" else
                  wp.policy_for_commands(ACT) if mode == "commands" else None)
        script = (harness.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20))
            .replace("NAME", json.dumps("__acl_ig"))
            .replace("CASES", "(" + json.dumps(cases) + ")")
            .replace("HELPER", json.dumps(tc.helper_source("__acl_ig", policy))))
        path = tmp_path / (mode + ".js")
        path.write_text(script, encoding="utf-8")
        done = subprocess.run([shutil.which("node"), str(path)], capture_output=True,
                              text=True, encoding="utf-8", timeout=30)
        assert done.returncode == 0, done.stderr
        replies = json.loads(done.stdout)
        for name, case in cases.items():
            allowed = mode == "creation" and name == "good"
            assert (replies[name].get("body") == {"id": "5"}) == allowed, (mode, name, replies[name])
            graph = tc.InPageGraph(StubTab(), allow_writes=mode in ("creation", "unbound"),
                allow_object_updates=mode == "commands", write_act=ACT,
                write_page=PAGE if mode == "creation" else "")
            kwargs = {k: v for k, v in case.items() if k not in ("path", "params")}
            if allowed:
                assert graph.write(case["path"], case["params"], **kwargs)["body"]
            else:
                with pytest.raises(ValueError):
                    graph.write(case["path"], case["params"], **kwargs)
        if mode == "commands":
            with pytest.raises(ValueError):
                graph.pin_page(PAGE)


def test_live_finding_fixture_distinguishes_observation_from_synthetic_preparation():
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures/ads_manager/fb_api_page_backed_2026_10_26.json"
    row = json.loads(fixture.read_text(encoding="utf-8"))
    assert row["observed"]["blocking_rule"] == "instagram_identity"
    assert row["observed"]["advertising_writes"] == 0
    assert row["synthetic"]["edge_after"]["data"][0]["id"] == IG
    assert "synthetic" in row["evidence_limit"]


def test_stale_prepare_cache_cannot_prepare_after_switch_to_facebook_only(tmp_path):
    meta = PageMeta()
    client = g.GraphClient(meta, allow_writes=True, min_interval_s=0, max_calls=200, sleep=lambda _: None)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client))
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    assert creator.plan_only(plan).error == ""
    assert creator._ig_cache[PAGE].status == ig.PREPARE
    plan.ad_sets[0].placements = {"mode": "manual", "facebook_positions": ["feed"], "instagram_positions": []}
    out = creator.run(plan)
    assert not out.error, out.error
    assert all(p != EDGE for p, _ in meta.posts)


@pytest.mark.parametrize("bad", [False, [], "", {}, {"id": "bad"}])
def test_malformed_page_field_never_means_permission_to_prepare(bad):
    class Malformed(PageMeta):
        def request(self, method, path, params):
            if path == "/" + PAGE:
                return {"status": 200, "body": {"connected_instagram_account": bad}}
            return super().request(method, path, params)
    meta = Malformed()
    client = g.GraphClient(meta, min_interval_s=0, sleep=lambda _: None)
    assert ig.read_identity(client, ACT, PAGE).status == ig.UNKNOWN
    assert all(p != EDGE for p, _ in meta.gets)


@pytest.mark.parametrize("when", ["pin", "read"])
def test_stop_during_preparation_never_dispatches_or_leaves_intent(tmp_path, when):
    state = {"cancelled": False, "reads": 0}
    class CancelMeta(PageMeta):
        def pin_page(self, page):
            result = super().pin_page(page)
            if when == "pin":
                state["cancelled"] = True
            return result
        def request(self, method, path, params):
            result = super().request(method, path, params)
            if path == EDGE:
                state["reads"] += 1
                if when == "read" and state["reads"] == 2:
                    state["cancelled"] = True
            return result
    meta = CancelMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1), cancelled=lambda: state["cancelled"])
    assert out.stop_kind == "cancelled" and meta.posts == []
    assert not ig.intent_path(PAGE).exists()
