"""ACL-2284: a found creative that differs from the request ONLY in media is stale, not an error."""
import copy

import pytest

from src.fb_api import creator as cr
from src.fb_api import creative_recovery as recovery
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run, g

#: Fields that exist on the AdCreative node (what creative_recovery.FIELDS may ask for). Graph answers (#100) otherwise.
CREATIVE_FIELDS = {"id", "name", "account_id", "adlabels", "object_story_spec", "asset_feed_spec",
                   "degrees_of_freedom_spec", "contextual_multi_ads", "instagram_user_id"}


def _top_level(fields):
    out, depth, cur = [], 0, ""
    for ch in fields:
        depth += ch == "{"
        depth -= ch == "}"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return {f.split("{")[0].strip() for f in out if f.strip()}


class StrictMeta(FakeMeta):
    """The fake rejects unknown Graph fields on creative reads, like the real node does."""

    def _get(self, path, params):
        parts = path.strip("/").split("/")
        is_creative = (len(parts) == 2 and parts[0] == ACT and parts[1] == "adcreatives") or (
            len(parts) == 1 and parts[0] in self.objects and self.objects[parts[0]]["_kind"] == "creative")
        if is_creative and params.get("fields") is not None:
            unknown = _top_level(params["fields"]) - CREATIVE_FIELDS
            if unknown:
                return {"error": {"code": 100, "type": "OAuthException",
                                  "message": f"(#100) Tried accessing nonexisting field ({sorted(unknown)})"}}
        return super()._get(path, params)


def creatives(meta):
    return [o for o in meta.objects.values() if o["_kind"] == "creative"]


def adcreative_posts(meta):
    return [p for p, _ in meta.posts if p.endswith("/adcreatives")]


def _images(monkeypatch, first="HASHMAIN", second="HASHVERT"):
    """Feed-shaped image creatives with the two labelled files (main and Stories/Reels)."""
    original = cr.CampaignCreator.creative_params

    def with_feed(self, *a, **k):
        params = original(self, *a, **k)
        params["asset_feed_spec"] = {"images": [{"hash": first, "adlabels": [{"name": "main"}]},
                                                {"hash": second, "adlabels": [{"name": "vert"}]}],
                                     "bodies": [{"text": "b"}]}
        return params
    monkeypatch.setattr(cr.CampaignCreator, "creative_params", with_feed)
    monkeypatch.setattr("tests.test_acl1761_api_create.wp.check", lambda *a, **k: None)


def _videos(monkeypatch, meta, lengths):
    original = cr.CampaignCreator.creative_params

    def with_feed(self, *a, **k):
        params = original(self, *a, **k)
        params["asset_feed_spec"] = {"videos": [{"video_id": "10"}], "bodies": [{"text": "b"}]}
        return params
    monkeypatch.setattr(cr.CampaignCreator, "creative_params", with_feed)
    monkeypatch.setattr("tests.test_acl1761_api_create.wp.check", lambda *a, **k: None)
    base = meta._get

    def get(path, params):
        if params.get("fields") == "length" and path.strip("/") in lengths:
            return {"id": path.strip("/"), "length": lengths[path.strip("/")]}
        return base(path, params)
    meta._get = get


def _earlier(tmp_path, meta):
    """The creative is confirmed in the receipt, the ad is not: the repeat finds it by receipt."""
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error and len(creatives(meta)) == 1
    return draft, creatives(meta)[0]


class StaleOnWriteMeta(StrictMeta):
    """Doc122: Meta answers the first creative write with an EARLIER creative (earlier tag) with another image."""

    def write(self, path, params, **kw):
        if path.endswith("/adcreatives") and not getattr(self, "_seeded", False):
            self._seeded = True
            stale = copy.deepcopy(params)
            stale["asset_feed_spec"]["images"][0]["hash"] = "OLDHASH"
            stale["adlabels"] = [{"name": "acl-eb7048ed0b98"}]
            oid = self._store("creative", stale)
            self.posts.append((path, dict(params)))
            return {"status": 200, "body": {"id": oid}}
        return super().write(path, params, **kw)


