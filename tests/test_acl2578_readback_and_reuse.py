"""ACL-2578: `_<digits>` on a stored image name is not a readback mismatch; a re-run never reuses a name. Fakes only."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import name_receipt as nr  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _publisher, _run  # noqa: E402
from tests.test_acl2250_copy_counter import BASE, _names  # noqa: E402


@pytest.mark.parametrize("expected,actual,result", [
    ("acl-ab.png", "acl-ab.png_105", "acl-ab.png"),
    ("acl-ab.png", "acl-ab.png_1", "acl-ab.png"),
    ("acl-ab.png", "acl-ab.png", "acl-ab.png"),
    ("acl-ab.png", "acl-cd.png_105", "acl-cd.png_105"),         # another image stays a difference
    ("acl-ab.png", "acl-ab.png_x", "acl-ab.png_x"),
    ("acl-ab.png", "acl-ab.png_105_7", "acl-ab.png_105_7"),
    ("acl-ab.png", "", ""),
])
def test_stored_image_name_accepts_only_a_digit_suffix(expected, actual, result):
    assert cr.stored_image_name(expected, actual) == result


def _suffixing_meta(suffix="_105"):
    """Library names come back with Meta's `_<digits>` suffix; the hash is not the md5, so the name is compared."""
    meta = FakeMeta(hash_fn=lambda d: hashlib.sha1(d).hexdigest())
    real = meta.write

    def write(path, params, *a, **k):
        got = real(path, params, *a, **k)
        if path.endswith("/adimages"):
            for h in list(meta.images):
                if not meta.images[h].endswith(suffix):
                    meta.images[h] += suffix
        return got
    meta.write = write
    return meta


def test_readback_passes_when_meta_appends_a_digit_suffix_to_the_image_name(tmp_path):
    meta = _suffixing_meta()
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and out.readback["ok"] is True, out.mismatches
    assert any(c["field"] == "image_file" and c["ok"] for c in out.readback["checks"])


def test_readback_still_fails_for_a_genuinely_different_image_name(tmp_path):
    meta = _suffixing_meta()
    real = meta.request

    def request(method, path, params=None):
        got = real(method, path, params)
        if path.endswith("/adimages") and isinstance(got.get("body"), dict):
            for row in got["body"].get("data", []):
                row["name"] = "acl-0000000000000000.png_105"
        return got
    meta.request = request
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert [m["field"] for m in out.mismatches] == ["image_file"]


def _stop_at_first_image(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adimages"] = "unsent"
    first = _publisher(meta).publish_draft(_draft(tmp_path / "a", ads=2))
    assert first["error"] and first["campaign_id"]
    return meta, first


@pytest.fixture
def dirs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return tmp_path


def test_rerun_after_a_stop_at_the_first_image_reuses_campaign_and_ad_set_with_a_replaced_creative(dirs):
    meta, first = _stop_at_first_image(dirs)
    d = _draft(dirs / "b", ads=3)                           # replaced creative file and one more ad
    d.ad_sets[0].ads[0].creative_path = str(dirs / "b" / "replaced.png")
    (dirs / "b" / "replaced.png").write_bytes(b"another picture")
    res = _publisher(meta).publish_draft(d)                 # a new job = a new publisher
    assert res["error"] is None and " copy " not in (res["published_as"] or "")
    assert res["campaign_id"] == first["campaign_id"] and res["reused"].get("campaign")
    assert _names(meta) == [BASE] and _names(meta, "adset") == [BASE]
    assert sum(1 for o in meta.objects.values() if o["_kind"] == "ad") == 3


def test_stop_message_names_the_copy_that_a_changed_draft_really_gets(dirs):
    meta, first = _stop_at_first_image(dirs)
    assert f'"{BASE} copy 1"' in first["error"] and "_2" not in first["error"]
    d = _draft(dirs / "b", ads=2)
    d.ad_sets[0].daily_budget = 77.0                        # the campaign settings differ: no reuse
    res = _publisher(meta).publish_draft(d)
    assert res["error"] is None and res["published_as"] == BASE + " copy 1"


def test_left_over_known_by_receipt_is_never_given_the_same_name_when_the_listing_misses_it(dirs, monkeypatch):
    meta, first = _stop_at_first_image(dirs)
    assert nr.load(ACT, BASE)["campaign_id"] == first["campaign_id"]
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])      # search lag
    d = _draft(dirs / "b", ads=2)
    d.ad_sets[0].daily_budget = 77.0                        # not reusable
    res = _publisher(meta).publish_draft(d)
    assert res["error"] is None and res["published_as"] == BASE + " copy 1"
    assert sorted(_names(meta)) == [BASE, BASE + " copy 1"], "never two campaigns with one name"


# ── B1: a left-over with no ads is judged by its settings, not by Meta's derived placement aggregate ──

def test_left_over_without_ads_is_reused_even_if_meta_reports_a_different_placement_aggregate(dirs):
    meta, first = _stop_at_first_image(dirs)
    # Meta's aggregate on a campaign that has no ads yet: the settings are identical to the builder.
    meta.mutate_on_read[("campaign", "advantage_state_info")] = {"advantage_placement_state": "DISABLED"}
    d = _draft(dirs / "b", ads=3)                           # renamed / extra creatives do not matter
    res = _publisher(meta).publish_draft(d)
    assert res["campaign_id"] == first["campaign_id"] and res["reused"].get("campaign")
    assert not res["published_as"] and _names(meta) == [BASE]


