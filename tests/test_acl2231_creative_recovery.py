"""ACL-2231: persistent missing metadata must not duplicate a confirmed creative."""
import copy
import json
from pathlib import Path

import pytest

from src.fb_api import creator as cr
from src.fb_api import creative_recovery as recovery
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run


class UntaggedMeta(FakeMeta):
    def __init__(self, **kw):
        super().__init__(drop_labels=("creative",), **kw)

    def _store(self, kind, params):
        oid = super()._store(kind, params)
        if kind == "creative":
            self.objects[oid]["account_id"] = ACT.removeprefix("act_")
        return oid


def creatives(meta):
    return [o for o in meta.objects.values() if o["_kind"] == "creative"]


def test_missing_tag_finishes_with_positive_content_and_retry_never_creates_again(tmp_path):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    first, _ = _run(meta, draft)
    assert not first.error and first.readback["ok"], first.error or first.mismatches
    assert len(creatives(meta)) == 1 and len(first.ad_ids) == 1
    assert first.creative_ids == [creatives(meta)[0]["id"]]
    posts = copy.deepcopy(meta.posts)
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"], second.error or second.mismatches
    assert meta.posts == posts


def test_unlabelled_orphan_is_reused_after_restart_from_verified_receipt(tmp_path):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error and len(creatives(meta)) == 1
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"], second.error or second.mismatches
    assert len(creatives(meta)) == 1 and len(second.ad_ids) == 1


def test_legacy_orphan_with_old_tag_is_matched_by_ad_identity_and_full_content(tmp_path, monkeypatch):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error and len(creatives(meta)) == 1
    creative = creatives(meta)[0]
    creative["name"] = creative["name"].replace(cr.plan_from_draft(draft).label, "acl-aaaaaaaaaaaa") + " 2026-10-26-" + "a"*32
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "fresh-machine"))
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"], second.error or second.mismatches
    assert len(creatives(meta)) == 1


@pytest.mark.parametrize("field,value", [("account_id", "999"), ("object_story_spec.page_id", "999"),
                                       ("object_story_spec.link_data.message", "wrong")])
def test_created_id_with_wrong_content_never_reaches_an_ad(tmp_path, field, value):
    meta = UntaggedMeta()
    meta.mutate_on_read[("creative", field)] = value
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error and not out.ad_ids
    assert len(creatives(meta)) == 1
    assert out.creative_ids == [creatives(meta)[0]["id"]]


def test_unknown_creative_send_is_not_repeated_after_restart(tmp_path):
    meta = UntaggedMeta()
    meta.fail_next["adcreatives"] = "unsent"
    draft = _draft(tmp_path, ads=1)
    one, _ = _run(meta, draft, reconcile_wait_s=0)
    assert one.error
    before = len([p for p, _ in meta.posts if p.endswith("/adcreatives")])
    two, _ = _run(meta, draft, reconcile_wait_s=0)
    assert two.error and not two.ad_ids
    assert len([p for p, _ in meta.posts if p.endswith("/adcreatives")]) == before


def test_receipt_failure_before_dispatch_prevents_creative_post(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(recovery, "_atomic_write", fail)
    meta = UntaggedMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error and not any(p.endswith("/adcreatives") for p, _ in meta.posts)


def test_corrupt_receipt_stops_instead_of_becoming_absent(tmp_path):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    _run(meta, draft, reconcile_wait_s=0)
    for path in (Path(recovery.get_local_config_dir()) / "creative_receipts").glob("*.json"):
        path.write_text("{broken", encoding="utf-8")
    posts = list(meta.posts)
    out, _ = _run(meta, draft)
    assert out.error and meta.posts == posts


def test_incomplete_creative_lookup_never_allows_a_new_post(tmp_path):
    meta = UntaggedMeta()
    original = meta._get
    def get(path, params):
        row = original(path, params)
        if path.endswith("/adcreatives"):
            row["paging"] = {"next": "https://example.invalid/more"}
        return row
    meta._get = get
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error and not any(p.endswith("/adcreatives") for p, _ in meta.posts)


def test_recovery_identity_does_not_share_two_different_ad_indices():
    assert recovery.same_ad("Ad #1 [acl-aaaaaaaaaaaa] 2026-10-27-" + "b"*32, "Ad #1")
    assert not recovery.same_ad("Ad #2 [acl-aaaaaaaaaaaa]", "Ad #1")


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": {}}, {"data": [None]},
                                  {"data": [{"id": "123"}]}, {"data": [], "paging": []}])
