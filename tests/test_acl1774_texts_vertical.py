"""ACL-1774: up to five texts or a separate Stories/Reels file on the API backend.

Один текст и один файл идут прежним `object_story_spec`. Вертикальный файл с ОДНИМ текстом -
`asset_feed_spec`: два файла с метками и два правила по площадкам. Несколько текстов: с вертикальным
файлом Meta отказала вживую (100/1885878), без него используется Dynamic Creative по решению 20.10.
Кампания PAUSED, дочерние объекты следуют проверенному guard ACL-2114. Ни одного живого вызова:
Graph — поддельная Meta из теста ACL-1761.
"""
from __future__ import annotations

import copy
import json
import shutil
import struct
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
from tests.test_acl1761_api_create import ACT, TOKEN, FakeMeta, _draft, _run  # noqa: E402


def png(w: int, h: int, salt: str = "") -> bytes:
    """Минимальный PNG: заголовок с размером (этого хватает чтению размера) и соль для разных md5."""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h)
            + b"\x08\x02\x00\x00\x00" + salt.encode())


def _ad_with(tmp_path, *, main=(1080, 1080), vertical=(1080, 1920), texts=0):
    """Черновик из одного объявления: основной файл, необязательный вертикальный, `texts` доп. текстов."""
    d = _draft(tmp_path, ads=1)
    ad = d.ad_sets[0].ads[0]
    Path(ad.creative_path).write_bytes(png(*main, salt="main"))
    if vertical:
        v = tmp_path / "1000_story.png"
        v.write_bytes(png(*vertical, salt="vert"))
        ad.vertical_filename, ad.vertical_path = v.name, str(v)
    ad.text_variants = [{"name": f"T{i}", "body": f"Body variant {i}", "headline": f"Head variant {i}",
                         "description": f"Desc variant {i}"} for i in range(2, 2 + texts)]
    return d


def _creative(meta):
    return next(o for o in meta.objects.values() if o["_kind"] == "creative")


# ───────────────────────── план ─────────────────────────

def test_one_text_and_one_file_keep_the_old_creative(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path, vertical=None))
    assert out.error == "" and out.readback["ok"], out.mismatches
    cre = _creative(meta)
    assert "asset_feed_spec" not in cre and "link_data" in cre["object_story_spec"]


def test_several_texts_go_as_dynamic_creative_on_their_own_ad_set(tmp_path):
    """Решение владельца 2026-10-20: до 5 текстов - Dynamic Creative: флаг на наборе, лента без правил."""
    for texts in (1, 2, 4):
        meta = FakeMeta()
        out, _ = _run(meta, _ad_with(tmp_path, vertical=None, texts=texts))
        assert out.error == "" and out.readback["ok"], out.mismatches
        ad_set = next(o for o in meta.objects.values() if o["_kind"] == "adset")
        assert ad_set["is_dynamic_creative"] is True and ad_set["status"] == cr.CHILD_STATUS
        fs = _creative(meta)["asset_feed_spec"]
        assert len(fs["bodies"]) == texts + 1 and len(fs["titles"]) == texts + 1
        assert "asset_customization_rules" not in fs and len(fs["images"]) == 1
        assert "adlabels" not in fs["images"][0], "один файл - без меток и правил"
        assert all(p["status"] == (cr.PAUSED if path.endswith("/campaigns") else cr.CHILD_STATUS)
                   for path, p in meta.posts if path.endswith(("/campaigns", "/adsets", "/ads")))


def test_a_single_text_ad_set_is_not_dynamic(tmp_path):
    meta = FakeMeta()
    _run(meta, _ad_with(tmp_path, vertical=None))
    ad_set = next(o for o in meta.objects.values() if o["_kind"] == "adset")
    assert "is_dynamic_creative" not in ad_set


def test_a_dynamic_ad_must_be_alone_in_its_ad_set(tmp_path):
    d = _draft(tmp_path, ads=2)
    d.ad_sets[0].ads[0].text_variants = [{"name": "T2", "body": "Body two", "headline": "Head two",
                                          "description": "Desc two"}]
    with pytest.raises(cr.PlanError, match="only one ad in such an ad set"):
        cr.plan_from_draft(d)
    meta = FakeMeta()
    try:
        _run(meta, d)
    except cr.PlanError:
        pass
    assert meta.posts == []


def test_dynamic_flag_is_true_only_and_only_on_ad_sets():
    adset = {"name": "s", "campaign_id": "1", "status": "PAUSED", "is_dynamic_creative": True}
    assert wp.check(ACT, "POST", f"/{ACT}/adsets", adset) == "adsets"
    for bad in (False, "true", 1, None):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/adsets", dict(adset, is_dynamic_creative=bad))
    for edge in ("campaigns", "ads", "adcreatives"):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/{edge}", {"is_dynamic_creative": True, "name": "x", "status": "PAUSED"})


def test_repeats_and_blanks_collapse_to_one_text_and_pass(tmp_path):
    d = _ad_with(tmp_path, vertical=None)
    d.ad_sets[0].ads[0].text_variants = [{"body": "Body 0 ", "headline": "", "description": ""}]
    ad = cr.plan_from_draft(d).ad_sets[0].ads[0]
    assert len(ad.bodies) == 1 and len(ad.titles) == 1 and not ad.uses_feed