def test_campaign_with_ads_and_a_wrong_placement_aggregate_is_still_not_reused(dirs):
    meta = FakeMeta()
    meta.mutate_on_read[("campaign", "advantage_state_info")] = {"advantage_placement_state": "ENABLED"}
    drafts = [_draft(dirs / x, ads=1) for x in "ab"]
    for d in drafts:
        for adset in d.ad_sets:
            adset.placements = {"mode": "advantage_plus"}
    first = _publisher(meta).publish_draft(drafts[0])
    assert first["error"] is None
    meta.mutate_on_read[("campaign", "advantage_state_info")] = {"advantage_placement_state": "DISABLED"}
    res = _publisher(meta).publish_draft(drafts[1])
    assert res["campaign_id"] != first["campaign_id"]       # the ACL-1772 policy is unchanged when ads exist


# ── B2: the name stays taken in every branch of the receipt check ──

def _fail_reads_of(meta, monkeypatch, campaign_id, times, body=None):
    """The read by id of one campaign fails `times` times (None = always), then Meta answers."""
    real = meta.request
    left = {"n": times}
    seen = {"reads": 0}
    body = body or {"code": 2, "message": "Service temporarily unavailable"}

    def request(method, path, params=None):
        if method == "GET" and path.strip("/").split("/")[-1] == campaign_id:
            seen["reads"] += 1
            if left["n"] is None or left["n"] > 0:
                if left["n"] is not None:
                    left["n"] -= 1
                return {"status": 500, "body": {"error": body}}
        return real(method, path, params)
    monkeypatch.setattr(meta, "request", request)
    return seen


def test_unreadable_receipt_campaign_is_retried_then_continues_as_a_new_copy(dirs, monkeypatch):
    """Owner decision (ACL-2583): no stop and no "publish again" text; a new operation under a distinct copy name."""
    meta, first = _stop_at_first_image(dirs)
    seen = _fail_reads_of(meta, monkeypatch, first["campaign_id"], None)
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])
    sleeps = []
    pub = _publisher(meta)
    pub.sleep = sleeps.append
    res = pub.publish_draft(_draft(dirs / "b", ads=2))
    assert res["error"] is None, res["error"]
    assert res["published_as"] == BASE + " copy 1" and res["campaign_id"] != first["campaign_id"]
    assert sorted(_names(meta)) == [BASE, BASE + " copy 1"], "the earlier name is never reused"
    assert seen["reads"] >= len(cr.TRANSIENT_READ_WAITS_S) + 1
    assert 25 <= sum(sleeps) <= 35, sleeps                       # about 30 s in total, through the injected sleep
    assert "few minutes" not in str(res)


def test_receipt_campaign_read_that_recovers_is_resumed(dirs, monkeypatch):
    meta, first = _stop_at_first_image(dirs)
    _fail_reads_of(meta, monkeypatch, first["campaign_id"], 2)       # two failures, the third try answers
    d = _draft(dirs / "b", ads=3)                           # replaced creative file and one more ad (as above)
    d.ad_sets[0].ads[0].creative_path = str(dirs / "b" / "replaced.png")
    (dirs / "b" / "replaced.png").write_bytes(b"another picture")
    res = _publisher(meta).publish_draft(d)
    assert res["error"] is None, res["error"]
    assert res["campaign_id"] == first["campaign_id"] and res["reused"].get("campaign")
    assert _names(meta) == [BASE]


def test_rate_limit_on_the_receipt_read_stays_a_stop(dirs, monkeypatch):
    meta, first = _stop_at_first_image(dirs)
    before = len(meta.posts)
    _fail_reads_of(meta, monkeypatch, first["campaign_id"], None, {"code": 17, "message": "request limit reached"})
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])
    res = _publisher(meta).publish_draft(_draft(dirs / "b", ads=2))
    assert res["error"] and len(meta.posts) == before
    assert _names(meta) == [BASE]


def test_deleted_receipt_campaign_does_not_block_the_name(dirs):
    meta, first = _stop_at_first_image(dirs)
    meta.objects.pop(first["campaign_id"])                  # Meta: "Object with ID ... does not exist"
    res = _publisher(meta).publish_draft(_draft(dirs / "b", ads=2))
    assert res["error"] is None and res["campaign_id"] != first["campaign_id"]


def test_live_campaign_with_the_exact_name_but_no_label_holds_the_name(dirs, monkeypatch):
    meta, first = _stop_at_first_image(dirs)
    meta.objects[first["campaign_id"]]["adlabels"] = []     # Meta did not keep the tag
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])      # search lag
    res = _publisher(meta).publish_draft(_draft(dirs / "b", ads=2))
    assert res["error"] is None and res["published_as"] == BASE + " copy 1"
    assert sorted(_names(meta)) == [BASE, BASE + " copy 1"]


def test_receipt_campaign_state_is_read_once_when_it_is_the_family_top(dirs, monkeypatch):
    meta, first = _stop_at_first_image(dirs)
    calls = []
    real = cr.CampaignCreator._resume_state
    monkeypatch.setattr(cr.CampaignCreator, "_resume_state",
                        lambda self, *a, **k: calls.append(a[2]) or real(self, *a, **k))
    d = _draft(dirs / "b", ads=2)
    d.ad_sets[0].daily_budget = 77.0                        # not reusable
    _publisher(meta).publish_draft(d)
    assert calls.count(first["campaign_id"]) == 1