def test_unreadable_lookup_cannot_authorize_a_creative_post(tmp_path, body):
    meta = UntaggedMeta()
    original = meta._get
    meta._get = lambda path, params: copy.deepcopy(body) if path.endswith("/adcreatives") else original(path, params)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "creative_unknown"
    assert not any(p.endswith("/adcreatives") for p, _ in meta.posts)


@pytest.mark.parametrize("field,value", [("account_id", "999"), ("object_story_spec.page_id", "999"),
                                       ("object_story_spec.link_data.message", "wrong"), ("id", "999")])
def test_retained_tag_never_overrides_receipt_content_verification(tmp_path, field, value):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error and len(creatives(meta)) == 1
    obj = creatives(meta)[0]
    obj["adlabels"] = [{"name": cr.plan_from_draft(draft).label}]
    meta.mutate_on_read[("creative", field)] = value
    posts = copy.deepcopy(meta.posts)
    second, _ = _run(meta, draft)
    assert second.stop_kind == "creative_unverified" and not second.ad_ids
    assert meta.posts == posts


def test_missing_tag_many_ads_finish_inside_real_create_budget(tmp_path):
    from tests.test_acl1768_ad_sets import _draft as sets_draft, _publisher_for
    from src.web import publish_api
    draft = sets_draft(tmp_path, sets=10, ads=3)
    budget, refusal = publish_api.create_call_budget(cr.plan_from_draft(draft))
    assert not refusal
    meta = UntaggedMeta()
    pub = _publisher_for(meta)
    result = pub.publish_draft(draft)
    assert result["success"], result["error"]
    assert pub._client.calls <= budget
    assert len(creatives(meta)) == 30


@pytest.mark.parametrize("paging", [{"next": []}, {"next": False}, {"next": 1}])
def test_malformed_next_is_not_a_completed_listing(tmp_path, paging):
    meta = UntaggedMeta()
    original = meta._get
    meta._get = lambda path, params: {"data": [], "paging": paging} if path.endswith("/adcreatives") else original(path, params)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "creative_unknown"
    assert not any(p.endswith("/adcreatives") for p, _ in meta.posts)


def test_same_ad_with_unreadable_content_does_not_prove_absence(tmp_path):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    ad = cr.plan_from_draft(draft).ad_sets[0].ads[0]
    original = meta._get
    meta._get = lambda path, params: {"data": [{"id": "987", "name": f"{ad.name} #{ad.index} [acl-aaaaaaaaaaaa]"}]} if path.endswith("/adcreatives") else original(path, params)
    out, _ = _run(meta, draft)
    assert out.stop_kind == "creative_unknown"
    assert not any(p.endswith("/adcreatives") for p, _ in meta.posts)


def test_anonymized_live_shape_retains_old_metadata_but_requires_same_content():
    fixture = json.loads((Path(__file__).parent / "fixtures/ads_manager/acl2231_retained_creative_label.json").read_text())
    act, expected, actual = fixture["act"], fixture["expected"], fixture["actual"]
    assert recovery.same_ad(actual["name"], "Sample #1")
    assert recovery.complete(expected, actual) and recovery.matches(act, expected, actual)
    actual["object_story_spec"]["link_data"]["call_to_action"]["value"]["lead_gen_form_id"] = "999"
    assert not recovery.matches(act, expected, actual)


def test_explicit_other_campaign_tag_is_not_recovered_as_our_orphan(tmp_path):
    meta = FakeMeta()
    draft = _draft(tmp_path, ads=1)
    first, _ = _run(meta, draft)
    assert not first.error
    # A material-identical creative positively owned by a different campaign
    # remains that campaign's object, even when the ad identity is identical.
    old = creatives(meta)[0]
    old["adlabels"] = [{"name": "acl-aaaaaaaaaaaa"}]
    campaign = meta.objects[first.campaign_id]
    campaign["daily_budget"] = "999"
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"]
    assert len(creatives(meta)) == 2
    assert old["id"] not in second.creative_ids


def test_known_receipt_does_not_override_explicit_other_campaign_ownership(tmp_path):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error and len(creatives(meta)) == 1
    creatives(meta)[0]["adlabels"] = [{"name": "acl-aaaaaaaaaaaa"}]
    posts = list(meta.posts)
    second, _ = _run(meta, draft)
    assert second.error and not second.ad_ids
    assert meta.posts == posts