def test_a_sixth_text_is_refused_before_any_write(tmp_path):
    six = _ad_with(tmp_path, vertical=None, texts=5)           # основной + 5 = 6 текстов
    with pytest.raises(cr.PlanError, match="up to 5 primary texts"):
        cr.plan_from_draft(six)
    meta = FakeMeta()
    try:
        _run(meta, six)
    except cr.PlanError:
        pass
    assert meta.posts == []


def test_an_instant_form_ad_with_several_texts_is_refused_not_guessed(tmp_path):
    d = _ad_with(tmp_path, vertical=None, texts=1)
    d.ad_sets[0].destination = "instant_form"
    d.ad_sets[0].ads[0].lead_form_id = "123456"
    with pytest.raises(cr.PlanError, match="Instant Form with an asset feed is not verified"):
        cr.plan_from_draft(d)


# ───────────────────────── вертикальный файл ─────────────────────────

def test_a_vertical_file_goes_up_with_two_labelled_files_and_two_rules(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path))
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert len([p for p, _ in meta.posts if p.endswith("/adimages")]) == 2
    fs = _creative(meta)["asset_feed_spec"]
    labels = [i["adlabels"][0]["name"] for i in fs["images"]]
    assert labels == ["acl_main", "acl_vertical"]
    rules = {r["image_label"]["name"]: r["customization_spec"] for r in fs["asset_customization_rules"]}
    assert rules["acl_vertical"]["facebook_positions"] == ["story", "facebook_reels"]
    assert rules["acl_vertical"]["instagram_positions"] == ["story", "reels"]
    assert "story" not in rules["acl_main"]["facebook_positions"] and "feed" in rules["acl_main"]["facebook_positions"]
    assert all(p["status"] == (cr.PAUSED if path.endswith("/campaigns") else cr.CHILD_STATUS)
               for path, p in meta.posts if path.endswith(("/campaigns", "/adsets", "/ads")))


def test_texts_and_the_vertical_file_together_are_refused_before_any_write(tmp_path):
    """Живой прогон 2026-10-14 (Doc120): Meta отказала 100/1885878 - правило берёт один текст."""
    for texts in (1, 3):
        meta = FakeMeta()
        with pytest.raises(cr.PlanError) as e:
            cr.plan_from_draft(_ad_with(tmp_path, texts=texts))
        assert wp.MSG_TEXTS_VERTICAL in str(e.value)
        try:
            _run(meta, _ad_with(tmp_path, texts=texts))
        except cr.PlanError:
            pass
        assert meta.posts == []


def test_a_vertical_file_with_one_text_has_one_body_and_labelled_rules(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path))
    assert out.error == "" and out.readback["ok"], out.mismatches
    fs = _creative(meta)["asset_feed_spec"]
    assert len(fs["bodies"]) == len(fs["titles"]) == len(fs["descriptions"]) == 1
    assert [r["image_label"]["name"] for r in fs["asset_customization_rules"]] == ["acl_vertical", "acl_main"]
    assert {i["adlabels"][0]["name"] for i in fs["images"]} == {"acl_main", "acl_vertical"}


@pytest.mark.parametrize("main,vertical,message", [
    ((1080, 1920), (1080, 1920), "the main file must be 1:1 or 4:5"),
    ((1920, 1080), (1080, 1920), "the main file must be 1:1 or 4:5"),
    ((1080, 1080), (1080, 1080), "must be 9:16"),
    ((1080, 1350), (1200, 628), "must be 9:16"),
])
def test_a_wrong_aspect_ratio_is_refused_before_any_upload(tmp_path, main, vertical, message):
    meta = FakeMeta()
    d = _ad_with(tmp_path, main=main, vertical=vertical)
    out, _ = _run(meta, d)
    assert message in out.error, out.error
    assert meta.posts == []


def test_the_ratios_1_1_4_5_and_9_16_pass_with_a_small_tolerance(tmp_path):
    for main in ((1080, 1080), (1080, 1350), (1080, 1352)):
        meta = FakeMeta()
        out, _ = _run(meta, _ad_with(tmp_path, main=main, vertical=(1080, 1920)))
        assert out.error == "", (main, out.error)


def test_an_unreadable_size_is_refused_not_guessed(tmp_path):
    d = _ad_with(tmp_path)
    Path(d.ad_sets[0].ads[0].vertical_path).write_bytes(b"not an image at all")
    meta = FakeMeta()
    out, _ = _run(meta, d)
    assert "cannot be read" in out.error and meta.posts == []


def test_a_vertical_file_without_a_main_file_and_a_kind_mismatch_are_plan_errors(tmp_path):
    d = _ad_with(tmp_path)
    d.ad_sets[0].ads[0].creative_path = ""
    with pytest.raises(cr.PlanError, match="needs the main file too"):
        cr.plan_from_draft(d)
    d = _ad_with(tmp_path)
    ad = d.ad_sets[0].ads[0]
    ad.vertical_path = str(tmp_path / "x.mp4")
    with pytest.raises(cr.PlanError, match="two images or two videos"):
        cr.plan_from_draft(d)
    d = _ad_with(tmp_path)
    d.ad_sets[0].ads[0].vertical_path = ""
    with pytest.raises(cr.PlanError, match="Stories / Reels file was not found"):
        cr.plan_from_draft(d)


