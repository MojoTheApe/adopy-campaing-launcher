"""ACL-2221: Instagram identity of a Page without a linked Instagram - read only, no creation by us.

The native "Use Facebook Page" button is an internal Ads Manager GraphQL mutation and the public
POST edge is rejected on our transport, so the product only DETECTS an existing Page-backed identity
and otherwise stops with an instruction. Every scenario here also asserts zero POSTs.
"""
import pytest

from src.fb_api import creator as cr, graph as g, instagram as ig, preflight_rules as pf
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run
from tests.test_acl1819_preflight import AuditMeta, _audit, _by

IG = "17841400000000001"
NO_IDENTITY = ("This Page has no Instagram identity. In Ads Manager press 'Use Facebook Page' "
               "for this Page, then run Check plan again.")


def _creatives(meta):
    return [o for o in meta.objects.values() if o["_kind"] == "creative"]


def _facebook_only(draft):
    draft.ad_sets[0].placements = {"mode": "manual", "facebook_positions": ["feed"], "instagram_positions": []}
    return draft


# ───────────────────────── discovery order and readback ─────────────────────────

@pytest.mark.parametrize("mode", ["page_backed_field", "page_backed_edge"])
def test_an_existing_page_backed_identity_is_used_in_the_creative(tmp_path, mode):
    meta = FakeMeta(instagram=mode)
    out, client = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and out.readback["ok"] is True
    assert all(c["object_story_spec"]["instagram_user_id"] == IG for c in _creatives(meta))
    assert len([c for c in out.readback["checks"] if c["field"] == "instagram_user_id"]) == 2
    assert not any("page_backed_instagram_accounts" in p for p, _ in meta.posts)
    assert all(p.rsplit("/", 1)[1] in {"campaigns", "adsets", "adimages", "adcreatives", "ads"}
               for p, _ in meta.posts)


def test_the_page_field_wins_over_the_edge_and_costs_one_read(tmp_path):
    meta = FakeMeta(instagram="page_backed_field")
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=60)
    got = ig.read_identity(client, ACT, "111")
    assert (got.status, got.ig_id, got.source) == (ig.FOUND, IG, "Page-backed")
    assert meta.page_backed_reads == 0


def test_the_linked_instagram_has_priority_over_a_page_backed_one(tmp_path):
    meta = FakeMeta(instagram="one")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and meta.page_backed_reads == 0
    assert _creatives(meta)[0]["object_story_spec"]["instagram_user_id"] == IG


def test_check_plan_accepts_an_existing_page_backed_identity_without_writes(tmp_path):
    meta = AuditMeta(instagram="page_backed_edge")
    row = _by(_audit(meta, _draft(tmp_path, ads=1)))["instagram_identity"]
    assert row["status"] == "ok" and IG in row["detail"] and "Page-backed" in row["detail"]
    assert meta.posts == []


# ───────────────────────── no identity at all: clear stop before any write ─────────────────────────

def test_check_plan_blocks_a_page_without_any_identity_with_the_instruction(tmp_path):
    meta = AuditMeta(instagram="none")
    out = _audit(meta, _draft(tmp_path, ads=1))
    row = _by(out)["instagram_identity"]
    assert row["status"] == "error" and row["detail"] == NO_IDENTITY
    assert pf.blocks(out.audit) and meta.posts == []


def test_publish_stops_before_any_campaign_media_or_ad_write(tmp_path):
    meta = FakeMeta(instagram="none")
    out, client = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == NO_IDENTITY + " Nothing more was created." and out.stop_kind == "plan"
    assert meta.posts == [] and client.writes == 0 and out.created == {}
    assert not any(c["object_story_spec"].get("instagram_user_id") for c in _creatives(meta))


@pytest.mark.parametrize("mode", ["none", "edge_error_100"])
def test_an_empty_or_unreadable_edge_is_never_a_confirmed_id(tmp_path, mode):
    got = ig.read_identity(g.GraphClient(FakeMeta(instagram=mode), sleep=lambda s: None, min_interval_s=0.0),
                           ACT, "111")
    assert got.status == ig.NONE and not got.ig_id and got.message == NO_IDENTITY


def test_a_page_id_is_never_accepted_as_the_instagram_identity(tmp_path):
    for mode in ("page_id_as_instagram", "edge_page_id"):
        meta = FakeMeta(instagram=mode)
        got = ig.read_identity(g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0), ACT, "111")
        assert got.status != ig.FOUND and not got.ig_id, mode
        out, _ = _run(FakeMeta(instagram=mode), _draft(tmp_path, ads=1))
        assert out.error and out.stop_kind == "plan", mode


# ───────────────────────── unknown outcomes keep their stops ─────────────────────────

@pytest.mark.parametrize("mode", ["edge_error", "edge_partial", "edge_malformed", "edge_ambiguous", "error"])
def test_unreadable_or_ambiguous_reads_stay_unknown_and_stop(tmp_path, mode):
    meta = FakeMeta(instagram=mode)
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "plan" and "did not confirm" in out.error
    assert meta.posts == [] and client.writes == 0
    row = _by(_audit(AuditMeta(instagram=mode), _draft(tmp_path, ads=1)))["instagram_identity"]
    assert row["status"] == "error" and "did not confirm" in row["detail"]


# ───────────────────────── Facebook-only plans are unaffected ─────────────────────────

def test_a_facebook_only_plan_reads_no_identity_and_publishes(tmp_path):
    meta = FakeMeta(instagram="none")
    out, _ = _run(meta, _facebook_only(_draft(tmp_path, ads=1)))
    assert out.error == "" and out.readback["ok"] is True
    assert meta.page_backed_reads == 0
    assert not any(c["object_story_spec"].get("instagram_user_id") for c in _creatives(meta))
    row = _by(_audit(AuditMeta(instagram="none"), _facebook_only(_draft(tmp_path, ads=1))))["instagram_identity"]
    assert row["status"] != "error"


# ───────────────────────── nothing in this feature can write ─────────────────────────

def test_the_identity_module_has_no_write_path():
    assert not hasattr(ig, "prepare_identity")
    import inspect
    assert ".post(" not in inspect.getsource(ig) and "write(" not in inspect.getsource(ig)


def test_the_cost_of_one_identity_is_two_reads():
    assert ig.READS_PER_PAGE == 2


def test_a_long_numeric_page_id_returned_as_the_identity_is_refused():
    page = "10000000000000123"

    class Stub:
        def __init__(self, row):
            self.row = row

        def get(self, path, params=None):
            return self.row(path)

    field = ig.read_identity(Stub(lambda p: {"id": page, "connected_page_backed_instagram_account": {"id": page}}),
                             ACT, page)
    edge = ig.read_identity(Stub(lambda p: {"id": page} if p == f"/{page}" else {"data": [{"id": page}]}),
                            ACT, page)
    assert field.status == edge.status == ig.UNKNOWN and not field.ig_id and not edge.ig_id