class ExistingIdenticalMeta(FakeMeta):
    """ACL-2249: Meta answers the creative write with an existing creative that keeps an earlier run's tag."""

    def _store(self, kind, params):
        oid = super()._store(kind, params)
        if kind == "creative":
            self.objects[oid]["adlabels"] = [{"name": "acl-eb7048ed0b98"}]
        return oid


def test_rewrite_returning_identical_creative_with_earlier_tag_is_accepted_with_warning(tmp_path):
    meta = ExistingIdenticalMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert not out.error and out.readback["ok"], out.error or out.mismatches
    assert len(out.ad_ids) == 1 and out.creative_ids == [creatives(meta)[0]["id"]]
    assert any("existing identical creative (tag of an earlier run)" in w for w in out.warnings)
    # the shared creative keeps its own tag: the current one is never written onto it
    assert creatives(meta)[0]["adlabels"] == [{"name": "acl-eb7048ed0b98"}]
    assert not [p for p in meta.posts if "adlabels" in p[0]]


@pytest.mark.parametrize("field,value", [("account_id", "999"), ("object_story_spec.page_id", "999"),
                                       ("object_story_spec.link_data.message", "wrong")])
def test_rewrite_returning_old_tag_creative_with_other_account_or_content_stops(tmp_path, field, value):
    meta = ExistingIdenticalMeta()
    meta.mutate_on_read[("creative", field)] = value
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error and not out.ad_ids
    assert not any("existing identical creative" in w for w in out.warnings)


def test_old_tag_creative_with_different_content_is_never_adopted_by_name_or_tag(tmp_path):
    meta = FakeMeta()
    draft = _draft(tmp_path, ads=1)
    first, _ = _run(meta, draft)
    assert not first.error
    old = creatives(meta)[0]
    old["adlabels"] = [{"name": "acl-eb7048ed0b98"}]
    old["object_story_spec"]["link_data"]["message"] = "other text"
    campaign = meta.objects[first.campaign_id]
    campaign["daily_budget"] = "999"
    second, _ = _run(meta, draft)
    assert not second.error
    assert old["id"] not in second.creative_ids


def test_accepted_identical_creative_is_reported_as_reused_not_created(tmp_path):
    meta = ExistingIdenticalMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert not out.error
    cid = creatives(meta)[0]["id"]
    assert cid not in out.created.get("creatives", []) and cid in out.reused["creatives"]


def test_resume_after_accepting_identical_creative_and_ad_failure_continues(tmp_path):
    meta = ExistingIdenticalMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error and len(creatives(meta)) == 1
    assert any("existing identical creative" in w for w in first.warnings)
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"], second.error or second.mismatches
    assert len(creatives(meta)) == 1 and len(second.ad_ids) == 1
    assert any("existing identical creative" in w for w in second.warnings)


def test_resume_of_accepted_identical_creative_stops_when_content_changed_since(tmp_path):
    meta = ExistingIdenticalMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error
    creatives(meta)[0]["object_story_spec"]["link_data"]["message"] = "changed"
    second, _ = _run(meta, draft)
    assert second.error and not second.ad_ids


def test_receipt_of_same_ad_with_other_content_is_stale_not_an_error(tmp_path):
    path = tmp_path / "r.json"
    first = recovery.Receipt(path, ACT, "ad", "digest-1")
    first.save("123")
    again = recovery.Receipt(path, ACT, "ad", "digest-2")
    assert again.data is None and again.object_id == ""
    with pytest.raises(ValueError):
        recovery.Receipt(path, "act_999", "ad", "digest-1")


def test_video_pairs_only_when_video_ids_are_the_only_difference():
    params = {"object_story_spec": {"page_id": "1"},
              "asset_feed_spec": {"videos": [{"video_id": "10"}], "bodies": [{"text": "a"}]}}
    row = {"id": "5", "account_id": ACT.removeprefix("act_"), "object_story_spec": {"page_id": "1"},
           "asset_feed_spec": {"videos": [{"video_id": "20"}], "bodies": [{"text": "a"}]}}
    assert recovery.video_pairs(ACT, params, row) == [("10", "20")]
    row["asset_feed_spec"]["bodies"] = [{"text": "other"}]
    assert recovery.video_pairs(ACT, params, row) is None