def test_the_check_plan_shows_the_vertical_file_and_refuses_the_combination_without_writing(tmp_path):
    from tests.test_acl1819_preflight import _audit

    meta = FakeMeta()
    out = _audit(meta, _ad_with(tmp_path))
    assert meta.posts == []
    ad = out.plan["ad_sets"][0]["ads"][0]
    assert ad["vertical_file"] and len(ad["creative"]["asset_feed_spec"]["bodies"]) == 1
    assert len(ad["creative"]["asset_feed_spec"]["asset_customization_rules"]) == 2
    for texts, msg in ((1, wp.MSG_TEXTS_VERTICAL),):
        meta = FakeMeta()
        try:
            out = _audit(meta, _ad_with(tmp_path, texts=texts))
        except cr.PlanError as e:
            assert msg in str(e)
        else:
            assert msg in json.dumps(out.__dict__, default=str)
        assert meta.posts == []


# ───────────────────────── сверка ─────────────────────────

@pytest.mark.parametrize("key,value,field", [
    ("creative.asset_feed_spec.bodies", [{"text": "Other body"}], "primary_texts"),
    ("creative.asset_feed_spec.titles", [{"text": "Other head"}], "headlines"),
    ("creative.asset_feed_spec.descriptions", [{"text": "Other"}], "descriptions"),
    ("creative.asset_feed_spec.call_to_action_types", ["SIGN_UP"], "call_to_action"),
])
def test_readback_checks_every_text_field(tmp_path, key, value, field):
    meta = FakeMeta()
    meta.mutate_ads[key] = value
    out, _ = _run(meta, _ad_with(tmp_path))
    assert field in {m["field"] for m in out.mismatches}, out.mismatches


def test_readback_checks_both_files_and_their_placements(tmp_path):
    meta = FakeMeta()
    meta.mutate_ads["creative.asset_feed_spec.images"] = [
        {"hash": "0" * 32, "adlabels": {"data": [{"name": "acl_main"}]}},
        {"hash": "1" * 32, "adlabels": {"data": [{"name": "acl_vertical"}]}}]
    out, _ = _run(meta, _ad_with(tmp_path))
    bad = {m["field"] for m in out.mismatches}
    assert {"main_file", "stories_reels_file"} <= bad, out.mismatches


def test_readback_catches_a_swapped_placement_rule(tmp_path):
    meta = FakeMeta()
    meta.mutate_ads["creative.asset_feed_spec.asset_customization_rules"] = [
        {"image_label": {"name": "acl_vertical"}, "customization_spec": {
            "publisher_platforms": ["facebook"], "facebook_positions": ["feed"]}},
        {"image_label": {"name": "acl_main"}, "customization_spec": {
            "publisher_platforms": ["facebook"], "facebook_positions": ["story"]}}]
    out, _ = _run(meta, _ad_with(tmp_path))
    assert {"placements_stories_reels", "placements_main"} <= {m["field"] for m in out.mismatches}


def test_readback_catches_a_lost_file(tmp_path):
    meta = FakeMeta()
    meta.mutate_ads["creative.asset_feed_spec.images"] = [meta_img for meta_img in []]
    out, _ = _run(meta, _ad_with(tmp_path))
    assert "file_count" in {m["field"] for m in out.mismatches}


def test_a_repeated_run_does_not_upload_the_pair_twice(tmp_path):
    meta = FakeMeta()
    d = _ad_with(tmp_path)
    _run(meta, d)
    n = len([p for p, _ in meta.posts if p.endswith("/adimages")])
    _run(meta, d)
    assert len([p for p, _ in meta.posts if p.endswith("/adimages")]) == n == 2


def test_the_creator_never_sends_what_its_own_policy_would_refuse(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path))     # FakeMeta зовёт политику на каждой записи
    assert out.error == "" and out.readback["ok"]


# ───────────────────────── политика записи: Python и страница ─────────────────────────

def _feed(**over) -> dict:
    spec = {"bodies": [{"text": "b1"}], "titles": [{"text": "t1"}],
            "descriptions": [{"text": "d1"}], "images": [{"hash": "a" * 32}],
            "link_urls": [{"website_url": "https://x.invalid/l"}], "call_to_action_types": ["LEARN_MORE"],
            "ad_formats": ["SINGLE_IMAGE"]}
    spec.update(over)
    return spec


def _pair(**over) -> dict:
    rule = lambda lab, spec: {"image_label": {"name": lab}, "customization_spec": spec}  # noqa: E731
    spec = _feed(images=[{"hash": "a" * 32, "adlabels": [{"name": "acl_main"}]},
                         {"hash": "b" * 32, "adlabels": [{"name": "acl_vertical"}]}],
                 asset_customization_rules=[
                     rule("acl_vertical", {"publisher_platforms": ["facebook", "instagram"],
                                           "facebook_positions": ["story"], "instagram_positions": ["story", "reels"]}),
                     rule("acl_main", {"publisher_platforms": ["facebook"], "facebook_positions": ["feed"]})])
    spec.update(over)
    return spec