def test_written_creative_answered_with_an_earlier_one_with_other_image_gets_a_new_creative(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StaleOnWriteMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert not out.error, out.error
    assert cr.STALE_WARNING in out.warnings
    assert len(creatives(meta)) == 2 and len(adcreative_posts(meta)) == 2
    new = [c for c in creatives(meta) if c["asset_feed_spec"]["images"][0]["hash"] == "HASHMAIN"]
    assert len(new) == 1 and out.creative_ids == [new[0]["id"]] and len(out.ad_ids) == 1
    stale_id = next(c["id"] for c in creatives(meta) if c is not new[0])
    assert stale_id not in out.creative_ids and stale_id not in out.created.get("creatives", [])


def test_receipt_found_creative_with_other_image_gets_a_new_creative(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    old["asset_feed_spec"]["images"][0]["hash"] = "OLDHASH"
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert cr.STALE_WARNING in second.warnings
    assert len(creatives(meta)) == 2 and len(second.ad_ids) == 1
    assert old["id"] not in second.creative_ids


def test_a_matching_creative_elsewhere_in_the_list_is_preferred_over_writing(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    twin = copy.deepcopy(old)
    twin["id"] = str(int(old["id"]) + 1000)
    meta.objects[twin["id"]] = twin
    old["asset_feed_spec"]["images"][0]["hash"] = "OLDHASH"       # the receipt points at the stale one
    before = len(adcreative_posts(meta))
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert second.creative_ids == [twin["id"]] and len(creatives(meta)) == 2
    assert len(adcreative_posts(meta)) == before                    # no creative was written
    assert cr.STALE_WARNING_REUSED in second.warnings and cr.STALE_WARNING not in second.warnings


def test_only_the_order_of_the_two_labelled_files_changed_is_accepted(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    old["asset_feed_spec"]["images"].reverse()
    before = len(adcreative_posts(meta))
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert len(creatives(meta)) == 1 and second.creative_ids == [old["id"]]
    assert len(adcreative_posts(meta)) == before
    assert cr.STALE_WARNING not in second.warnings


def test_swapped_labels_on_the_same_hashes_is_not_the_same_set(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    a, b = old["asset_feed_spec"]["images"]
    a["adlabels"], b["adlabels"] = b["adlabels"], a["adlabels"]
    second, _ = _run(meta, draft)
    assert not second.error
    assert cr.STALE_WARNING in second.warnings and len(creatives(meta)) == 2


def test_identical_creative_is_still_reused(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    before = len(adcreative_posts(meta))
    second, _ = _run(meta, draft)
    assert not second.error and second.creative_ids == [old["id"]]
    assert len(adcreative_posts(meta)) == before
    assert cr.STALE_WARNING not in second.warnings


@pytest.mark.parametrize("field,value", [
    ("account_id", "999"),
    ("object_story_spec.page_id", "999"),
    ("object_story_spec.link_data.message", "wrong"),
    ("object_story_spec.link_data.call_to_action.type", "SIGN_UP"),
    ("object_story_spec.link_data.link", "https://other.invalid/x")])
def test_other_page_account_text_cta_or_link_still_stops_even_with_other_media(tmp_path, monkeypatch, field, value):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    old["asset_feed_spec"]["images"][0]["hash"] = "OLDHASH"
    meta.mutate_on_read[("creative", field)] = value
    posts = list(meta.posts)
    second, _ = _run(meta, draft)
    assert second.stop_kind == "creative_unverified" and not second.ad_ids
    assert cr.STALE_WARNING not in second.warnings and meta.posts == posts


def test_text_difference_in_the_feed_with_other_media_stops(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = StrictMeta()
    draft, old = _earlier(tmp_path, meta)
    old["asset_feed_spec"]["images"][0]["hash"] = "OLDHASH"
    old["asset_feed_spec"]["bodies"] = [{"text": "other text"}]
    second, _ = _run(meta, draft)
    assert second.stop_kind == "creative_unverified" and not second.ad_ids


@pytest.mark.parametrize("lengths", [{"10": 12.5, "20": 30.0}, {"10": 12.5, "20": 12.5}, {}])
def test_receipt_found_creative_with_earlier_tag_and_other_video_gets_a_new_creative(tmp_path, monkeypatch, lengths):
    meta = StrictMeta()
    _videos(monkeypatch, meta, lengths)
    draft, old = _earlier(tmp_path, meta)
    old["asset_feed_spec"]["videos"][0]["video_id"] = "20"
    old["adlabels"] = [{"name": "acl-eb7048ed0b98"}]               # the tag of an earlier run
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert cr.STALE_WARNING in second.warnings
    assert len(creatives(meta)) == 2 and len(second.ad_ids) == 1 and old["id"] not in second.creative_ids


def test_receipt_found_creative_with_earlier_tag_and_other_text_still_stops(tmp_path, monkeypatch):
    meta = StrictMeta()
    _videos(monkeypatch, meta, {"10": 12.5, "20": 30.0})
    draft, old = _earlier(tmp_path, meta)
    old["asset_feed_spec"]["videos"][0]["video_id"] = "20"
    old["asset_feed_spec"]["bodies"] = [{"text": "other text"}]
    old["adlabels"] = [{"name": "acl-eb7048ed0b98"}]
    second, _ = _run(meta, draft)
    assert second.stop_kind == "creative_unverified" and not second.ad_ids
    assert len(creatives(meta)) == 1


class AlwaysStaleMeta(StaleOnWriteMeta):
    """Meta keeps answering creative writes with an earlier creative carrying other media."""

    def write(self, path, params, **kw):
        self._seeded = False
        return super().write(path, params, **kw)


def test_a_second_stale_answer_stops_instead_of_looping(tmp_path, monkeypatch):
    _images(monkeypatch)
    meta = AlwaysStaleMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "creative_unverified" and not out.ad_ids
    assert len(adcreative_posts(meta)) == 2        # the first write and exactly one replacement


def test_fake_rejects_unknown_creative_fields():
    meta = StrictMeta()
    meta.objects["5"] = {"id": "5", "_kind": "creative"}
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=10)
    with pytest.raises(Exception):
        client.get("/5", {"fields": "id,no_such_field"})
    assert client.get("/5", {"fields": recovery.FIELDS})["id"] == "5"


P = {"object_story_spec": {"page_id": "1"},
     "asset_feed_spec": {"images": [{"hash": "A", "adlabels": [{"name": "main"}]},
                                    {"hash": "B", "adlabels": [{"name": "vert"}]}], "bodies": [{"text": "t"}]}}


def _row(**feed):
    row = {"id": "5", "account_id": ACT.removeprefix("act_"), "object_story_spec": {"page_id": "1"},
           "asset_feed_spec": copy.deepcopy(P["asset_feed_spec"])}
    row["asset_feed_spec"].update(feed)
    return row


def test_media_diff_classes():
    assert recovery.media_diff(ACT, P, _row()) == "same"
    assert recovery.media_diff(ACT, P, _row(images=list(reversed(P["asset_feed_spec"]["images"])))) == "order"
    assert recovery.media_diff(ACT, P, _row(images=[{"hash": "X", "adlabels": [{"name": "main"}]},
                                                    {"hash": "B", "adlabels": [{"name": "vert"}]}])) == "stale"
    assert recovery.media_diff(ACT, P, _row(bodies=[{"text": "u"}])) == "other"
    assert recovery.media_diff(ACT, P, _row(images=[{"hash": "X"}], bodies=[{"text": "u"}])) == "other"
    assert recovery.media_diff("act_999", P, _row()) == "other"
    assert recovery.media_diff(ACT, P, None) == "other"
    unreadable = _row()
    unreadable.pop("asset_feed_spec")
    assert recovery.media_diff(ACT, P, unreadable) == "other"
    page = _row()
    page["object_story_spec"]["page_id"] = "2"
    page["asset_feed_spec"]["images"][0]["hash"] = "X"
    assert recovery.media_diff(ACT, P, page) == "other"