class _Videos:
    def __init__(self, lengths):
        self.lengths = lengths

    #: Fields that exist on the AdVideo node; Graph answers (#100) for anything else (e.g. account_id).
    ALLOWED = {"id", "length", "title", "description", "created_time", "updated_time"}

    def get(self, path, params=None):
        asked = set(((params or {}).get("fields") or "").split(","))
        if not asked <= self.ALLOWED:
            raise AssertionError(f"(#100) Tried accessing nonexisting field {sorted(asked - self.ALLOWED)}")
        v = self.lengths[path.strip("/")]
        if isinstance(v, Exception):
            raise v
        return {"length": v, "account_id": ACT.removeprefix("act_")} if not isinstance(v, dict) else v


def _pair_case():
    params = {"object_story_spec": {"page_id": "1"},
              "asset_feed_spec": {"videos": [{"video_id": "10"}], "bodies": [{"text": "a"}]}}
    row = {"id": "5", "account_id": ACT.removeprefix("act_"), "object_story_spec": {"page_id": "1"},
           "asset_feed_spec": {"videos": [{"video_id": "20"}], "bodies": [{"text": "a"}]}}
    return params, row


def _same_videos(lengths, params, row):
    me = type("Me", (), {"client": _Videos(lengths)})()
    return cr.CampaignCreator._same_videos(me, type("P", (), {"act": ACT})(), params, row)


def test_video_pair_with_equal_length_is_accepted():
    params, row = _pair_case()
    assert _same_videos({"10": 12.5, "20": 12.5}, params, row) is True


def test_video_pair_with_other_length_is_refused():
    params, row = _pair_case()
    assert _same_videos({"10": 12.5, "20": 30.0}, params, row) is False


def test_video_pair_is_refused_when_other_content_differs():
    params, row = _pair_case()
    row["asset_feed_spec"]["bodies"] = [{"text": "other"}]
    assert _same_videos({"10": 12.5, "20": 12.5}, params, row) is False


def test_unreadable_video_is_accepted_but_other_changes_are_not():
    params, row = _pair_case()
    assert _same_videos({"10": 12.5, "20": RuntimeError("boom")}, params, row) is True
    assert _same_videos({"10": 12.5, "20": None}, params, row) is True
    row["asset_feed_spec"]["videos"].append({"video_id": "21"})
    assert _same_videos({"10": 12.5, "20": 12.5, "21": 1}, params, row) is False


def test_several_identical_creatives_use_the_oldest(tmp_path):
    meta = UntaggedMeta()
    draft = _draft(tmp_path, ads=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.error and len(creatives(meta)) == 1
    old = creatives(meta)[0]
    twin = copy.deepcopy(old)
    twin["id"] = str(int(old["id"]) + 1000)
    meta.objects[twin["id"]] = twin
    for path in (Path(recovery.get_local_config_dir()) / "creative_receipts").glob("*.json"):
        path.unlink()
    second, _ = _run(meta, draft)
    assert not second.error, second.error
    assert second.creative_ids == [old["id"]] and len(creatives(meta)) == 2


def test_old_unknown_send_receipt_stops_blocking_after_15_minutes(tmp_path):
    import os
    import time
    meta = UntaggedMeta()
    meta.fail_next["adcreatives"] = "unsent"
    draft = _draft(tmp_path, ads=1)
    one, _ = _run(meta, draft, reconcile_wait_s=0)
    assert one.error
    two, _ = _run(meta, draft, reconcile_wait_s=0)
    assert two.error and not two.ad_ids          # fresh receipt still blocks
    old = time.time() - cr.RECEIPT_UNKNOWN_S - 60
    for path in (Path(recovery.get_local_config_dir()) / "creative_receipts").glob("*.json"):
        os.utime(path, (old, old))
    three, _ = _run(meta, draft, reconcile_wait_s=0)
    assert not three.error and len(three.ad_ids) == 1


def test_video_reads_only_use_fields_that_exist_on_the_video_node():
    params, row = _pair_case()
    seen = []
    class Spy(_Videos):
        def get(self, path, params=None):
            seen.append(params["fields"])
            return super().get(path, params)
    me = type("Me", (), {"client": Spy({"10": 12.5, "20": 12.5})})()
    assert cr.CampaignCreator._same_videos(me, type("P", (), {"act": ACT})(), params, row) is True
    assert seen == ["length", "length"]
