"""ACL-2528: several primary texts on an Instant Form (lead) ad, offline.

First step only: several texts + ONE file on an Instant Form ad set. The creative is an asset feed whose button
carries the lead form id (`asset_feed_spec.call_to_actions[0].value.lead_gen_form_id`), as Ads Manager stores it
(owner capture 2026-11-11); the ad set is Dynamic Creative (mode A1, same as a Website ad today).
The ONE live paused test write is a separate step with the owner; nothing here calls Meta.
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
from tests.test_acl1775_instant_forms import FORM, FORM2, PAGE, FormMeta, _instant, _run  # noqa: E402

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

def test_several_texts_on_a_form_ad_go_as_a_dynamic_feed_with_the_form_in_the_button(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _draft(tmp_path, texts=2))
    assert out.error == "" and out.readback["ok"], out.mismatches
    ad_set, cre = _of(meta, "adset"), _of(meta, "creative")
    # Mode A1: Dynamic Creative flag on the ad set, as on Website; lead form ad set otherwise unchanged.
    assert ad_set["is_dynamic_creative"] is True and ad_set["status"] == cr.CHILD_STATUS
    assert ad_set["destination_type"] == cr.DESTINATION_ON_AD
    fs = cre["asset_feed_spec"]
    assert len(fs["bodies"]) == 3 and len(fs["titles"]) == 3
    assert fs["link_urls"] == [{"website_url": LINK}]
    assert fs["call_to_actions"] == [{"type": fs["call_to_action_types"][0],
                                      "value": {"link": LINK, "lead_gen_form_id": FORM}}]
    assert "asset_customization_rules" not in fs and len(fs["images"]) == 1
    # Page (+ Instagram identity) only next to the feed; no link_data / video_data.
    assert set(cre["object_story_spec"]) <= {"page_id", wp.INSTAGRAM_KEY}
    assert cre["object_story_spec"]["page_id"] == PAGE
    # Enhancements and multi-advertiser ads stay OFF, everything PAUSED.
    assert cre["contextual_multi_ads"] == {"enroll_status": "OPT_OUT"}
    feats = cre["degrees_of_freedom_spec"]["creative_features_spec"]
    assert feats and all(v == {"enroll_status": "OPT_OUT"} for v in feats.values())
    assert all(p["status"] == (cr.PAUSED if path.endswith("/campaigns") else cr.CHILD_STATUS)
               for path, p in meta.posts if path.endswith(("/campaigns", "/adsets", "/ads")))


def test_a_video_form_ad_with_several_texts_goes_as_a_feed_too(tmp_path):
    meta = FormMeta()
    meta.next_id = 100000  # numeric-ID contract of asset-feed videos
    out, _ = _run(meta, _draft(tmp_path, texts=1, video=True))
    assert out.error == "" and out.readback["ok"], out.mismatches
    fs = _of(meta, "creative")["asset_feed_spec"]
    assert fs["ad_formats"] == ["SINGLE_VIDEO"] and len(fs["videos"]) == 1
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
    assert "call_to_actions" not in fs
    assert fs["link_urls"] == [{"website_url": "https://example.invalid/l"}]


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


def test_a_dynamic_form_ad_must_be_alone_in_its_ad_set(tmp_path):
    d = _instant(tmp_path, forms=(FORM, FORM2))
    d.ad_sets[0].ads[0].text_variants = [{"name": "T2", "body": "Body two", "headline": "Head two",
                                          "description": "Desc two"}]
    with pytest.raises(cr.PlanError, match="only one ad in such an ad set"):
        cr.plan_from_draft(d)


@pytest.mark.parametrize("field,key,value", [
    ("lead_gen_form_id", "creative.asset_feed_spec.call_to_actions",
     [{"type": "SIGN_UP", "value": {"link": LINK, "lead_gen_form_id": FORM2}}]),
    ("link", "creative.asset_feed_spec.link_urls", [{"website_url": "https://example.invalid/l"}]),
    ("feed_call_to_action", "creative.asset_feed_spec.call_to_actions",
     [{"type": "GET_QUOTE", "value": {"link": LINK, "lead_gen_form_id": FORM}}]),
    ("lead_gen_form_id", "creative.asset_feed_spec.call_to_actions", None),
], ids=["other_form", "site_link", "other_button", "no_button"])
def test_readback_catches_what_meta_returned_differently(tmp_path, field, key, value):
    meta = FormMeta()
    meta.mutate_ads[key] = value
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert field in {m["field"] for m in out.mismatches}, out.mismatches


def test_a_form_missing_from_the_page_is_a_readback_mismatch(tmp_path):
    meta = FormMeta()
    meta.drop_forms_after_creative = True
    out, _ = _run(meta, _draft(tmp_path, texts=1))
    assert "form_on_page" in {c["field"] for c in out.readback["mismatches"]}


def test_form_link_constant_matches_the_creator():
    assert wp.LEAD_FEED_LINK == cr.INSTANT_FORM_LINK


# ───────────────────────── policy: Python and page helper agree ─────────────────────────

def _feed(**over):
    spec = {"bodies": [{"text": "b1"}, {"text": "b2"}], "titles": [{"text": "t1"}],
            "images": [{"hash": "a" * 32}], "link_urls": [{"website_url": LINK}],
            "call_to_action_types": ["SIGN_UP"], "ad_formats": ["SINGLE_IMAGE"],
            "call_to_actions": [{"type": "SIGN_UP", "value": {"link": LINK, "lead_gen_form_id": FORM}}]}
    for k, v in over.items():
        if v is None:
            spec.pop(k, None)
        else:
            spec[k] = v
    return spec


def _cre(spec, story=None, **top):
    return {"name": "x", "object_story_spec": story or {"page_id": PAGE}, "asset_feed_spec": spec, **top}


def _btn(**value):
    return [{"type": "SIGN_UP", "value": value}]


TWO_FILES = [{"hash": "a" * 32, "adlabels": [{"name": "acl_main"}]},
             {"hash": "b" * 32, "adlabels": [{"name": "acl_vertical"}]}]
RULE = {"customization_spec": {"publisher_platforms": ["facebook"]}}

#: (name, params, on_ad, allowed): one table for BOTH lines (Python and page helper).
MATRIX = [
    ("ok_full", _cre(_feed()), True, True),
    ("ok_without_link", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=FORM))), True, True),
    ("ok_five_texts", _cre(_feed(bodies=[{"text": f"b{i}"} for i in range(5)])), True, True),
    ("ok_video", _cre(_feed(images=None, videos=[{"video_id": "123456789", "thumbnail_url": "https://x.invalid/t.jpg"}],
                            ad_formats=["SINGLE_VIDEO"])), True, True),
    ("ok_instagram", _cre(_feed(), story={"page_id": PAGE, "instagram_user_id": "1784140000001"}), True, True),
    # Not an on-ad set: the form (and its button key) is refused everywhere.
    ("no_on_ad", _cre(_feed()), False, False),
    ("no_on_ad_without_form", _cre(_feed(call_to_actions=None)), False, True),
    # Value checks of the form id and the button.
    ("form_not_digits", _cre(_feed(call_to_actions=_btn(lead_gen_form_id="abc123"))), True, False),
    ("form_too_short", _cre(_feed(call_to_actions=_btn(lead_gen_form_id="123"))), True, False),
    ("form_number", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=7000001))), True, False),
    ("form_empty", _cre(_feed(call_to_actions=_btn(lead_gen_form_id=""))), True, False),
    ("form_missing", _cre(_feed(call_to_actions=_btn(link=LINK))), True, False),
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
    ("site_link_on_form", _cre(_feed(link_urls=[{"website_url": "https://example.invalid/l"}])), True, False),
    # One file only, no rules on a form feed.
    ("two_files_rules", _cre(_feed(images=TWO_FILES, asset_customization_rules=[
        dict(RULE, image_label={"name": "acl_main"}), dict(RULE, image_label={"name": "acl_vertical"})],
                                   bodies=[{"text": "b1"}])), True, False),
    # The form id anywhere else stays refused, even next to a valid button.
    ("form_also_in_story", _cre(_feed(), story={"page_id": PAGE, "link_data": {
        "call_to_action": {"type": "SIGN_UP", "value": {"lead_gen_form_id": FORM}}}}), True, False),
    ("form_in_feed_top", _cre(_feed(lead_gen_form_id=FORM)), True, False),
    ("form_in_title_item", _cre(_feed(titles=[{"text": "t1", "lead_gen_form_id": FORM}])), True, False),
    ("form_in_second_button_value", _cre(_feed(call_to_actions=[
        {"type": "SIGN_UP", "value": {"lead_gen_form_id": FORM, "link": LINK, "x": {"lead_gen_form_id": FORM}}}])), True, False),
    ("form_top_level", {"name": "x", "lead_gen_form_id": FORM}, True, False),
    ("form_in_feed_of_a_non_creative_edge", None, True, False),
]
MATRIX = [m for m in MATRIX if m[1] is not None]
MATRIX_IDS = [m[0] for m in MATRIX]


@pytest.mark.parametrize("name,params,on_ad,allowed", MATRIX, ids=MATRIX_IDS)
def test_policy_python_matrix(name, params, on_ad, allowed):
    path = f"/{ACT}/adcreatives"
    if allowed:
        assert wp.check(ACT, "POST", path, params, on_ad=on_ad) == "adcreatives"
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", path, params, on_ad=on_ad)


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
    web = _cre(_feed(call_to_actions=None, link_urls=[{"website_url": "https://example.invalid/l"}],
                     call_to_action_types=["GET_OFFER"]))
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", web) == "adcreatives"
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", web, on_ad=False) == "adcreatives"


def test_page_policy_carries_the_new_values():
    pol = wp.policy_for_page(ACT)
    assert pol["asset_feed"]["form_link"] == LINK and pol["lead_form"]["feed_key"] == "call_to_actions"


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
    const spec = { method: 'POST', path: '/' + ACTV + '/adcreatives', params: m.params };
    if (m.on_ad) spec.on_ad = true;
    out[m.name] = await h.call(spec);
  }
  out.calls = calls.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_matrix() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_feed_form"
    matrix = [{"name": n, "params": p, "on_ad": o} for n, p, o, _a in MATRIX]
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


# ───────────────────────── Check plan ─────────────────────────

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
    assert _of(meta, "adset")["is_dynamic_creative"] is True


def test_no_body_at_all_publishes_without_a_primary_text(tmp_path):
    d = _texts_draft(tmp_path, body1="", variants=[])
    d.ad_sets[0].ads[0].ad_title = "Head"
    plan = cr.plan_from_draft(d)
    assert plan.ad_sets[0].ads[0].bodies == []
    meta = FormMeta()
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    assert _of(meta, "creative")  # created without a primary text


def test_the_dynamic_one_ad_per_ad_set_rule_still_holds_with_an_empty_text_1(tmp_path):
    d = _instant(tmp_path, forms=(FORM, FORM2))
    d.ad_sets[0].ads[0].ad_body = ""
    d.ad_sets[0].ads[0].text_variants = [{"name": "T2", "body": "Body two", "headline": "Head two",
                                          "description": ""}]
    with pytest.raises(cr.PlanError, match="only one ad in such an ad set"):
        cr.plan_from_draft(d)


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
