"""ACL-1761: паритет с роботом — площадки вручную и Advantage+ creative выключен.

Живой тест 9095: план и созданное ad set говорили «automatic (Advantage+
placements)», а робот ставит площадки руками. Список площадок зеркалит
`src/fb_ads_rpa/meta_dom.py` (`_SUBITEMS_ON`); тест сверяет зеркало с
исходником робота по тексту (без импорта: это код RPA с Playwright).
Улучшения Advantage+ creative выключены (как `disable_text_variations`),
и политика записи разрешает только OPT_OUT — в Python и в помощнике страницы.
"""
from __future__ import annotations

import ast
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
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402


def _rpa_constant(name: str):
    """Значение константы робота из исходника meta_dom.py (ast, без импорта)."""
    tree = ast.parse((ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in meta_dom.py")


def test_the_placement_mirror_matches_the_robots_on_list():
    on = set(_rpa_constant("_SUBITEMS_ON"))
    ours = set(cr.POSITION_LABELS.values())
    assert ours == on
    pairs = {("facebook", p) for p in cr.FACEBOOK_POSITIONS} | {("instagram", p) for p in cr.INSTAGRAM_POSITIONS}
    assert pairs == set(cr.POSITION_LABELS)
    assert set(cr.PUBLISHER_PLATFORMS) == {"facebook", "instagram"}
    cats = _rpa_constant("_PLACEMENT_CATEGORIES")
    assert cats["Search results"] is False and cats["Apps and sites"] is False
    assert "search" not in cr.FACEBOOK_POSITIONS and "audience_network" not in cr.PUBLISHER_PLATFORMS
    for off in ("facebook_reels_overlay", "marketplace", "right_hand_column", "profile_feed"):
        assert off not in cr.FACEBOOK_POSITIONS
    for off in ("explore", "explore_home", "profile_feed", "ig_search", "profile_reels"):
        assert off not in cr.INSTAGRAM_POSITIONS


def _adset(meta):
    return next(o for o in meta.objects.values() if o["_kind"] == "adset")


def test_the_ad_set_gets_the_robots_manual_placements(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.readback["ok"] is True
    t = _adset(meta)["targeting"]
    assert t["publisher_platforms"] == ["facebook", "instagram"]
    assert t["facebook_positions"] == ["feed", "story", "facebook_reels", "instream_video"]
    assert t["instagram_positions"] == ["stream", "story", "reels"]
    assert t["device_platforms"] == ["mobile", "desktop"] and "user_os" not in t
    fields = {c["field"] for c in out.readback["checks"]}
    assert {"publisher_platforms", "facebook_positions", "instagram_positions", "devices"} <= fields


def test_ios_keeps_the_manual_placements_on_mobile_only(tmp_path):
    meta = FakeMeta()
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].os = "iOS"
    out, _ = _run(meta, d)
    t = _adset(meta)["targeting"]
    assert out.readback["ok"] is True
    assert t["user_os"] == ["iOS"] and t["device_platforms"] == ["mobile"]
    assert t["facebook_positions"] == list(cr.FACEBOOK_POSITIONS)


@pytest.mark.parametrize("key,value,field", [
    ("targeting.facebook_positions", ["feed", "marketplace"], "facebook_positions"),
    ("targeting.instagram_positions", ["stream", "explore"], "instagram_positions"),
    ("targeting.publisher_platforms", ["facebook", "instagram", "audience_network"], "publisher_platforms"),
    ("targeting.device_platforms", ["mobile"], "devices"),
])
def test_readback_flags_placements_meta_changed(tmp_path, key, value, field):
    meta = FakeMeta()
    meta.mutate_on_read[("adset", key)] = value
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert field in {m["field"] for m in out.mismatches}


def test_the_creative_opts_out_of_advantage_plus_enhancements(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    cre = next(o for o in meta.objects.values() if o["_kind"] == "creative")
    assert cre["degrees_of_freedom_spec"] == {
        "creative_features_spec": {"text_optimizations": {"enroll_status": "OPT_OUT"}}}
    assert any(c["field"] == "enhancement text_optimizations" and c["ok"] for c in out.readback["checks"])


@pytest.mark.parametrize("value", ["OPT_IN", None])
def test_readback_flags_an_enhancement_meta_turned_on(tmp_path, value):
    meta = FakeMeta()
    meta.mutate_ads["creative.degrees_of_freedom_spec.creative_features_spec.text_optimizations.enroll_status"] = value
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert "enhancement text_optimizations" in {m["field"] for m in out.mismatches}


def _creative(enroll):
    return {"name": "x", "object_story_spec": {"page_id": "1"},
            "degrees_of_freedom_spec": {"creative_features_spec": {"text_optimizations": {"enroll_status": enroll}}}}


def test_policy_allows_only_opt_out_for_enhancements():
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", _creative("OPT_OUT")) == "adcreatives"
    for bad in ("OPT_IN", "PAUSED", "ACTIVE", ""):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/adcreatives", _creative(bad))
    # «enroll_status» не открывает лазейку для статуса показа
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/ads", {"name": "a", "status": "OPT_OUT",
                                              "creative": {"creative_id": "1"}})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", {**_creative("OPT_OUT"),
                                                      "degrees_of_freedom_spec": "not json"})