def _cre(spec, **over) -> dict:
    p = {"name": "n", "object_story_spec": {"page_id": "111"}, "asset_feed_spec": spec,
         "degrees_of_freedom_spec": cr.creative_features(), "contextual_multi_ads": dict(cr.MULTI_ADS_OPT_OUT),
         "adlabels": [{"name": "t"}]}
    p.update(over)
    return p


def _drop(spec, key):
    s = copy.deepcopy(spec)
    s.pop(key)
    return s


def _mut(spec, fn):
    s = copy.deepcopy(spec)
    fn(s)
    return s


POLICY_CASES = [
    ("instagram_feed", _cre(_feed(), object_story_spec={"page_id": "111", "instagram_user_id": "123456"}), True),
    ("instagram_feed_bad_id", _cre(_feed(), object_story_spec={"page_id": "111", "instagram_user_id": "bad"}), False),
    ("instagram_feed_extra_story_key", _cre(_feed(), object_story_spec={"page_id": "111", "instagram_user_id": "123456", "unknown": "x"}), False),
    ("texts_ok", _cre(_feed()), True),
    # Несколько текстов - Dynamic Creative: до 5, но только при одном файле (с правилами Meta отказала, 2026-10-14).
    ("two_bodies", _cre(_feed(bodies=[{"text": "b1"}, {"text": "b2"}])), True),
    ("five_texts", _cre(_feed(bodies=[{"text": f"b{i}"} for i in range(5)])), True),
    ("two_titles", _cre(_feed(titles=[{"text": "t1"}, {"text": "t2"}])), True),
    ("two_descriptions", _cre(_feed(descriptions=[{"text": "d1"}, {"text": "d2"}])), True),
    ("pair_two_bodies", _cre(_pair(bodies=[{"text": "b1"}, {"text": "b2"}])), False),
    ("six_bodies", _cre(_feed(bodies=[{"text": f"b{i}"} for i in range(6)])), False),
    ("six_titles", _cre(_feed(titles=[{"text": f"t{i}"} for i in range(6)])), False),
    ("six_descriptions", _cre(_feed(descriptions=[{"text": f"d{i}"} for i in range(6)])), False),
    ("no_descriptions_ok", _cre(_drop(_feed(), "descriptions")), True),
    ("empty_body", _cre(_feed(bodies=[{"text": "  "}])), False),
    ("body_too_long", _cre(_feed(bodies=[{"text": "x" * 1025}])), False),
    ("title_too_long", _cre(_feed(titles=[{"text": "x" * 256}])), False),
    ("text_with_extra_key", _cre(_feed(bodies=[{"text": "b", "adlabels": []}])), False),
    ("extra_spec_key", _cre(_feed(optimization_type="REGULAR")), False),
    ("extra_spec_key_group", _cre(_feed(groups=[])), False),
    ("no_bodies", _cre(_drop(_feed(), "bodies")), False),
    ("no_titles", _cre(_drop(_feed(), "titles")), False),
    ("pair_ok", _cre(_pair()), True),
    ("pair_one_rule", _cre(_mut(_pair(), lambda s: s["asset_customization_rules"].pop())), False),
    ("pair_three_rules", _cre(_mut(_pair(), lambda s: s["asset_customization_rules"].append(
        copy.deepcopy(s["asset_customization_rules"][0])))), False),
    ("pair_unlabeled", _cre(_mut(_pair(), lambda s: s["images"][0].pop("adlabels"))), False),
    ("pair_foreign_label", _cre(_mut(_pair(), lambda s: s["images"][0].update(adlabels=[{"name": "mine"}]))), False),
    ("pair_same_label", _cre(_mut(_pair(), lambda s: s["images"][1].update(adlabels=[{"name": "acl_main"}]))), False),
    ("pair_rule_label_unknown", _cre(_mut(_pair(), lambda s: s["asset_customization_rules"][0].update(
        image_label={"name": "acl_other"}))), False),
    ("pair_rule_extra_key", _cre(_mut(_pair(), lambda s: s["asset_customization_rules"][0].update(priority=1))), False),
    ("pair_unknown_position", _cre(_mut(_pair(), lambda s: s["asset_customization_rules"][1][
        "customization_spec"].update(facebook_positions=["marketplace"]))), False),
    ("pair_unknown_spec_key", _cre(_mut(_pair(), lambda s: s["asset_customization_rules"][1][
        "customization_spec"].update(device_platforms=["mobile"]))), False),
    ("pair_empty_spec", _cre(_mut(_pair(), lambda s: s["asset_customization_rules"][1].update(
        customization_spec={}))), False),
    ("three_files", _cre(_mut(_pair(), lambda s: s["images"].append({"hash": "c" * 32}))), False),
    ("single_with_rules", _cre(_feed(asset_customization_rules=_pair()["asset_customization_rules"])), False),
    ("single_labelled", _cre(_feed(images=[{"hash": "a" * 32, "adlabels": [{"name": "acl_main"}]}])), False),
    ("images_and_videos", _cre(_feed(videos=[{"video_id": "12345", "thumbnail_url": "https://t.invalid/x.jpg"}])), False),
    ("bad_hash", _cre(_feed(images=[{"hash": "zz"}])), False),
    ("format_mismatch", _cre(_feed(ad_formats=["SINGLE_VIDEO"])), False),
    ("format_automatic", _cre(_feed(ad_formats=["AUTOMATIC_FORMAT"])), False),
    ("link_http", _cre(_feed(link_urls=[{"website_url": "http://x.invalid/"}])), False),
    ("link_with_deeplink", _cre(_feed(link_urls=[{"website_url": "https://x.invalid/", "deeplink_url": "x"}])), False),
    ("cta_unknown", _cre(_feed(call_to_action_types=["WATCH_MORE"])), False),
    ("cta_two", _cre(_feed(call_to_action_types=["LEARN_MORE", "SIGN_UP"])), False),
    ("story_with_link_data", _cre(_feed(), object_story_spec={"page_id": "111", "link_data": {"link": "https://x.invalid/"}}), False),
    ("story_missing", _cre(_feed(), object_story_spec=None), False),
    ("form_id_in_feed", _cre(_feed(lead_gen_form_id="123456")), False),
    ("nested_feed", _cre(_feed(), object_story_spec={"page_id": "111", "asset_feed_spec": _feed()}), False),
]
POLICY_CASES.append(("video_ok", _cre(_mut(_feed(), lambda s: (s.pop("images"), s.update(
    videos=[{"video_id": "12345", "thumbnail_url": "https://t.invalid/x.jpg"}], ad_formats=["SINGLE_VIDEO"])))), True))


