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