#: Ревью R5-M1: всё, кроме «выключить функцию из списка», — отказ (в обоих слоях).
BAD_DOF = {
    "dof_type": {"degrees_of_freedom_type": "USER_ENROLLED_AUTOFLOW"},
    "dof_type_and_spec": {"degrees_of_freedom_type": "USER_ENROLLED_AUTOFLOW",
                          "creative_features_spec": {"text_optimizations": {"enroll_status": "OPT_OUT"}}},
    "no_enroll": {"creative_features_spec": {"text_optimizations": {}}},
    "unknown_feature": {"creative_features_spec": {"image_touchups": {"enroll_status": "OPT_OUT"}}},
    "extra_item_key": {"creative_features_spec": {"text_optimizations": {"enroll_status": "OPT_OUT",
                                                                         "action_metadata": {}}}},
    "empty_spec": {"creative_features_spec": {}},
    "spec_list": {"creative_features_spec": [{"text_optimizations": {"enroll_status": "OPT_OUT"}}]},
    "dof_list": [{"creative_features_spec": {"text_optimizations": {"enroll_status": "OPT_OUT"}}}],
}
BAD_DOF_WHY = {k: "dof" for k in BAD_DOF}
#: Объявление — только ссылка на готовый креатив: встроенный креатив провёз бы своё.
BAD_AD_CREATIVE = {
    "ad_inline_dof": {"creative_id": "7", "degrees_of_freedom_spec": {
        "degrees_of_freedom_type": "USER_ENROLLED_AUTOFLOW"}},
    "ad_inline_story": {"object_story_spec": {"page_id": "1"}},
    "ad_inline_features": {"creative_id": "7", "degrees_of_freedom_spec": {
        "creative_features_spec": {"image_touchups": {}}}},
    "ad_creative_list": [{"creative_id": "7"}],
}
BAD_DOF_WHY.update({k: "creative" for k in BAD_AD_CREATIVE})


@pytest.mark.parametrize("name", sorted(BAD_DOF))
@pytest.mark.parametrize("as_string", [False, True])
def test_policy_refuses_any_dof_other_than_opting_listed_features_out(name, as_string):
    spec = BAD_DOF[name]
    params = {"name": "x", "object_story_spec": {"page_id": "1"},
              "degrees_of_freedom_spec": json.dumps(spec) if as_string else spec}
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", params)


@pytest.mark.parametrize("name", sorted(BAD_AD_CREATIVE))
@pytest.mark.parametrize("as_string", [False, True])
def test_policy_an_ad_only_references_a_creative_by_id(name, as_string):
    creative = BAD_AD_CREATIVE[name]
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/ads", {"name": "a", "status": "PAUSED",
                                              "creative": json.dumps(creative) if as_string else creative})
    assert wp.check(ACT, "POST", f"/{ACT}/ads", {"name": "a", "status": "PAUSED",
                                                 "creative": {"creative_id": "7"}}) == "ads"


_VALID_DOF = {"creative_features_spec": {"text_optimizations": {"enroll_status": "OPT_OUT"}}}


@pytest.mark.parametrize("story", [
    {"page_id": "1", "degrees_of_freedom_spec": _VALID_DOF},
    {"page_id": "1", "link_data": {"degrees_of_freedom_spec": _VALID_DOF}},
    {"page_id": "1", "link_data": {"child": [{"degrees_of_freedom_spec": _VALID_DOF}]}},
])
def test_policy_refuses_degrees_of_freedom_spec_below_the_top(story):
    with pytest.raises(wp.WriteRefused, match="only allowed at the top"):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", {"name": "x", "object_story_spec": story})


