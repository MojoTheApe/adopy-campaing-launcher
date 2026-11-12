"""ACL-2528 / ACL-2584: several primary texts on an Instant Form (lead) ad, offline.

Several texts + ONE file on an Instant Form ad set. The creative is the Ads Manager shape captured by the owner
(2026-11-11, variant A2): an asset feed with `optimization_type: PLACEMENT`, two placement rules, every text on the
main body label (text 1 also on the Stories / Reels label) and the lead form id in the feed button
(`asset_feed_spec.call_to_actions[0].value.lead_gen_form_id`). The ad set is NOT Dynamic Creative, so it takes as many
ads as Meta allows. The ONE live paused test write is a separate step with the owner; nothing here calls Meta.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT  # noqa: E402
from tests.test_acl1774_texts_vertical import png  # noqa: E402
from tests.test_acl1775_instant_forms import FORM, FORM2, PAGE, FormMeta, _creator, _instant, _run  # noqa: E402

LINK = cr.INSTANT_FORM_LINK


def _draft(tmp_path, *, texts=2, vertical=False, video=False):
    d = _instant(tmp_path, forms=(FORM,), video=video)
    ad = d.ad_sets[0].ads[0]
    if not video:
        Path(ad.creative_path).write_bytes(png(1080, 1080, salt="main"))
    if vertical:
        v = tmp_path / "1000_story.png"
        v.write_bytes(png(1080, 1920, salt="vert"))
        ad.vertical_filename, ad.vertical_path = v.name, str(v)
    ad.text_variants = [{"name": f"T{i}", "body": f"Body variant {i}", "headline": f"Head variant {i}",
                         "description": f"Desc variant {i}"} for i in range(2, 2 + texts)]
    return d


def _of(meta, kind):
    return next(o for o in meta.objects.values() if o["_kind"] == kind)


# ───────────────────────── creator ─────────────────────────

def _labels(item):
    return [x["name"] for x in item.get("adlabels", [])]


def test_several_texts_on_a_form_ad_go_as_the_ads_manager_placement_feed(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _draft(tmp_path, texts=2))
    assert out.error == "" and out.readback["ok"], out.mismatches
    ad_set, cre = _of(meta, "adset"), _of(meta, "creative")
    # Variant A2: the ad set is NOT Dynamic Creative; lead form ad set otherwise unchanged.
    assert "is_dynamic_creative" not in ad_set and ad_set["status"] == cr.CHILD_STATUS
    assert ad_set["destination_type"] == cr.DESTINATION_ON_AD
    fs = cre["asset_feed_spec"]
    assert fs["optimization_type"] == "PLACEMENT"
    assert len(fs["bodies"]) == 3 and len(fs["titles"]) == 3 and len(fs["descriptions"]) == 3
    # Every text carries the main body label, only text 1 also carries the Stories / Reels label.
    assert [_labels(b) for b in fs["bodies"]] == [["acl_body_main", "acl_body_vertical"], ["acl_body_main"],
                                                  ["acl_body_main"]]
    assert _labels(fs["titles"][0]) == ["acl_title_main", "acl_title_vertical"]
    assert fs["link_urls"] == [{"website_url": LINK, "adlabels": [{"name": "acl_link_main"},
                                                                  {"name": "acl_link_vertical"}]}]
    assert fs["call_to_actions"] == [{"type": fs["call_to_action_types"][0],
                                      "value": {"link": LINK, "lead_gen_form_id": FORM}}]
    assert len(fs["images"]) == 1 and _labels(fs["images"][0]) == ["acl_image_main", "acl_image_vertical"]
    main, vertical = fs["asset_customization_rules"]
    assert main["body_label"] == {"name": "acl_body_main"} and vertical["body_label"] == {"name": "acl_body_vertical"}
    assert main["image_label"] == {"name": "acl_image_main"} and vertical["link_url_label"] == {"name": "acl_link_vertical"}
    assert "story" in vertical["customization_spec"]["facebook_positions"]
    assert not set(main["customization_spec"]["facebook_positions"]) & {"story", "facebook_reels"}
    # Page (+ Instagram identity) only next to the feed; no link_data / video_data.
    assert set(cre["object_story_spec"]) <= {"page_id", wp.INSTAGRAM_KEY}
    assert cre["object_story_spec"]["page_id"] == PAGE
    # Enhancements and multi-advertiser ads stay OFF, everything PAUSED.
    assert cre["contextual_multi_ads"] == {"enroll_status": "OPT_OUT"}
    feats = cre["degrees_of_freedom_spec"]["creative_features_spec"]
    assert feats and all(v == {"enroll_status": "OPT_OUT"} for v in feats.values())
    assert all(p["status"] == (cr.PAUSED if path.endswith("/campaigns") else cr.CHILD_STATUS)
               for path, p in meta.posts if path.endswith(("/campaigns", "/adsets", "/ads")))


def test_the_ad_set_takes_several_ads_each_with_its_own_texts(tmp_path):
    d = _instant(tmp_path, forms=(FORM, FORM, FORM))
    for n, ad in enumerate(d.ad_sets[0].ads):
        Path(ad.creative_path).write_bytes(png(1080, 1080, salt=f"m{n}"))
        ad.ad_body = f"Ad {n} body 1"
        ad.text_variants = [{"name": f"T{i}", "body": f"Ad {n} body {i}", "headline": f"Ad {n} head {i}",
                             "description": ""} for i in range(2, 2 + 1 + n % 2)]
    plan = cr.plan_from_draft(d)  # no "only one ad in such an ad set" refusal
    assert not plan.ad_sets[0].dynamic and len(plan.ad_sets[0].ads) == 3
    meta = FormMeta()
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert len(_objs(meta, "ad")) == 3 and len(_objs(meta, "creative")) == 3
    assert all("is_dynamic_creative" not in a for a in _objs(meta, "adset"))
    feeds = [c["asset_feed_spec"] for c in _objs(meta, "creative")]
    assert all(f["optimization_type"] == "PLACEMENT" for f in feeds)
    assert sorted(len(f["bodies"]) for f in feeds) == [2, 2, 3]


def _objs(meta, kind):
    return [o for o in meta.objects.values() if o["_kind"] == kind]


def test_advantage_plus_placements_use_a_catch_all_main_rule(tmp_path):
    d = _draft(tmp_path, texts=1)
    d.ad_sets[0].placements = {"mode": "advantage_plus"}
    meta = FormMeta()
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"], out.mismatches
    main, vertical = _of(meta, "creative")["asset_feed_spec"]["asset_customization_rules"]
    assert main["customization_spec"] == {} and "story" in vertical["customization_spec"]["instagram_positions"]


def test_manual_placements_without_stories_reels_refuse_several_texts_before_any_write(tmp_path):
    d = _draft(tmp_path, texts=1)
    d.ad_sets[0].placements = {"mode": "manual", "facebook_positions": ["feed"], "instagram_positions": []}
    meta = FormMeta()
    with pytest.raises(cr.PlanError, match="Feed and Stories / Reels"):
        cr.plan_from_draft(d)
    assert meta.posts == []


def test_a_video_form_ad_with_several_texts_goes_as_a_feed_too(tmp_path):
    meta = FormMeta()
    meta.next_id = 100000  # numeric-ID contract of asset-feed videos
    out, _ = _run(meta, _draft(tmp_path, texts=1, video=True))
    assert out.error == "" and out.readback["ok"], out.mismatches
    fs = _of(meta, "creative")["asset_feed_spec"]
    assert fs["ad_formats"] == ["SINGLE_VIDEO"] and len(fs["videos"]) == 1
    assert _labels(fs["videos"][0]) == ["acl_video_main", "acl_video_vertical"]
    assert fs["asset_customization_rules"][0]["video_label"] == {"name": "acl_video_main"}
    assert fs["call_to_actions"][0]["value"]["lead_gen_form_id"] == FORM


def test_one_text_form_ad_keeps_the_old_creative(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _instant(tmp_path, forms=(FORM,)))
    assert out.error == "" and out.readback["ok"], out.mismatches
    cre = _of(meta, "creative")
    assert "asset_feed_spec" not in cre
    assert cre["object_story_spec"]["link_data"]["call_to_action"]["value"] == {"lead_gen_form_id": FORM}
    assert "is_dynamic_creative" not in _of(meta, "adset")


def test_a_website_feed_is_unchanged(tmp_path):
    from tests.test_acl1761_api_create import FakeMeta, _run as site_run
    from tests.test_acl1774_texts_vertical import _ad_with
    meta = FakeMeta()
    out, _ = site_run(meta, _ad_with(tmp_path, vertical=None, texts=1))
    assert out.error == "" and out.readback["ok"], out.mismatches
    fs = next(o for o in meta.objects.values() if o["_kind"] == "creative")["asset_feed_spec"]
    assert "call_to_actions" not in fs and "optimization_type" not in fs
    assert fs["link_urls"] == [{"website_url": "https://example.invalid/l"}]
    assert next(o for o in meta.objects.values() if o["_kind"] == "adset")["is_dynamic_creative"] is True


def test_a_stories_reels_file_on_a_form_ad_is_still_refused_before_any_write(tmp_path):
    meta = FormMeta()
    for texts in (0, 2):
        d = _draft(tmp_path, texts=texts, vertical=True)
        with pytest.raises(cr.PlanError, match="Stories / Reels file works only with a Website ad set"):
            cr.plan_from_draft(d)
        try:
            _run(meta, d)
        except cr.PlanError:
            pass
    assert meta.posts == []


def test_a_website_dynamic_ad_must_still_be_alone_in_its_ad_set(tmp_path):
    from tests.test_acl1774_texts_vertical import _ad_with
    d = _ad_with(tmp_path, vertical=None, texts=1)
    extra = copy.deepcopy(d.ad_sets[0].ads[0])
    d.ad_sets[0].ads.append(extra)
    with pytest.raises(cr.PlanError, match="only one ad in such an ad set"):
        cr.plan_from_draft(d)


def test_the_check_plan_summary_describes_the_mode(tmp_path):
    creator, _client = _creator(FormMeta())
    plan = cr.plan_from_draft(_draft(tmp_path, texts=1))
    summary = creator.plan_summary(plan, [{}], {})
    mode = summary["ad_sets"][0]["creative_mode"]
    assert "Dynamic Creative" not in mode and "placement rules" in mode


@pytest.mark.parametrize("field,key,value", [
    ("lead_gen_form_id", "creative.asset_feed_spec.call_to_actions",
     [{"type": "SIGN_UP", "value": {"link": LINK, "lead_gen_form_id": FORM2}}]),
    ("link", "creative.asset_feed_spec.link_urls", [{"website_url": "https://example.invalid/l"}]),
    ("feed_call_to_action", "creative.asset_feed_spec.call_to_actions",
     [{"type": "GET_QUOTE", "value": {"link": LINK, "lead_gen_form_id": FORM}}]),
    ("lead_gen_form_id", "creative.asset_feed_spec.call_to_actions", None),
    ("optimization_type", "creative.asset_feed_spec.optimization_type", "DEGREES_OF_FREEDOM"),
    ("optimization_type", "creative.asset_feed_spec.optimization_type", None),
    ("primary_texts", "creative.asset_feed_spec.bodies", [{"text": "Body variant 2"}]),
    ("bodies_vertical_label", "creative.asset_feed_spec.bodies",
     [{"text": "Body variant 2", "adlabels": [{"name": "acl_body_main"}]},
      {"text": "Body variant 3", "adlabels": [{"name": "acl_body_main"}]}]),
    ("rule_main_labels", "creative.asset_feed_spec.asset_customization_rules", []),
    ("rule_main_placements", "creative.asset_feed_spec.asset_customization_rules",
     [{"customization_spec": {"publisher_platforms": ["facebook"]},
       "body_label": {"name": "acl_body_main"}, "title_label": {"name": "acl_title_main"},
       "description_label": {"name": "acl_description_main"}, "link_url_label": {"name": "acl_link_main"},
       "image_label": {"name": "acl_image_main"}}]),
], ids=["other_form", "site_link", "other_button", "no_button", "other_mode", "no_mode", "texts_as_set",
        "vertical_label_lost", "rules_gone", "main_rule_placements"])
def test_readback_catches_what_meta_returned_differently(tmp_path, field, key, value):
    meta = FormMeta()
    meta.mutate_ads[key] = value
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert field in {m["field"] for m in out.mismatches}, out.mismatches


def test_readback_reads_the_texts_as_a_set(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    cre = _of(meta, "creative")["asset_feed_spec"]
    reordered = list(reversed(cre["bodies"]))  # same texts and labels, other order
    meta2 = FormMeta()
    meta2.mutate_ads["creative.asset_feed_spec.bodies"] = reordered
    out2, _ = _run(meta2, _draft(tmp_path, texts=1))
    assert "primary_texts" not in {m["field"] for m in out2.mismatches}, out2.mismatches


def test_a_form_missing_from_the_page_is_a_readback_mismatch(tmp_path):
    meta = FormMeta()
    meta.drop_forms_after_creative = True
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert "form_on_page" in {c["field"] for c in out.readback["mismatches"]}


def test_form_link_constant_matches_the_creator():
    assert wp.LEAD_FEED_LINK == cr.INSTANT_FORM_LINK
    assert wp.INSTANT_VERTICAL_POSITIONS == {"facebook_positions": cr.VERTICAL_FACEBOOK,
                                             "instagram_positions": cr.VERTICAL_INSTAGRAM}


# ───────────────────────── policy: Python and page helper agree ─────────────────────────

def _lab(*names):
    return [{"name": n} for n in names]


def _feed(**over):
    """The A2 shape, written out by hand (not through the creator)."""
    spec = {
        "optimization_type": "PLACEMENT",
        "bodies": [{"text": "b1", "adlabels": _lab("acl_body_main", "acl_body_vertical")},
                   {"text": "b2", "adlabels": _lab("acl_body_main")}],
        "titles": [{"text": "t1", "adlabels": _lab("acl_title_main", "acl_title_vertical")}],
        "images": [{"hash": "a" * 32, "adlabels": _lab("acl_image_main", "acl_image_vertical")}],
        "link_urls": [{"website_url": LINK, "adlabels": _lab("acl_link_main", "acl_link_vertical")}],
        "call_to_action_types": ["SIGN_UP"], "ad_formats": ["SINGLE_IMAGE"],
        "call_to_actions": [{"type": "SIGN_UP", "value": {"link": LINK, "lead_gen_form_id": FORM}}],
        "asset_customization_rules": [_rule(0, MAIN_POS), _rule(1, VERT_POS)]}
    for k, v in over.items():
        if v is None:
            spec.pop(k, None)
        else:
            spec[k] = v
    return spec


MAIN_POS = {"publisher_platforms": ["facebook", "instagram"], "facebook_positions": ["feed"],
            "instagram_positions": ["stream"]}
VERT_POS = {"publisher_platforms": ["facebook", "instagram"], "facebook_positions": ["story", "facebook_reels"],
            "instagram_positions": ["story", "reels"]}


def _rule(n, positions, kinds=("body", "title", "link", "image"), **over):
    side = ("main", "vertical")[n]
    keys = {"body": "body_label", "title": "title_label", "link": "link_url_label", "image": "image_label",
            "description": "description_label", "video": "video_label"}
    rule = {"customization_spec": positions, **{keys[k]: {"name": f"acl_{k}_{side}"} for k in kinds}}
    rule.update(over)
    return rule


def _cre(spec, story=None, **top):
    return {"name": "x", "object_story_spec": story or {"page_id": PAGE}, "asset_feed_spec": spec, **top}


def _btn(**value):
    return [{"type": "SIGN_UP", "value": value}]


def _t(text="b", *labels):
    return {"text": text, "adlabels": _lab(*labels)}


#: (name, params, on_ad, allowed): one table for BOTH lines (Python and page helper).
MATRIX = [
    ("ok_full", _cre(_feed()), True, True),
    ("ok_without_link_in_button", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=FORM))), True, True),
    ("ok_five_texts", _cre(_feed(bodies=[_t(f"b{i}", "acl_body_main", *(["acl_body_vertical"] if i == 0 else []))
                                         for i in range(5)])), True, True),
    ("ok_no_body", _cre(_feed(bodies=None, asset_customization_rules=[
        _rule(0, MAIN_POS, ("title", "link", "image")), _rule(1, VERT_POS, ("title", "link", "image"))])), True, True),
    ("ok_descriptions", _cre(_feed(descriptions=[_t("d1", "acl_description_main", "acl_description_vertical")],
                                   asset_customization_rules=[
        _rule(0, MAIN_POS, ("body", "title", "description", "link", "image")),
        _rule(1, VERT_POS, ("body", "title", "description", "link", "image"))])), True, True),
    ("ok_video", _cre(_feed(images=None, videos=[{"video_id": "123456789", "thumbnail_url": "https://x.invalid/t.jpg",
                                                  "adlabels": _lab("acl_video_main", "acl_video_vertical")}],
                            ad_formats=["SINGLE_VIDEO"], asset_customization_rules=[
        _rule(0, MAIN_POS, ("body", "title", "link", "video")), _rule(1, VERT_POS, ("body", "title", "link", "video"))])),
     True, True),
    ("ok_instagram", _cre(_feed(), story={"page_id": PAGE, "instagram_user_id": "1784140000001"}), True, True),
    ("ok_catch_all_main_rule", _cre(_feed(asset_customization_rules=[_rule(0, {}), _rule(1, VERT_POS)])), True, True),
    # Not an on-ad set: the form (and its button key) is refused everywhere.
    ("no_on_ad", _cre(_feed()), False, False),
    # The mode and the shape are exact.
    ("no_optimization_type", _cre(_feed(optimization_type=None)), True, False),
    ("other_optimization_type", _cre(_feed(optimization_type="DEGREES_OF_FREEDOM")), True, False),
    ("no_title", _cre(_feed(titles=None, asset_customization_rules=[
        _rule(0, MAIN_POS, ("body", "link", "image")), _rule(1, VERT_POS, ("body", "link", "image"))])), True, False),
    # No headline (Ads Manager HAR): exactly one empty title with both labels, title_label kept in both rules.
    ("ok_empty_title", _cre(_feed(titles=[_t("", "acl_title_main", "acl_title_vertical")])), True, True),
    ("empty_title_without_vertical_label", _cre(_feed(titles=[_t("", "acl_title_main")])), True, False),
    ("empty_title_next_to_another", _cre(_feed(titles=[_t("t1", "acl_title_main", "acl_title_vertical"),
                                                       _t("", "acl_title_main")])), True, False),
    ("empty_title_first_of_two", _cre(_feed(titles=[_t("", "acl_title_main", "acl_title_vertical"),
                                                    _t("t2", "acl_title_main")])), True, False),
    ("whitespace_title", _cre(_feed(titles=[_t(" ", "acl_title_main", "acl_title_vertical")])), True, False),
    ("empty_title_with_extra_key", _cre(_feed(titles=[{"text": "", "x": 1, "adlabels": _lab(
        "acl_title_main", "acl_title_vertical")}])), True, False),
    ("empty_title_rule_loses_title_label", _cre(_feed(
        titles=[_t("", "acl_title_main", "acl_title_vertical")], asset_customization_rules=[
            _rule(0, MAIN_POS, ("body", "link", "image")), _rule(1, VERT_POS, ("body", "link", "image"))])), True, False),
    ("empty_body", _cre(_feed(bodies=[_t("", "acl_body_main", "acl_body_vertical")])), True, False),
    ("empty_second_body", _cre(_feed(bodies=[_t("b1", "acl_body_main", "acl_body_vertical"),
                                             _t("", "acl_body_main")])), True, False),
    ("empty_description", _cre(_feed(descriptions=[_t("", "acl_description_main", "acl_description_vertical")],
                                     asset_customization_rules=[
        _rule(0, MAIN_POS, ("body", "title", "description", "link", "image")),
        _rule(1, VERT_POS, ("body", "title", "description", "link", "image"))])), True, False),
    # A rule with platforms only has no positions: the two rules would overlap completely.
    ("vertical_rule_platforms_only", _cre(_feed(asset_customization_rules=[
        _rule(0, MAIN_POS), _rule(1, {"publisher_platforms": ["facebook", "instagram"]})])), True, False),
    ("main_rule_platforms_only", _cre(_feed(asset_customization_rules=[
        _rule(0, {"publisher_platforms": ["facebook", "instagram"]}), _rule(1, VERT_POS)])), True, False),
    ("unlabelled_text", _cre(_feed(bodies=[{"text": "b1"}, {"text": "b2"}])), True, False),
    ("second_text_with_vertical_label", _cre(_feed(bodies=[
        _t("b1", "acl_body_main", "acl_body_vertical"), _t("b2", "acl_body_main", "acl_body_vertical")])), True, False),
    ("first_text_without_vertical_label", _cre(_feed(bodies=[_t("b1", "acl_body_main"), _t("b2", "acl_body_main")])),
     True, False),
    ("foreign_label", _cre(_feed(bodies=[_t("b1", "acl_body_main", "evil_label"), _t("b2", "acl_body_main")])), True, False),
    ("six_texts", _cre(_feed(bodies=[_t(f"b{i}", "acl_body_main", *(["acl_body_vertical"] if i == 0 else []))
                                     for i in range(6)])), True, False),
    ("empty_text", _cre(_feed(bodies=[_t("  ", "acl_body_main", "acl_body_vertical")])), True, False),
    ("text_too_long", _cre(_feed(bodies=[_t("x" * 1025, "acl_body_main", "acl_body_vertical")])), True, False),
    ("two_files", _cre(_feed(images=[
        {"hash": "a" * 32, "adlabels": _lab("acl_image_main", "acl_image_vertical")},
        {"hash": "b" * 32, "adlabels": _lab("acl_image_main")}])), True, False),
    ("file_with_one_label", _cre(_feed(images=[{"hash": "a" * 32, "adlabels": _lab("acl_image_main")}])), True, False),
    ("file_crop_key", _cre(_feed(images=[{"hash": "a" * 32, "image_crops": {"90x160": [[0, 0], [1, 1]]},
                                          "adlabels": _lab("acl_image_main", "acl_image_vertical")}])), True, False),
    ("no_rules", _cre(_feed(asset_customization_rules=None)), True, False),
    ("one_rule", _cre(_feed(asset_customization_rules=[_rule(0, MAIN_POS)])), True, False),
    ("three_rules", _cre(_feed(asset_customization_rules=[_rule(0, MAIN_POS), _rule(1, VERT_POS),
                                                          _rule(1, VERT_POS)])), True, False),
    ("rules_swapped", _cre(_feed(asset_customization_rules=[_rule(1, VERT_POS), _rule(0, MAIN_POS)])), True, False),
    ("rule_missing_a_label_key", _cre(_feed(asset_customization_rules=[
        _rule(0, MAIN_POS, ("body", "title", "image")), _rule(1, VERT_POS)])), True, False),
    ("rule_extra_key", _cre(_feed(asset_customization_rules=[_rule(0, MAIN_POS, x=1), _rule(1, VERT_POS)])), True, False),
    ("main_rule_has_stories", _cre(_feed(asset_customization_rules=[_rule(0, VERT_POS), _rule(1, VERT_POS)])), True, False),
    ("vertical_rule_has_feed", _cre(_feed(asset_customization_rules=[_rule(0, MAIN_POS), _rule(1, MAIN_POS)])), True, False),
    ("vertical_rule_empty", _cre(_feed(asset_customization_rules=[_rule(0, MAIN_POS), _rule(1, {})])), True, False),
    ("rule_unknown_position", _cre(_feed(asset_customization_rules=[
        _rule(0, dict(MAIN_POS, facebook_positions=["marketplace"])), _rule(1, VERT_POS)])), True, False),
    ("extra_feed_key", _cre(_feed(lead_gen_configuration={"is_work_email_enforcement_enabled": False})), True, False),
    ("audios_key", _cre(_feed(audios=[{"type": "random"}])), True, False),
    # Value checks of the form id and the button.
    ("form_not_digits", _cre(_feed(call_to_actions=_btn(lead_gen_form_id="abc123"))), True, False),
    ("form_too_short", _cre(_feed(call_to_actions=_btn(lead_gen_form_id="123"))), True, False),
    ("form_number", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=7000001))), True, False),
    ("form_empty", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=""))), True, False),
    ("form_missing", _cre(_feed(call_to_actions=_btn(link=LINK))), True, False),
    ("no_button", _cre(_feed(call_to_actions=None)), True, False),
    ("value_extra_key", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=FORM, tos_accepted=True))), True, False),
    ("value_other_link", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=FORM, link="https://evil.invalid/"))), True, False),
    ("two_buttons", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=FORM) * 2)), True, False),
    ("button_type_differs", _cre(_feed(call_to_actions=[{"type": "APPLY_NOW", "value": {"lead_gen_form_id": FORM}}])), True, False),
    ("button_extra_key", _cre(_feed(call_to_actions=[{"type": "SIGN_UP", "value": {"lead_gen_form_id": FORM},
                                                      "x": 1}])), True, False),
    ("button_not_list", _cre(_feed(call_to_actions={"type": "SIGN_UP", "value": {"lead_gen_form_id": FORM}})), True, False),
    # The form must be a form-capable button and the placeholder link.
    ("cta_not_for_forms", _cre(_feed(call_to_action_types=["GET_OFFER"],
                                     call_to_actions=[{"type": "GET_OFFER", "value": {"lead_gen_form_id": FORM}}])), True, False),
    ("site_link_on_form", _cre(_feed(link_urls=[{"website_url": "https://example.invalid/l",
                                                 "adlabels": _lab("acl_link_main", "acl_link_vertical")}])), True, False),
    # The form id anywhere else stays refused, even next to a valid button.
    ("form_also_in_story", _cre(_feed(), story={"page_id": PAGE, "link_data": {
        "call_to_action": {"type": "SIGN_UP", "value": {"lead_gen_form_id": FORM}}}}), True, False),
    ("form_in_feed_top", _cre(_feed(lead_gen_form_id=FORM)), True, False),
    ("form_in_title_item", _cre(_feed(titles=[{"text": "t1", "lead_gen_form_id": FORM,
                                               "adlabels": _lab("acl_title_main", "acl_title_vertical")}])), True, False),
    ("form_in_second_button_value", _cre(_feed(call_to_actions=[
        {"type": "SIGN_UP", "value": {"lead_gen_form_id": FORM, "link": LINK, "x": {"lead_gen_form_id": FORM}}}])), True, False),
    ("form_top_level", {"name": "x", "lead_gen_form_id": FORM}, True, False),
]
MATRIX_IDS = [m[0] for m in MATRIX]


@pytest.mark.parametrize("name,params,on_ad,allowed", MATRIX, ids=MATRIX_IDS)
def test_policy_python_matrix(name, params, on_ad, allowed):
    path = f"/{ACT}/adcreatives"
    if allowed:
        assert wp.check(ACT, "POST", path, params, on_ad=on_ad) == "adcreatives"
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", path, params, on_ad=on_ad)


def test_policy_accepts_exactly_what_the_creator_builds(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _draft(tmp_path, texts=3, video=False))
    assert out.error == ""  # every POST went through wp.check inside the client


def test_the_old_dynamic_form_feed_is_refused():
    old = _cre({"bodies": [{"text": "b1"}, {"text": "b2"}], "titles": [{"text": "t1"}],
                "images": [{"hash": "a" * 32}], "link_urls": [{"website_url": LINK}],
                "call_to_action_types": ["SIGN_UP"], "ad_formats": ["SINGLE_IMAGE"],
                "call_to_actions": _btn(link=LINK, lead_gen_form_id=FORM)})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", old, on_ad=True)


def test_policy_does_not_mutate_the_callers_params():
    params = _cre(_feed())
    before = copy.deepcopy(params)
    wp.check(ACT, "POST", f"/{ACT}/adcreatives", params, on_ad=True)
    assert params == before


def test_the_feed_button_is_refused_on_other_edges():
    for edge in ("adsets", "ads", "campaigns"):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/{edge}", {"name": "x", "status": "PAUSED", "asset_feed_spec": _feed()},
                     on_ad=True)


def test_the_website_feed_schema_is_unchanged():
    web = _cre({"bodies": [{"text": "b1"}], "titles": [{"text": "t1"}], "images": [{"hash": "a" * 32}],
                "link_urls": [{"website_url": "https://example.invalid/l"}],
                "call_to_action_types": ["GET_OFFER"], "ad_formats": ["SINGLE_IMAGE"]})
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", web) == "adcreatives"
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", web, on_ad=False) == "adcreatives"
    # The Instant Form keys are refused on a Website feed.
    for key, value in (("optimization_type", "PLACEMENT"), ("call_to_actions", _btn(lead_gen_form_id=FORM))):
        bad = _cre(dict(web["asset_feed_spec"], **{key: value}))
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/adcreatives", bad)


def test_page_policy_carries_the_new_values():
    pol = wp.policy_for_page(ACT)
    assert pol["asset_feed"]["form_link"] == LINK and pol["lead_form"]["feed_key"] == "call_to_actions"
    inst = pol["asset_feed"]["instant"]
    assert inst["optimization"] == "PLACEMENT"
    assert inst["kinds"]["bodies"] == {"labels": ["acl_body_main", "acl_body_vertical"], "rule_key": "body_label"}
    assert inst["vertical_positions"]["instagram_positions"] == ["story", "reels"]


_NODE = r"""
const calls = [];
const fakeFetch = async (url, init) => { calls.push(String(url)); return { status: 200,
  text: async () => JSON.stringify({ id: '5' }), headers: { get: () => null } }; };
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams', HELPER)(
  G, loc, doc, { getEntriesByType: () => [] }, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
