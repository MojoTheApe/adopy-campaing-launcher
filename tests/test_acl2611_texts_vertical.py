"""ACL-2611: several texts AND a Stories/Reels file publish (labelled feed), offline.

Все тексты идут на основной файл (ленты), первый текст каждого вида - ещё и на Stories/Reels. Форма -
размеченная лента Ads Manager (ACL-2584), расширенная на два файла: основной - метка основного правила,
вертикальный - метка вертикального. Живых вызовов нет: Graph - поддельная Meta из тестов ACL-1761/2528.
Живая проверка на пилотном аккаунте - отдельный шаг (ещё не сделан).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft as _site_draft, _run as _site_run  # noqa: E402
from tests.test_acl1774_texts_vertical import _ad_with, png  # noqa: E402
from tests.test_acl1775_instant_forms import FORM, FormMeta, _instant, _run as _form_run  # noqa: E402
from tests.test_acl2528_form_several_texts import PAGE, _node_matrix  # noqa: E402


def _labels(item):
    return [x["name"] for x in item.get("adlabels", [])]


def _creative(meta):
    return next(o for o in meta.objects.values() if o["_kind"] == "creative")


def _adset(meta):
    return next(o for o in meta.objects.values() if o["_kind"] == "adset")


def _mp4s(tmp_path, ad):
    from tests.test_acl1819_preflight import _mp4
    Path(ad.creative_path).write_bytes(_mp4(15, 1080, 1080) + b"m" * 256)
    vertical = tmp_path / "1000_story.mp4"
    vertical.write_bytes(_mp4(15, 1080, 1920) + b"v" * 256)
    ad.vertical_filename, ad.vertical_path = vertical.name, str(vertical)


def _texts(ad, n):
    ad.text_variants = [{"name": f"T{i}", "body": f"Body variant {i}", "headline": f"Head variant {i}",
                         "description": f"Desc variant {i}"} for i in range(2, 2 + n)]


def website(tmp_path, *, extra=1, video=False):
    """Website: main + Stories/Reels file, 1 + `extra` texts of each kind."""
    if not video:
        return _ad_with(tmp_path, texts=extra)
    d = _site_draft(tmp_path, video=True, ads=1)
    ad = d.ad_sets[0].ads[0]
    _mp4s(tmp_path, ad)
    _texts(ad, extra)
    return d


def instant(tmp_path, *, extra=1, video=False):
    d = _instant(tmp_path, forms=(FORM,), video=video)
    ad = d.ad_sets[0].ads[0]
    if video:
        _mp4s(tmp_path, ad)
    else:
        Path(ad.creative_path).write_bytes(png(1080, 1080, salt="main"))
        v = tmp_path / "1000_story.png"
        v.write_bytes(png(1080, 1920, salt="vert"))
        ad.vertical_filename, ad.vertical_path = v.name, str(v)
    _texts(ad, extra)
    return d


def run_website(draft, video=False):
    meta = FakeMeta()
    if video:
        meta.next_id = 100000
    out, _ = _site_run(meta, draft)
    return meta, out


def run_instant(draft, video=False):
    meta = FormMeta()
    if video:
        meta.next_id = 100000
    out, _ = _form_run(meta, draft)
    return meta, out


# ───────────────────────── shape ─────────────────────────

def _assert_labelled(fs, kind, n_texts, *, instant_form):
    media = "videos" if kind == "video" else "images"
    # Every text of every kind: main label; the first one also the vertical label.
    for key, name in (("bodies", "body"), ("titles", "title"), ("descriptions", "description")):
        assert len(fs[key]) == n_texts
        assert [_labels(x) for x in fs[key]] == [[f"acl_{name}_main", f"acl_{name}_vertical"]] + \
            [[f"acl_{name}_main"]] * (n_texts - 1)
    # Two files: the main one carries the main label only, the Stories/Reels one the vertical label only.
    assert [_labels(x) for x in fs[media]] == [[f"acl_{kind}_main"], [f"acl_{kind}_vertical"]]
    assert fs["link_urls"][0]["adlabels"] == [{"name": "acl_link_main"}, {"name": "acl_link_vertical"}]
    main, vertical = fs["asset_customization_rules"]
    for rule, side in ((main, "main"), (vertical, "vertical")):
        assert rule[f"{kind}_label"] == {"name": f"acl_{kind}_{side}"}
        for key, name in (("body", "body"), ("title", "title"), ("description", "description"), ("link_url", "link")):
            assert rule[f"{key}_label"] == {"name": f"acl_{name}_{side}"}
    assert "story" in vertical["customization_spec"]["facebook_positions"]
    assert "story" not in (main["customization_spec"].get("facebook_positions") or [])
    assert fs["ad_formats"] == [("SINGLE_VIDEO" if kind == "video" else "SINGLE_IMAGE")]
    assert ("call_to_actions" in fs) == instant_form
    assert (fs.get("optimization_type") == "PLACEMENT") == instant_form


@pytest.mark.parametrize("extra", [1, 4], ids=["2 texts", "5 texts"])
@pytest.mark.parametrize("video", [False, True], ids=["image", "video"])
def test_website_several_texts_with_a_vertical_file_publish_labelled(tmp_path, extra, video):
    meta, out = run_website(website(tmp_path, extra=extra, video=video), video)
    assert out.error == "" and out.readback["ok"], out.error or out.mismatches
    ad_set = _adset(meta)
    assert "is_dynamic_creative" not in ad_set and ad_set["status"] == cr.CHILD_STATUS
    fs = _creative(meta)["asset_feed_spec"]
    _assert_labelled(fs, "video" if video else "image", extra + 1, instant_form=False)
    assert fs["link_urls"][0]["website_url"].startswith("https://")
    assert all(p["status"] == (cr.PAUSED if path.endswith("/campaigns") else cr.CHILD_STATUS)
               for path, p in meta.posts if path.endswith(("/campaigns", "/adsets", "/ads")))


@pytest.mark.parametrize("extra", [1, 4], ids=["2 texts", "5 texts"])
@pytest.mark.parametrize("video", [False, True], ids=["image", "video"])
def test_instant_form_several_texts_with_a_vertical_file_publish_labelled(tmp_path, extra, video):
    meta, out = run_instant(instant(tmp_path, extra=extra, video=video), video)
    assert out.error == "" and out.readback["ok"], out.error or out.mismatches
    assert "is_dynamic_creative" not in _adset(meta)
    fs = _creative(meta)["asset_feed_spec"]
    _assert_labelled(fs, "video" if video else "image", extra + 1, instant_form=True)
    assert fs["asset_customization_rules"][0]["customization_spec"] == {}  # ACL-2584 catch-all main rule
    assert fs["call_to_actions"][0]["value"]["lead_gen_form_id"] == FORM


def test_the_plan_has_no_texts_vertical_refusal(tmp_path):
    for build in (website, instant):
        plan = cr.plan_from_draft(build(tmp_path, extra=2))
        ad = plan.ad_sets[0].ads[0]
        assert ad.multi_text and ad.vertical_path and len(ad.bodies) == 3
    assert not hasattr(wp, "MSG_TEXTS_VERTICAL")
    assert not any(r["id"] == "texts_vertical" for r in pf.RULES)


def test_a_website_pair_with_several_texts_and_no_headline_is_refused_at_plan_time(tmp_path):
    d = website(tmp_path, extra=1)
    ad = d.ad_sets[0].ads[0]
    ad.ad_title = ""
    ad.text_variants = [{"body": f"Body {i}", "headline": "", "description": ""} for i in (2, 3)]
    with pytest.raises(cr.PlanError, match="need a headline"):
        cr.plan_from_draft(d)


def test_a_sixth_text_is_still_refused_with_a_vertical_file(tmp_path):
    with pytest.raises(cr.PlanError, match="up to 5 primary texts"):
        cr.plan_from_draft(website(tmp_path, extra=5))
    with pytest.raises(cr.PlanError, match="up to 5 primary texts"):
        cr.plan_from_draft(instant(tmp_path, extra=5))


# ───────────────────────── unchanged behaviour ─────────────────────────

def test_one_text_with_a_vertical_file_keeps_the_old_website_shape(tmp_path):
    meta, out = run_website(_ad_with(tmp_path, texts=0))
    assert out.error == "" and out.readback["ok"], out.mismatches
    fs = _creative(meta)["asset_feed_spec"]
    assert [_labels(x) for x in fs["images"]] == [["acl_main"], ["acl_vertical"]]
    assert "adlabels" not in fs["bodies"][0] and "adlabels" not in fs["link_urls"][0]


def test_several_texts_without_a_vertical_file_stay_dynamic_creative(tmp_path):
    meta, out = run_website(_ad_with(tmp_path, vertical=None, texts=2))
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert _adset(meta)["is_dynamic_creative"] is True
    fs = _creative(meta)["asset_feed_spec"]
    assert "asset_customization_rules" not in fs and "adlabels" not in fs["bodies"][0]


def test_a_dynamic_ad_must_still_be_alone_but_a_vertical_pair_need_not_be(tmp_path):
    d = _site_draft(tmp_path, ads=2)
    d.ad_sets[0].ads[0].text_variants = [{"body": "Body two", "headline": "Head two", "description": ""}]
    with pytest.raises(cr.PlanError, match="only one ad in such an ad set"):
        cr.plan_from_draft(d)
    pair = _site_draft(tmp_path, ads=2)
    ad = pair.ad_sets[0].ads[0]
    Path(ad.creative_path).write_bytes(png(1080, 1080, salt="m"))
    v = tmp_path / "1000_story.png"
    v.write_bytes(png(1080, 1920, salt="v"))
    ad.vertical_filename, ad.vertical_path = v.name, str(v)
    _texts(ad, 2)
    assert len(cr.plan_from_draft(pair).ad_sets[0].ads) == 2


# ───────────────────────── read back ─────────────────────────

def _mismatch(out, field):
    return [m for m in out.mismatches if field in str(m)]


KINDS = {"website": (run_website, website, FakeMeta, _site_run),
         "instant": (run_instant, instant, FormMeta, _form_run)}


@pytest.mark.parametrize("builder", ["website", "instant"])
def test_readback_catches_a_text_missing_its_main_label(tmp_path, builder):
    run, make, meta_cls, runner = KINDS[builder]
    base_meta, _ = run(make(tmp_path, extra=2))
    fs = copy.deepcopy(_creative(base_meta)["asset_feed_spec"])
    fs["bodies"][1]["adlabels"] = [{"name": "acl_body_vertical"}]  # text 2 would reach Stories/Reels too
    meta = meta_cls()
    meta.mutate_ads["creative.asset_feed_spec"] = fs
    out, _ = runner(meta, make(tmp_path, extra=2))
    assert not out.readback["ok"] and _mismatch(out, "bodies_main_label"), out.mismatches


@pytest.mark.parametrize("builder", ["website", "instant"])
def test_readback_catches_swapped_file_labels_and_a_wrong_rule(tmp_path, builder):
    run, make, meta_cls, runner = KINDS[builder]
    base_meta, _ = run(make(tmp_path, extra=1))
    good = _creative(base_meta)["asset_feed_spec"]
    swapped = copy.deepcopy(good)
    swapped["images"][0]["adlabels"], swapped["images"][1]["adlabels"] = \
        swapped["images"][1]["adlabels"], swapped["images"][0]["adlabels"]
    wrong_rule = copy.deepcopy(good)
    wrong_rule["asset_customization_rules"][1]["body_label"] = {"name": "acl_body_main"}
    for fs, field in ((swapped, ""), (wrong_rule, "rule_stories_reels_labels")):
        meta = meta_cls()
        meta.mutate_ads["creative.asset_feed_spec"] = fs
        out, _ = runner(meta, make(tmp_path, extra=1))
        # Swapped file labels bind each file to the wrong rule: the file checks fail (no field named).
        assert not out.readback["ok"] and _mismatch(out, field), (field, out.mismatches)


def test_readback_catches_wrong_stories_reels_placements_on_a_website_pair(tmp_path):
    base_meta, _ = run_website(website(tmp_path, extra=1))
    fs = copy.deepcopy(_creative(base_meta)["asset_feed_spec"])
    fs["asset_customization_rules"][1]["customization_spec"] = {"publisher_platforms": ["facebook"],
                                                                "facebook_positions": ["story"]}
    meta = FakeMeta()
    meta.mutate_ads["creative.asset_feed_spec"] = fs
    out, _ = _site_run(meta, website(tmp_path, extra=1))
    assert not out.readback["ok"] and _mismatch(out, "rule_stories_reels_placements"), out.mismatches


# ───────────────────────── write policy ─────────────────────────

def _creatives(tmp_path):
    made = {}
    for name, run, make, video in (("web_img", run_website, website, False), ("web_vid", run_website, website, True),
                                   ("form_img", run_instant, instant, False), ("form_vid", run_instant, instant, True)):
        meta, res = run(make(tmp_path, extra=2, video=video), video)
        assert res.error == "", res.error
        cre = {k: v for k, v in _creative(meta).items() if k in (
            "name", "object_story_spec", "asset_feed_spec", "degrees_of_freedom_spec", "contextual_multi_ads", "adlabels")}
        made[name] = (cre, name.startswith("form"))
    return made


def _mutations(cre):
    def mut(fn):
        c = copy.deepcopy(cre)
        fn(c["asset_feed_spec"])
        return c

    media = "videos" if "videos" in cre["asset_feed_spec"] else "images"
    short = media[:-1]
    both = [{"name": f"acl_{short}_main"}, {"name": f"acl_{short}_vertical"}]
    return {
        "text3_gets_vertical": mut(lambda s: s["bodies"][2].__setitem__(
            "adlabels", [{"name": "acl_body_main"}, {"name": "acl_body_vertical"}])),
        "first_text_loses_vertical": mut(lambda s: s["titles"][0].__setitem__("adlabels", [{"name": "acl_title_main"}])),
        "files_swapped": mut(lambda s: s[media].reverse()),
        "file_has_both_labels": mut(lambda s: s[media][0].__setitem__("adlabels", both)),
        "three_files": mut(lambda s: s[media].append(copy.deepcopy(s[media][1]))),
        "rule_misses_a_kind": mut(lambda s: s["asset_customization_rules"][1].pop("title_label")),
        "rule_wrong_label": mut(lambda s: s["asset_customization_rules"][0].__setitem__(
            "body_label", {"name": "acl_body_vertical"})),
        "third_rule": mut(lambda s: s["asset_customization_rules"].append(
            copy.deepcopy(s["asset_customization_rules"][1]))),
        "link_unlabelled": mut(lambda s: s["link_urls"][0].pop("adlabels")),
        "sixth_text": mut(lambda s: s["bodies"].extend(
            {"text": f"x{i}", "adlabels": [{"name": "acl_body_main"}]} for i in range(4))),
        "file_extra_key": mut(lambda s: s[media][1].__setitem__("url", "http://evil.example/x")),
        "file_bad_id": mut(lambda s: s[media][1].__setitem__("video_id" if media == "videos" else "hash", "abc")),
        "thumbnail_not_https": mut(lambda s: s[media][1].__setitem__("thumbnail_url", "http://x")
                                   if media == "videos" else s[media][1].__setitem__("hash", "not-a-hash")),
        "empty_text": mut(lambda s: s["bodies"][1].__setitem__("text", "  ")),
    }


def test_policy_accepts_the_builder_shape_and_refuses_every_tampering(tmp_path):
    for name, (cre, on_ad) in _creatives(tmp_path).items():
        assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", cre, on_ad=on_ad) == "adcreatives", name
        for what, bad in _mutations(cre).items():
            with pytest.raises(wp.WriteRefused):
                wp.check(ACT, "POST", f"/{ACT}/adcreatives", bad, on_ad=on_ad)


def test_several_unlabelled_texts_with_two_files_are_still_refused():
    """ACL-1774 (live 14.10, Meta 100/1885878): unlabelled texts next to two placement rules never go out."""
    pos = {"publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}
    spec = {"bodies": [{"text": "a"}, {"text": "b"}], "titles": [{"text": "t"}],
            "images": [{"hash": "a" * 32, "adlabels": [{"name": "acl_main"}]},
                       {"hash": "b" * 32, "adlabels": [{"name": "acl_vertical"}]}],
            "call_to_action_types": ["LEARN_MORE"], "ad_formats": ["SINGLE_IMAGE"],
            "asset_customization_rules": [
                {"customization_spec": pos, "image_label": {"name": "acl_main"}},
                {"customization_spec": dict(pos, facebook_positions=["story"]),
                 "image_label": {"name": "acl_vertical"}}]}
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", {"name": "x", "object_story_spec": {"page_id": PAGE},
                                                      "asset_feed_spec": spec})


def test_page_helper_agrees_with_python_on_the_new_shapes(tmp_path):
    rows = []
    for name, (cre, on_ad) in _creatives(tmp_path).items():
        rows.append((f"{name}_ok", cre, on_ad, True, ""))
        for what, bad in _mutations(cre).items():
            rows.append((f"{name}_{what}", bad, on_ad, False, ""))
    out = _node_matrix(rows)
    for name, _params, _on_ad, allowed, _edge in rows:
        got = out[name]
        if allowed:
            assert got.get("body") == {"id": "5"}, (name, got)
        else:
            assert got.get("error") == "write_refused", (name, got)