@pytest.mark.parametrize("edge,params", [
    ("adcreatives", {"name": "x", "object_story_spec": {"page_id": "1", "link_data": {"enroll_status": "OPT_IN"}}}),
    ("adcreatives", {"name": "x", "object_story_spec": '{"page_id": "1", "enroll_status": "OPT_IN"}'}),
    ("adsets", {"name": "s", "status": "PAUSED", "targeting": {"enroll_status": "OPT_IN"}}),
    ("campaigns", {"name": "c", "status": "PAUSED", "adlabels": [{"name": "l", "enroll_status": "OPT_IN"}]}),
])
def test_policy_refuses_opt_in_outside_degrees_of_freedom_spec(edge, params):
    with pytest.raises(wp.WriteRefused, match="OPT_OUT"):
        wp.check(ACT, "POST", f"/{ACT}/{edge}", params)


def test_the_creator_opts_out_exactly_the_policy_features():
    assert set(cr.CREATIVE_FEATURES_OPT_OUT) == set(wp.DOF_FEATURES)
    wp.check(ACT, "POST", f"/{ACT}/adcreatives", {"name": "x", "object_story_spec": {"page_id": "1"},
                                                  "degrees_of_freedom_spec": cr.creative_features()})


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
const creative = (v) => ({ name: 'x', object_story_spec: { page_id: '1' },
  degrees_of_freedom_spec: { creative_features_spec: { text_optimizations: { enroll_status: v } } } });
(async () => {
  const out = {};
  out.opt_out = await h.call({ method: 'POST', path: PATH, params: creative('OPT_OUT') });
  out.opt_in = await h.call({ method: 'POST', path: PATH, params: creative('OPT_IN') });
  out.as_string = await h.call({ method: 'POST', path: PATH,
    params: Object.assign(creative('OPT_OUT'), { degrees_of_freedom_spec: JSON.stringify(creative('OPT_IN').degrees_of_freedom_spec) }) });
  for (const [k, spec] of Object.entries(BAD_DOF)) {
    out[k] = await h.call({ method: 'POST', path: PATH, params: { name: 'x', object_story_spec: { page_id: '1' },
                                                                   degrees_of_freedom_spec: spec } });
  }
  const ADS = PATH.replace('/adcreatives', '/ads');
  for (const [k, creative] of Object.entries(BAD_AD_CREATIVE)) {
    out[k] = await h.call({ method: 'POST', path: ADS, params: { name: 'a', status: 'PAUSED', creative: creative } });
  }
  out.ad_ref = await h.call({ method: 'POST', path: ADS,
                              params: { name: 'a', status: 'PAUSED', creative: { creative_id: '7' } } });
  // Ревью R6: вложенный degrees_of_freedom_spec и OPT_IN вне него — отказ.
  const VALID = { creative_features_spec: { text_optimizations: { enroll_status: 'OPT_OUT' } } };
  out.nested_dof = await h.call({ method: 'POST', path: PATH,
    params: { name: 'x', object_story_spec: { page_id: '1', degrees_of_freedom_spec: VALID } } });
  out.nested_dof_list = await h.call({ method: 'POST', path: PATH,
    params: { name: 'x', object_story_spec: { page_id: '1', link_data: { child: [{ degrees_of_freedom_spec: VALID }] } } } });
  out.opt_in_outside = await h.call({ method: 'POST', path: PATH,
    params: { name: 'x', object_story_spec: { page_id: '1', link_data: { enroll_status: 'OPT_IN' } } } });
  out.opt_in_targeting = await h.call({ method: 'POST', path: PATH.replace('/adcreatives', '/adsets'),
    params: { name: 's', status: 'PAUSED', targeting: { enroll_status: 'OPT_IN' } } });
  out.calls = calls.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_page_helper_allows_only_opt_out_for_enhancements():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_parity"
    script = (_NODE.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20)).replace("NAME", json.dumps(name))
              .replace("PATH", json.dumps(f"/{ACT}/adcreatives"))
              .replace("BAD_DOF", "(" + json.dumps(BAD_DOF) + ")")
              .replace("BAD_AD_CREATIVE", "(" + json.dumps(BAD_AD_CREATIVE) + ")")
              .replace("HELPER", json.dumps(tc.helper_source(name, wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["opt_out"].get("body") == {"id": "5"}
    assert out["opt_in"] == {"error": "write_refused", "why": "dof"}
    assert out["as_string"] == {"error": "write_refused", "why": "dof"}
    for k, why in BAD_DOF_WHY.items():
        assert out[k] == {"error": "write_refused", "why": why}, k
    assert out["ad_ref"].get("body") == {"id": "5"}
    for k in ("nested_dof", "nested_dof_list", "opt_in_outside", "opt_in_targeting"):
        assert out[k] == {"error": "write_refused", "why": "status"}, k
    assert out["calls"] == 2