(async () => {
  const out = {};
  for (const m of MATRIX) {
    const spec = { method: 'POST', path: '/' + ACTV + '/' + (m.edge || 'adcreatives'), params: m.params };
    if (m.on_ad) spec.on_ad = true;
    out[m.name] = await h.call(spec);
  }
  out.calls = calls.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_matrix(rows=None) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_feed_form"
    matrix = [{"name": n, "params": p, "on_ad": o, **({"edge": e} if e else {})}
              for n, p, o, _a, e in (rows or [(*m, "") for m in MATRIX])]
    script = (_NODE.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20)).replace("NAME", json.dumps(name))
              .replace("ACTV", json.dumps(ACT)).replace("MATRIX", "(" + json.dumps(matrix) + ")")
              .replace("HELPER", json.dumps(tc.helper_source(name, wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([shutil.which("node"), path], capture_output=True, text=True, encoding="utf-8",
                              timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_page_helper_agrees_with_python_on_the_whole_matrix():
    out = _node_matrix()
    for name, _params, _on_ad, allowed in MATRIX:
        got = out[name]
        if allowed:
            assert got.get("body") == {"id": "5"}, (name, got)
        else:
            assert got.get("error") == "write_refused", (name, got)
    assert out["calls"] == sum(1 for m in MATRIX if m[3]), "wrong number of writes reached Meta"


_SET = {"name": "s", "campaign_id": "1", "status": "PAUSED", "destination_type": "ON_AD"}
#: (name, params, allowed): an Instant Form (ON_AD) ad set is never Dynamic Creative (ACL-2584 review M4).
ADSET_ROWS = [
    ("on_ad_plain", dict(_SET), True),
    ("on_ad_flag_false", dict(_SET, is_dynamic_creative=False), False),
    ("on_ad_dynamic", dict(_SET, is_dynamic_creative=True), False),
    ("website_dynamic_stays_allowed", {**_SET, "destination_type": "WEBSITE", "is_dynamic_creative": True}, True),
    ("no_destination_dynamic_stays_allowed",
     {k: v for k, v in _SET.items() if k != "destination_type"} | {"is_dynamic_creative": True}, True),
]


@pytest.mark.parametrize("name,params,allowed", ADSET_ROWS, ids=[r[0] for r in ADSET_ROWS])
def test_an_instant_form_ad_set_may_not_be_dynamic_creative_python(name, params, allowed):
    if allowed:
        assert wp.check(ACT, "POST", f"/{ACT}/adsets", params) == "adsets"
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/adsets", params)


def test_an_instant_form_ad_set_may_not_be_dynamic_creative_page_helper():
    out = _node_matrix([(n, p, False, a, "adsets") for n, p, a in ADSET_ROWS])
    for name, _p, allowed in ADSET_ROWS:
        assert (out[name].get("body") == {"id": "5"}) == allowed, (name, out[name])


# ───────────────────────── no headline (Ads Manager HAR) ─────────────────────────

def _no_headline_draft(tmp_path):
    d = _draft(tmp_path, texts=1)
    ad = d.ad_sets[0].ads[0]
    ad.ad_title = ""
    ad.text_variants = [{"name": "T2", "body": "Body variant 2", "headline": "", "description": ""}]
    return d


def test_an_ad_without_a_headline_sends_one_empty_title_as_ads_manager_did(tmp_path):
    meta = FormMeta()
    d = _no_headline_draft(tmp_path)
    assert cr.plan_from_draft(d).ad_sets[0].ads[0].titles == []
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"], (out.error, out.mismatches)
    fs = _of(meta, "creative")["asset_feed_spec"]
    assert fs["titles"] == [{"text": "", "adlabels": _lab("acl_title_main", "acl_title_vertical")}]
    assert all("title_label" in r for r in fs["asset_customization_rules"])  # kept in both rules
    assert "is_dynamic_creative" not in _of(meta, "adset")
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", _cre(fs), on_ad=True) == "adcreatives"


def test_readback_still_catches_a_headline_that_came_back_different(tmp_path):
    meta = FormMeta()
    meta.mutate_ads["creative.asset_feed_spec.titles"] = [{"text": "Meta wrote this"}]
    out, _ = _run(meta, _no_headline_draft(tmp_path))
    assert "headlines" in {m["field"] for m in out.mismatches}, out.mismatches


# ───────────────────────── readback hardening (ACL-2584 review M2, M3) ─────────────────────────

def test_readback_accepts_the_adlabel_spelling_of_the_rule_label_keys(tmp_path):
    first = FormMeta()
    _run(first, _draft(tmp_path, texts=1))
    rules = _of(first, "creative")["asset_feed_spec"]["asset_customization_rules"]
    renamed = [{(k[:-len("_label")] + "_adlabel" if k.endswith("_label") else k): v for k, v in r.items()}
               for r in rules]
    assert renamed != rules
    meta = FormMeta()
    meta.mutate_ads["creative.asset_feed_spec.asset_customization_rules"] = renamed
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert not [m for m in out.mismatches if str(m["field"]).startswith("rule_")], out.mismatches


def test_readback_flags_a_wrong_label_in_the_adlabel_spelling(tmp_path):
    first = FormMeta()
    _run(first, _draft(tmp_path, texts=1))
    rules = _of(first, "creative")["asset_feed_spec"]["asset_customization_rules"]
    bad = [{(k[:-len("_label")] + "_adlabel" if k.endswith("_label") else k):
            ({"name": "acl_body_other"} if k == "body_label" else v) for k, v in r.items()} for r in rules]
    meta = FormMeta()
    meta.mutate_ads["creative.asset_feed_spec.asset_customization_rules"] = bad
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert "rule_main_labels" in {m["field"] for m in out.mismatches}


def test_one_image_that_meta_stores_as_two_rows_is_not_a_mismatch(tmp_path):
    first = FormMeta()
    _run(first, _draft(tmp_path, texts=1))
    row = _of(first, "creative")["asset_feed_spec"]["images"][0]
    split = [{"hash": row["hash"], "adlabels": _lab("acl_image_main")},
             {"hash": row["hash"], "image_crops": {"90x160": [[0, 0], [1, 1]]},
              "adlabels": _lab("acl_image_vertical")}]
    meta = FormMeta()
    meta.mutate_ads["creative.asset_feed_spec.images"] = split
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert not {"file_count", "media_labels"} & {m["field"] for m in out.mismatches}, out.mismatches


def test_two_different_images_are_still_a_file_count_mismatch(tmp_path):
    first = FormMeta()
    _run(first, _draft(tmp_path, texts=1))
    row = _of(first, "creative")["asset_feed_spec"]["images"][0]
    meta = FormMeta()
    meta.mutate_ads["creative.asset_feed_spec.images"] = [
        {"hash": row["hash"], "adlabels": _lab("acl_image_main")},
        {"hash": "f" * 32, "adlabels": _lab("acl_image_vertical")}]
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert "file_count" in {m["field"] for m in out.mismatches}


# ───────────────────────── Check plan ─────────────────────────

def _form_inp(dest):
    return {"destination": dest, "objective": "leads", "link": "https://example.invalid/l", "country": "US",
            "ads": [{"file": "a.png", "form_id": FORM, "bodies": ["a", "b"], "headlines": []}]}


def test_no_headline_is_a_warning_for_instant_form_and_a_block_for_website():
    form = {v["rule"]: v for v in pf.evaluate_client(_form_inp("instant_form"))}["feed_texts"]
    assert form["status"] == "warning", form
    web = {v["rule"]: v for v in pf.evaluate_client(_form_inp("website"))}["feed_texts"]
    assert web["status"] == "error", web


def test_the_feed_texts_wording_does_not_claim_meta_requires_a_headline():
    rule = next(r for r in pf.load_json()["rules"] if r["id"] == "feed_texts")
    assert "Meta requires" not in rule["hint"] and "Meta requires" not in rule["label"]

def test_check_plan_rules_let_several_texts_pass_for_a_form_ad_set():
    inp = {"destination": "instant_form", "objective": "leads", "link": "https://example.invalid/l", "country": "US",
           "ads": [{"file": "a.png", "form_id": FORM, "bodies": ["a", "b"], "headlines": ["h1", "h2"]}]}
    rules = {v["rule"]: v for v in pf.evaluate_client(inp)}
    for rule in ("texts_vertical", "texts_dynamic", "texts_limit", "feed_texts"):
        assert rules[rule]["status"] == "ok", rules[rule]


# ───────────── owner decision 2026-11-12: an empty Primary text 1 never blocks ─────────────

def _texts_draft(tmp_path, *, body1, variants):
    d = _instant(tmp_path, forms=(FORM,))
    ad = d.ad_sets[0].ads[0]
    Path(ad.creative_path).write_bytes(png(1080, 1080, salt="main"))
    ad.ad_body = body1
    ad.text_variants = [{"name": f"T{i}", "body": b, "headline": h, "description": ""}
                        for i, (b, h) in enumerate(variants, 2)]
    return d


@pytest.mark.parametrize("title_only", [False, True])
def test_empty_or_title_only_text_1_publishes_with_texts_2_and_3_in_order(tmp_path, title_only):
    d = _texts_draft(tmp_path, body1="", variants=[("Body two", "Head two"), ("Body three", "Head three")])
    d.ad_sets[0].ads[0].ad_title = "Head" if title_only else ""
    plan = cr.plan_from_draft(d)  # no "choose Primary text 1 first" refusal
    assert plan.ad_sets[0].ads[0].bodies == ["Body two", "Body three"]
    meta = FormMeta()
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"], out.mismatches
    fs = _of(meta, "creative")["asset_feed_spec"]
    assert [b["text"] for b in fs["bodies"]] == ["Body two", "Body three"]
    assert "is_dynamic_creative" not in _of(meta, "adset")
    assert [_labels(b) for b in fs["bodies"]] == [["acl_body_main", "acl_body_vertical"], ["acl_body_main"]]


def test_no_body_at_all_publishes_without_a_primary_text(tmp_path):
    d = _texts_draft(tmp_path, body1="", variants=[])
    d.ad_sets[0].ads[0].ad_title = "Head"
    plan = cr.plan_from_draft(d)
    assert plan.ad_sets[0].ads[0].bodies == []
    meta = FormMeta()
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _of(meta, "creative")  # created without a primary text


def test_an_empty_text_1_does_not_bring_back_the_one_ad_per_ad_set_rule(tmp_path):
    d = _instant(tmp_path, forms=(FORM, FORM2))
    d.ad_sets[0].ads[0].ad_body = ""
    d.ad_sets[0].ads[0].text_variants = [{"name": "T2", "body": "Body two", "headline": "Head two",
                                          "description": ""}]
    assert len(cr.plan_from_draft(d).ad_sets[0].ads) == 2


def test_the_draft_keeps_texts_2_and_3_when_text_1_is_title_only(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.fb_ads_rpa import enrich_draft as en
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft
    lib = {"Head": {"headline": "Head", "body": "", "description": ""},
           "T2": {"headline": "H2", "body": "Body two", "description": ""},
           "T3": {"headline": "H3", "body": "Body three", "description": ""}}
    monkeypatch.setattr(en, "load_ad_account_map", lambda p: {})
    monkeypatch.setattr(en, "resolve_account_entry",
                        lambda *a: SimpleNamespace(ad_account_id="act_test", page_id="page", profile_id="profile"))
    monkeypatch.setattr(en, "lookup_ad_copy_by_title", lambda n: lib[n])
    ad = AdDraft(copy_name="Head", copy2_name="T2", more_copy_names=["T3"])
    draft = CampaignDraft("SA_x", "SA", "Acc", ad_sets=[AdSetDraft("SA_x", ads=[ad])])
    en.enrich_campaign_drafts([draft], account_label="Acc", upload_fb_path=str(tmp_path / "none.xlsx"),
                              ad_account_map_path=str(tmp_path / "map.csv"), creatives_root=str(tmp_path),
                              preview_mode=True)
    assert ad.ad_body == ""
    assert [v["body"] for v in ad.text_variants] == ["Body two", "Body three"]
    assert any("has no body" in w for w in draft.warnings)  # warning only
    assert not [e for e in draft.errors if "text" in e.lower()], draft.errors