@pytest.mark.parametrize("name,params,allowed", POLICY_CASES, ids=[c[0] for c in POLICY_CASES])
def test_python_policy_matrix(name, params, allowed):
    if allowed:
        assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", params) == "adcreatives"
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/adcreatives", params)


def test_the_new_key_exists_only_on_creatives():
    for edge in ("campaigns", "adsets", "ads", "adimages", "advideos"):
        assert "asset_feed_spec" not in wp.EDGE_KEYS[edge], edge
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/{edge}", {"asset_feed_spec": _feed(), "name": "x"})


def test_nothing_else_was_loosened():
    """Закрытый список ключей креатива: только прежние плюс одна запись ACL-1774."""
    assert wp.EDGE_KEYS["adcreatives"] == frozenset({
        "name", "object_story_spec", "degrees_of_freedom_spec", "adlabels", "contextual_multi_ads", "asset_feed_spec"})
    assert wp.EDGE_KEYS["ads"] == frozenset({"name", "adset_id", "creative", "status", "adlabels"})
    assert wp.EDGE_KEYS["adimages"] == frozenset() and wp.EDGE_KEYS["advideos"] == frozenset({"name", "title"})
    assert "is_dynamic_creative" in wp.EDGE_KEYS["adsets"] and not any(
        "is_dynamic_creative" in v for k, v in wp.EDGE_KEYS.items() if k != "adsets")
    assert wp.ASSET_FEED_MAX_TEXTS == 5 and wp.ASSET_FEED_TEXTS_WITH_RULES == 1 and wp.ASSET_FEED_MAX_MEDIA == 2


def test_the_policy_export_carries_the_asset_feed_rules():
    p = wp.policy_for_page(ACT)
    af = p["asset_feed"]
    assert af["key"] == "asset_feed_spec" and af["max_texts"] == 5 and af["max_texts_with_rules"] == 1 and af["max_media"] == 2
    assert af["text_max"] == {"bodies": 1024, "titles": 255, "descriptions": 255}
    assert "asset_feed_spec" in p["edges"]["adcreatives"] and "asset_feed_spec" in p["json_keys"]
    assert set(af["facebook_positions"]) >= {"feed", "story", "facebook_reels"}


