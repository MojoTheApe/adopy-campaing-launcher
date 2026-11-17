"""ACL-2056 (часть A): кнопка SEE_DETAILS на Instant Form и Instagram-идентичность на объявлениях с формой.

Offline: Graph - `FakeMeta`/`FormMeta` в памяти, страница помощника - Node. Meta не трогается.
SEE_DETAILS нет в документации Meta (значение с живых объявлений байеров), поэтому оно живёт в
ОТДЕЛЬНЫХ правилах реестра `acl2056.*` и только в `CTA_FORM`. Тесты 1-5 - про кнопку, 6-8 - про
Instagram: они должны проходить без изменений кода (идентичность уже доходит до форм-объявлений).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.campaign_bridge import meta_rules as mr  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402
from tests.test_acl1775_instant_forms import FORM, FORM2, FormMeta, _instant  # noqa: E402
from tests.test_acl1776_create_forms import FormsMeta, _saved, _tpl_draft  # noqa: E402
from tests.test_acl1819_preflight import _ad, _harness, _with  # noqa: E402

BTN = "SEE_DETAILS"
IG_ID = "17841400000000001"
NO_IG = "This Page has no Instagram identity."


@pytest.fixture(autouse=True)
def _library(tmp_path, monkeypatch):
    """Шаблоны форм (тест 7) - во временной машинной папке."""
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "acl"))


def _form_plan(tmp_path, cta):
    d = _instant(tmp_path, forms=(FORM,))
    for ad in d.ad_sets[0].ads:
        ad.cta = cta
    return d


def _website_draft(tmp_path, cta):
    d = _draft(tmp_path, ads=1)
    for ad in d.ad_sets[0].ads:
        ad.cta = cta
    return d


def _creator(meta):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=300)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None), client


def _story(fmt="link_data", cta=BTN, value=None):
    value = {"lead_gen_form_id": FORM} if value is None else value
    data = {"message": "m", "call_to_action": {"type": cta, "value": value}}
    if fmt == "link_data":
        data["link"] = cr.INSTANT_FORM_LINK
    return {"page_id": "111", fmt: data}


def _creative_params(story):
    return {"name": "x", "object_story_spec": story}


# ───────────────────────── 1. SEE_DETAILS проходит все проверки формы ─────────────────────────

def test_see_details_passes_the_plan_the_policy_and_the_real_rule_check_for_a_form_ad(tmp_path):
    plan = cr.plan_from_draft(_form_plan(tmp_path, BTN))
    assert plan.ad_sets[0].ads[0].cta == BTN
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", _creative_params(_story()), on_ad=True) == "adcreatives"
    # Реальный check_job с реестром, как у создателя: правило `cta.enum` - то, что легко упустить.
    creator, _ = _creator(FormMeta())
    jobs = creator.meta_rule_jobs(plan, [{"geo_locations": {"countries": ["MX"]}} for _ in plan.ad_sets])
    assert jobs and all(job["call_to_action"] == BTN for _w, job in jobs)
    for _where, job in jobs:
        assert mr.check_job(job, mr.load_rules()) == []


def test_the_page_helper_accepts_see_details_on_a_form_ad():
    out = _node_cases([{"name": "form", "edge": "adcreatives", "params": _creative_params(_story()), "on_ad": True},
                       {"name": "form_video", "edge": "adcreatives",
                        "params": _creative_params(_story("video_data")), "on_ad": True}])
    assert out == {"form": "sent", "form_video": "sent"}


def test_a_full_form_publish_carries_see_details_into_the_creative(tmp_path):
    meta = FormMeta()
    creator, _ = _creator(meta)
    out = creator.run(cr.plan_from_draft(_form_plan(tmp_path, BTN)))
    assert out.error == "" and out.readback["ok"] is True, out.mismatches
    creative, = [o for o in meta.objects.values() if o["_kind"] == "creative"]
    assert creative["object_story_spec"]["link_data"]["call_to_action"]["type"] == BTN


# ───────────────────────── 2. SEE_DETAILS отвергается для сайта ─────────────────────────

def test_see_details_is_refused_for_website_ads_in_every_layer(tmp_path):
    with pytest.raises(cr.PlanError, match=BTN):
        cr.plan_from_draft(_website_draft(tmp_path, BTN))
    # Политика с on_ad=False и страница помощника
    web = _creative_params(_story("link_data", value={"link": "https://x.invalid/"}))
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", web, on_ad=False)
    out = _node_cases([{"name": "web", "edge": "adcreatives", "params": web, "on_ad": False}])
    assert out == {"web": "refused"}
    # check_job: правило cta.enum отвергает значение вне формы
    job = {"objective": "OUTCOME_LEADS", "optimization_goal": "OFFSITE_CONVERSIONS",
           "destination_type": "WEBSITE", "call_to_action": BTN}
    found = mr.check_job(job, mr.load_rules())
    assert found and found[0].field == "call_to_action"
    # Лента сайта: call_to_action_types берётся из CTA_WEBSITE
    feed = {"bodies": [{"text": "b"}], "titles": [{"text": "t"}], "descriptions": [{"text": "d"}],
            "images": [{"hash": "a" * 32}], "link_urls": [{"website_url": "https://x.invalid/l"}],
            "call_to_action_types": [BTN], "ad_formats": ["SINGLE_IMAGE"]}
    creative = {"name": "n", "object_story_spec": {"page_id": "111"}, "asset_feed_spec": feed,
                "degrees_of_freedom_spec": cr.creative_features(),
                "contextual_multi_ads": dict(cr.MULTI_ADS_OPT_OUT), "adlabels": [{"name": "t"}]}
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", creative, on_ad=False)
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", creative, on_ad=True)


def test_a_form_draft_switched_to_website_keeps_the_button_and_is_refused(tmp_path):
    d = _form_plan(tmp_path, BTN)
    d.ad_sets[0].destination = "website"
    with pytest.raises(cr.PlanError, match=BTN):
        cr.plan_from_draft(d)


# ───────────────────────── 3. LEARN_MORE остаётся первой и по умолчанию ─────────────────────────

def test_learn_more_stays_first_and_the_default_form_creative_is_unchanged(tmp_path):
    assert wp.CTA_FORM[0] == "LEARN_MORE" and wp.CTA_FORM[-1] == BTN
    assert BTN not in wp.CTA_WEBSITE
    meta = FormMeta()
    creator, _ = _creator(meta)
    d = _instant(tmp_path, forms=(FORM, FORM2))
    out = creator.run(cr.plan_from_draft(d))
    assert out.error == ""
    for c in (o for o in meta.objects.values() if o["_kind"] == "creative"):
        assert c["object_story_spec"]["link_data"]["call_to_action"]["type"] == "LEARN_MORE"


# ───────────────────────── 4. документированные правила не тронуты ─────────────────────────

def test_the_documented_rules_do_not_hold_see_details_and_the_live_rules_hold_only_it():
    rules = {r["id"]: r for r in mr.load_rules().rules}
    assert BTN not in rules["cta.enum"]["allowed"]
    assert BTN not in rules["acl2125.cta.instant_form"]["allowed"]
    assert set(rules["acl2125.cta.instant_form"]["allowed"]) == {
        "APPLY_NOW", "DOWNLOAD", "GET_QUOTE", "LEARN_MORE", "SIGN_UP", "SUBSCRIBE"}
    live = [r for r in rules.values() if r["id"].startswith("acl2056.")]
    assert {r["kind"] for r in live} == {"cta_enum", "cta_by_objective"}
    for r in live:
        assert r["allowed"] == [BTN] and r["status"] == "ga"
        assert r["applies_to"] == {"objective": "OUTCOME_LEADS", "destination_type": "ON_AD",
                                   "optimization_goal": ["LEAD_GENERATION", "QUALITY_LEAD"]}
        assert "Not in Meta's docs" in r["note"]


# ───────────────────────── 5. данные выпадающего списка и cta_allowed на странице ─────────────────────────

def test_the_preflight_table_offers_see_details_for_forms_only():
    allowed = pf.export_rules()["consts"]["allowed_cta"]
    assert BTN in allowed["leads_instant_form"] and BTN not in allowed["leads"]
    assert allowed["leads_instant_form"][0] == "LEARN_MORE"
    assert pf.JSON_PATH.read_text(encoding="utf-8") == pf.export_json()


def test_cta_allowed_on_the_page_passes_see_details_for_a_form_and_errors_for_a_website():
    form_ads = [{**a, "form_id": "7", "cta": BTN} for a in _with()["ads"]]
    cases = {"form": _with(destination="instant_form", ads=form_ads),
             "web": _with(ads=_ad(0, cta=BTN))}
    out = _harness("--evaluate", cases)

    def verdict(name):
        return next(v for v in out[name] if v["rule"] == "cta_allowed")

    assert verdict("form")["status"] == "ok"
    assert verdict("web")["status"] == "error" and verdict("web")["ads"] == [1]
    # Python-линия даёт то же
    assert next(v for v in pf.evaluate_client(cases["form"]) if v["rule"] == "cta_allowed")["status"] == "ok"
    assert next(v for v in pf.evaluate_client(cases["web"]) if v["rule"] == "cta_allowed")["status"] == "error"


# ───────────────────────── 6. Instagram-идентичность на форм-объявлении ─────────────────────────

def _creatives(meta):
    return [o for o in meta.objects.values() if o["_kind"] == "creative"]


@pytest.mark.parametrize("video", [False, True], ids=["link", "video"])
def test_a_form_ad_carries_the_page_instagram_identity_and_the_readback_checks_it(tmp_path, video):
    meta = FormMeta(instagram="one")
    creator, _ = _creator(meta)
    out = creator.run(cr.plan_from_draft(_instant(tmp_path, forms=(FORM,), video=video)))
    assert out.error == "" and out.readback["ok"] is True, out.mismatches
    cre, = _creatives(meta)
    assert cre["object_story_spec"]["instagram_user_id"] == IG_ID
    assert ("video_data" in cre["object_story_spec"]) is video
    assert [c for c in out.readback["checks"] if c["field"] == "instagram_user_id"]


def test_the_readback_flags_a_different_identity_on_a_form_ad(tmp_path):
    meta = FormMeta(instagram="one")
    meta.mutate_ads = {"creative.object_story_spec.instagram_user_id": "17841400000000999"}
    creator, _ = _creator(meta)
    out = creator.run(cr.plan_from_draft(_instant(tmp_path, forms=(FORM,))))
    assert out.readback["ok"] is False
    assert any("instagram_user_id" in str(m) for m in out.mismatches)


# ───────────────────────── 7. то же для формы, созданной при публикации ─────────────────────────

def test_a_form_created_at_publish_also_gets_the_identity(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta(instagram="one")
    creator, _ = _creator(meta)
    out = creator.run(cr.plan_from_draft(_tpl_draft(tmp_path, [tid])))
    assert out.error == "", out.error
    assert len(meta.form_posts) == 1
    cre, = _creatives(meta)
    assert cre["object_story_spec"]["instagram_user_id"] == IG_ID
    assert out.readback["ok"] is True, out.mismatches


# ───────────────────────── 8. Страница без идентичности: отказ до записи ─────────────────────────

@pytest.mark.parametrize("mode", ["none", "error"])
def test_a_form_ad_on_a_page_without_identity_is_refused_before_any_write(tmp_path, mode):
    meta = FormMeta(instagram=mode)
    creator, _ = _creator(meta)
    out = creator.run(cr.plan_from_draft(_instant(tmp_path, forms=(FORM,))))
    assert out.stop_kind == "plan" and out.error
    assert (NO_IG in out.error) if mode == "none" else ("did not confirm an Instagram account" in out.error)
    assert meta.posts == [] and out.created == {}


# ───────────────────────── помощник страницы (Node) ─────────────────────────

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
(async () => {
  const out = {};
  for (const c of CASES) {
    const r = await h.call({ method: 'POST', path: '/' + ACTV + '/' + c.edge, params: c.params, on_ad: c.on_ad === true });
    out[c.name] = r.error === 'write_refused' ? 'refused' : (r.body ? 'sent' : JSON.stringify(r));
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_cases(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_2056"
    script = (_NODE.replace("ACTV", json.dumps(ACT)).replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20))
              .replace("NAME", json.dumps(name)).replace("CASES", json.dumps(cases))
              .replace("HELPER", json.dumps(tc.helper_source(name, wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)
