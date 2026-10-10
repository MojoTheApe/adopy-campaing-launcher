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
    assert out["opt_in"] == {"error": "write_refused", "why": "status"}
    assert out["as_string"] == {"error": "write_refused", "why": "status"}
    assert out["calls"] == 1