_NODE = r"""
const fakeFetch = async () => ({ status: 200, text: async () => JSON.stringify({ id: '5' }), headers: { get: () => null } });
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams',
             HELPER)(G, loc, doc, { getEntriesByType: () => [] }, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
const cases = CASES;
(async () => {
  const out = {};
  for (const c of cases) {
    const r = await h.call({ method: 'POST', path: c.edge ? '/' + ACTV + '/' + c.edge : '/' + ACTV + '/adcreatives', params: c.params });
    out[c.name] = r.error === 'write_refused' ? 'refused' : (r.body ? 'sent' : JSON.stringify(r));
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


_SET = {"name": "s", "campaign_id": "1", "status": "PAUSED"}
ADSET_FLAG_CASES = [
    ("flag_true", "adsets", {**_SET, "is_dynamic_creative": True}, True),
    ("flag_false", "adsets", {**_SET, "is_dynamic_creative": False}, False),
    ("flag_string", "adsets", {**_SET, "is_dynamic_creative": "true"}, False),
    ("flag_one", "adsets", {**_SET, "is_dynamic_creative": 1}, False),
    ("flag_on_ads", "ads", {"name": "a", "adset_id": "1", "status": "PAUSED", "creative": {"creative_id": "5"},
                            "is_dynamic_creative": True}, False),
    ("flag_on_campaigns", "campaigns", {"name": "c", "objective": "OUTCOME_LEADS", "status": "PAUSED",
                                        "is_dynamic_creative": True}, False),
]


def _node_verdicts() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_1774"
    cases = [{"name": n, "params": p} for n, p, _ in POLICY_CASES]
    cases += [{"name": n, "params": p, "edge": e} for n, e, p, _ in ADSET_FLAG_CASES]
    script = (_NODE.replace("ACTV", json.dumps(ACT)).replace("TOKENV", json.dumps(TOKEN))
              .replace("NAME", json.dumps(name)).replace("CASES", json.dumps(cases))
              .replace("HELPER", json.dumps(tc.helper_source(name, wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, timeout=90)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_page_helper_gives_the_same_verdicts_as_the_python_policy():
    got = _node_verdicts()
    for name, _edge, _params, allowed in ADSET_FLAG_CASES:
        assert got[name] == ("sent" if allowed else "refused"), (name, got[name])
    for name, _params, allowed in POLICY_CASES:
        assert got[name] == ("sent" if allowed else "refused"), (name, got[name])


# ───────────────────────── предпроверка (ACL-1819) ─────────────────────────

def _inp(**ad) -> dict:
    row = {"name": "1000 - T", "file": "1000_img.png", "form_id": "", "body": "Body", "headline": "Head", "cta": "LEARN_MORE"}
    row.update(ad)
    return {"destination": "website", "objective": "leads", "link": "https://t.example.com/c?sub=1", "country": "Mexico",
            "budget": "20", "age_min": 35, "age_max": 35, "ads": [row]}


def _verdict(inp: dict, rule: str) -> dict:
    return {v["rule"]: v for v in pf.evaluate_client(inp)}[rule]


@pytest.mark.parametrize("rule,ad,status", [
    ("texts_limit", {}, "ok"),
    ("texts_limit", {"bodies": [f"b{i}" for i in range(5)]}, "ok"),
    ("texts_limit", {"bodies": [f"b{i}" for i in range(6)]}, "error"),
    ("texts_limit", {"bodies": ["same", " same ", "s a m e"] + [f"b{i}" for i in range(4)]}, "ok"),
    ("texts_limit", {"headlines": [f"h{i}" for i in range(6)]}, "error"),
    ("texts_limit", {"descriptions": [f"d{i}" for i in range(6)]}, "error"),
    ("vertical_needs_main", {}, "ok"),
    ("vertical_needs_main", {"vertical_file": "1000_story.png"}, "ok"),
    ("vertical_needs_main", {"file": "", "vertical_file": "1000_story.png"}, "error"),
])
def test_client_rules_for_texts_and_the_vertical_file(rule, ad, status):
    assert _verdict(_inp(**ad), rule)["status"] == status


def test_the_page_gives_the_same_verdicts_for_the_new_rules():
    fixtures = {f"c{i}": _inp(**ad) for i, (_r, ad, _s) in enumerate([
        ("", {"bodies": [f"b{i}" for i in range(6)]}, ""), ("", {"file": "", "vertical_file": "v.png"}, ""),
        ("", {"bodies": ["a", "a "]}, ""), ("", {"vertical_file": "v.png"}, "")])}
    f = ROOT / "tests" / "preflight_harness.cjs"
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(fixtures, fh)
        path = fh.name
    try:
        r = subprocess.run([node, str(f), str(ROOT / "src" / "web" / "templates" / "report.html"), "--evaluate", path],
                           capture_output=True, text=True, timeout=90)
    finally:
        Path(path).unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    for name, inp in fixtures.items():
        assert js[name] == pf.evaluate_client(inp), name


def test_the_server_names_a_wrong_ratio_in_the_audit(tmp_path):
    from tests.test_acl1819_preflight import _audit

    out = _audit(FakeMeta(), _ad_with(tmp_path, main=(1080, 1080), vertical=(1080, 1080)))
    row = {v["rule"]: v for v in out.audit}["vertical_ratio"]
    assert row["status"] == "error" and "9:16" in row["detail"]
    ok = _audit(FakeMeta(), _ad_with(tmp_path))
    assert {v["rule"]: v for v in ok.audit}["vertical_ratio"]["status"] == "ok"


# ───────────────────────── форма → слоты → RPA ─────────────────────────

def _fields(**over):
    f = {"country": "Mexico", "account_label": "Acc", "tracking_link": "https://t.invalid/x", "camp_count": "1",
         "camp_0_name": "MX_test", "camp_0_ad_count": "1", "camp_0_budget": "20",
         "camp_0_ad_0_creative_file": "a.png", "camp_0_ad_0_copy": "T1"}
    f.update(over)
    return f


def test_old_forms_give_slots_without_the_new_keys():
    from src.web import app as web

    slots = web._parse_build_upload_form(_fields())[2]
    assert not {"copy_more", "creative_file_vertical"} & set(slots[0])
    assert web._parse_build_upload_form(_fields(camp_0_ad_0_copy3="", camp_0_ad_0_creative_file_vertical="")) == \
        web._parse_build_upload_form(_fields())


def test_filled_fields_reach_the_slot_and_the_draft():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields(
        camp_0_ad_0_copy2="T2", camp_0_ad_0_copy3="T3", camp_0_ad_0_copy5="T5",
        camp_0_ad_0_creative_file_vertical="a_story.png"))
    assert slots[0]["copy2"] == "T2" and slots[0]["copy_more"] == ["T3", "", "T5"]
    assert slots[0]["creative_file_vertical"] == "a_story.png"
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    ad = draft.ad_sets[0].ads[0]
    assert ad.more_copy_names == ["T3", "", "T5"] and ad.vertical_filename == "a_story.png"
    prev = ad.to_preview_dict()
    assert prev["more_copy_names"] == ["T3", "", "T5"] and prev["vertical_file"] == "a_story.png"


def test_an_ad_without_the_new_fields_previews_exactly_as_before():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields())
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    prev = draft.ad_sets[0].ads[0].to_preview_dict()
    assert "more_copy_names" not in prev and "vertical_file" not in prev


def test_rpa_refuses_the_new_fields_naming_each(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    assert web._api_only_fields_refusal(web._parse_build_upload_form(_fields(camp_0_ad_0_copy2="T2"))[2]) is None
    for key, title in (("camp_0_ad_0_copy3", "Primary text 3-5"), ("camp_0_ad_0_copy4", "Primary text 3-5"),
                       ("camp_0_ad_0_creative_file_vertical", "Stories / Reels file")):
        resp = web._api_only_fields_refusal(web._parse_build_upload_form(_fields(**{key: "x"}))[2])
        assert resp is not None and resp.status_code == 409, key
        err = json.loads(resp.body)["error"]
        assert title in err and "ACL_FB_BACKEND=api" in err, err
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    assert web._api_only_fields_refusal(web._parse_build_upload_form(_fields(camp_0_ad_0_copy3="x"))[2]) is None


# ───────────────────────── страница ─────────────────────────

def _page() -> str:
    return (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")


def test_the_page_has_the_controls_not_the_placeholders():
    p = _page()
    assert "future('texts'" not in p and "future('media'" not in p
    assert "texts:      { task: 'ACL-1774'" not in p and "media:      { task: 'ACL-1772'" not in p
    for marker in ("Primary text ' + n", "cb-textx-add", "creative_file_vertical", "cb-vertical-search",
                   "cbSyncTextRows", "Stories / Reels file", "name$=\"_creative_file_vertical\""):
        assert marker in p, marker
    assert "copy' + n" in p and "'_copy3'" in p        # имена полей copy3..copy5 уходят и в предпроверку


def test_the_page_routes_errors_to_the_new_fields():
    p = _page()
    assert "[/primary text [3-5]/i, '.cb-textx-search', 'ad']" in p
    assert r"[/stories ?\/ ?reels/i, '.cb-vertical-search', 'ad']" in p


def test_the_texts_and_the_vertical_file_are_copied_with_the_ad():
    p = _page()
    assert "cb-textx-sel-wrap" in p and "newRow.dataset.textsShown" in p and "srcVert" in p


def test_check_plan_names_dynamic_creative_and_readback_covers_both_directions(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _ad_with(tmp_path, vertical=None, texts=1))
    assert out.error == "" and out.readback["ok"], out.mismatches
    flags = [c for c in out.readback["checks"] if c["field"] == "dynamic_creative"]
    assert flags and all(c["expected"] is True and c["actual"] is True for c in flags)
    single = FakeMeta()
    out1, _ = _run(single, _ad_with(tmp_path, vertical=None))
    flags1 = [c for c in out1.readback["checks"] if c["field"] == "dynamic_creative"]
    assert flags1 and all(c["expected"] is False and c["actual"] is False and c["ok"] for c in flags1)


@pytest.fixture
def enrich_texts(monkeypatch, tmp_path):
    from tests.test_second_primary_text_reaches_the_robot import enrich
    return enrich.__wrapped__(monkeypatch, tmp_path)


def test_equal_bodies_keep_different_library_headlines_for_the_api(enrich_texts):
    from src.fb_ads_rpa.draft import AdDraft
    draft = enrich_texts(AdDraft(copy_name="Text6", copy2_name="Same", more_copy_names=["Same"]))
    ad = draft.ad_sets[0].ads[0]
    assert ad.ad_body2 == "", "the legacy RPA second-body behavior must remain unchanged"
    assert [v["headline"] for v in ad.text_variants] == ["H"]
    assert [v["body"] for v in ad.text_variants] == ["Texto  uno línea\ndos"]


def test_a_sparse_fifth_text_error_keeps_its_actual_field_number(enrich_texts):
    from src.web import app as web
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    fields = _fields(camp_0_ad_0_copy="Text6", camp_0_ad_0_copy5="Gone")
    country, account, slots = web._parse_build_upload_form(fields)
    [draft] = slots_to_campaign_drafts(slots, account_label=account, country=country)
    enriched = enrich_texts(draft.ad_sets[0].ads[0])
    assert any(e.startswith("Primary text 5 'Gone'") for e in enriched.errors), enriched.errors


def test_reused_pair_cannot_pass_readback_without_its_media_labels(tmp_path):
    meta = FakeMeta()
    draft = _ad_with(tmp_path)
    first, _ = _run(meta, draft)
    assert not first.error and first.readback["ok"]
    media = copy.deepcopy(_creative(meta)["asset_feed_spec"]["images"])
    for row in media:
        row.pop("adlabels", None)
    meta.mutate_ads["creative.asset_feed_spec.images"] = media
    writes = len(meta.posts)
    second, _ = _run(meta, draft)
    assert not second.readback["ok"], second.readback
    assert "media_labels" in {c["field"] for c in second.mismatches}
    assert len(meta.posts) == writes, "readback of a reused pair must not recreate objects"


def test_changed_feed_after_a_partial_run_gets_a_new_creative_without_duplicate_upload(tmp_path):
    meta = FakeMeta()
    draft = _ad_with(tmp_path, vertical=None, texts=1)
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.stop_kind == "unknown"
    assert len([o for o in meta.objects.values() if o["_kind"] == "creative"]) == 1
    draft.ad_sets[0].ads[0].text_variants[0]["body"] = "A changed second text"
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"], second.mismatches
    assert len([o for o in meta.objects.values() if o["_kind"] == "creative"]) == 2
    assert len([p for p, _ in meta.posts if p.endswith("/adimages")]) == 1
    writes = len(meta.posts)
    third, _ = _run(meta, draft)
    assert not third.error and third.readback["ok"]
    assert len(meta.posts) == writes


def test_reused_pair_cannot_pass_with_labels_but_no_media_ids(tmp_path):
    meta = FakeMeta()
    draft = _ad_with(tmp_path)
    first, _ = _run(meta, draft)
    assert first.readback["ok"]
    media = copy.deepcopy(_creative(meta)["asset_feed_spec"]["images"])
    for row in media:
        row.pop("hash", None)
    meta.mutate_ads["creative.asset_feed_spec.images"] = media
    second, _ = _run(meta, draft)
    assert {"main_file_binding", "stories_reels_file_binding"} <= {c["field"] for c in second.mismatches}


def test_vertical_media_uses_the_same_bounded_loader_in_prepare_and_audit(tmp_path, monkeypatch):
    meta = FakeMeta()
    draft = _ad_with(tmp_path)
    loaded = []
    original = cr.CampaignCreator.load_media

    def load(self, path, kind):
        loaded.append((path, kind))
        return original(self, path, kind)

    monkeypatch.setattr(cr.CampaignCreator, "load_media", load)
    out, _ = _run(meta, draft)
    ad = draft.ad_sets[0].ads[0]
    assert out.readback["ok"], out.error
    assert (ad.vertical_path, "image") in loaded
    loaded.clear()
    from tests.test_acl1819_preflight import _audit
    audit = _audit(FakeMeta(), draft)
    assert not audit.error, audit.error
    assert (ad.vertical_path, "image") in loaded


def test_both_videos_in_a_pair_keep_the_current_chunked_upload_path(tmp_path, monkeypatch):
    from tests.test_acl1819_preflight import _mp4
    from tests.test_acl1891_video_chunks import ChunkMeta
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", 512)
    draft = _draft(tmp_path, video=True, ads=1)
    ad = draft.ad_sets[0].ads[0]
    Path(ad.creative_path).write_bytes(_mp4(15, 1080, 1080) + b"m" * 2048)
    vertical = tmp_path / "1000_story.mp4"
    vertical.write_bytes(_mp4(15, 1080, 1920) + b"v" * 2048)
    ad.vertical_filename, ad.vertical_path = vertical.name, str(vertical)

    def no_whole_file_read(path):
        raise AssertionError(f"the bounded loader must handle {Path(path).name}")

    meta = ChunkMeta()
    meta.next_id = 100000  # Match the real numeric-ID contract of asset-feed videos.
    out, _ = _run(meta, draft, read_file=no_whole_file_read)
    assert not out.error and out.readback["ok"], out.error or out.mismatches
    assert len(meta.sessions) == 2 and all(s["done"] for s in meta.sessions.values())
    assert all("upload_phase" in p for path, p in meta.posts if path.endswith("/advideos"))


def test_partial_video_retry_ignores_an_expiring_thumbnail_url_in_creative_identity(tmp_path, monkeypatch):
    from tests.test_acl1819_preflight import _mp4
    from tests.test_acl1891_video_chunks import ChunkMeta
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", 512)
    draft = _draft(tmp_path, video=True, ads=1)
    ad = draft.ad_sets[0].ads[0]
    Path(ad.creative_path).write_bytes(_mp4(15, 1080, 1080) + b"m" * 2048)
    ad.text_variants = [{"body": "Second body", "headline": "Second head", "description": ""}]
    original = cr.CampaignCreator._wait_video
    thumbnails = []

    def wait(self, *args, **kw):
        original(self, *args, **kw)
        url = f"https://thumbnail.invalid/video.jpg?expires={len(thumbnails)}"
        thumbnails.append(url)
        return url

    monkeypatch.setattr(cr.CampaignCreator, "_wait_video", wait)
    meta = ChunkMeta()
    meta.next_id = 100000
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, draft, reconcile_wait_s=0)
    assert first.stop_kind == "unknown"
    second, _ = _run(meta, draft)
    assert not second.error and second.readback["ok"], second.error or second.mismatches
    assert len(set(thumbnails)) == 2
    assert len(meta.sessions) == 1
    assert len([o for o in meta.objects.values() if o["_kind"] == "creative"]) == 1
