"""ACL-2114: наборы и объявления создаются ACTIVE под кампанией на паузе.

Кампания - всегда PAUSED и пишется первой. Дети - ACTIVE только после чтения, подтвердившего
PAUSED у кампании (`children_active` в политике). Найденные с прошлого запуска объекты не
трогаем. Живой Meta здесь нет: FakeMeta в памяти.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402


def _edges(meta):
    return [p.rsplit("/", 1)[1] for p, _ in meta.posts]


def _objs(meta, kind):
    return [o for o in meta.objects.values() if o["_kind"] == kind]


# ── параметры по уровням ──────────────────────────────────────────────────────────────

def test_params_campaign_paused_children_active(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == ""
    posted = {e: [p for path, p in meta.posts if path.endswith("/" + e)]
              for e in ("campaigns", "adsets", "ads")}
    assert [p["status"] for p in posted["campaigns"]] == ["PAUSED"]
    assert [p["status"] for p in posted["adsets"]] == ["ACTIVE"]
    assert [p["status"] for p in posted["ads"]] == ["ACTIVE", "ACTIVE"]
    # креатив статуса не несёт; ACTIVE нигде глубже верхнего ключа
    for path, params in meta.posts:
        if path.endswith(("/adcreatives", "/adimages", "/advideos")):
            assert "status" not in params and "ACTIVE" not in json.dumps(params)
    # каждая запись набора/объявления шла с флагом, кампания и прочее - без
    flags = dict(meta.children_active_posts)
    assert flags[f"/{ACT}/campaigns"] is False
    assert flags[f"/{ACT}/adsets"] is True and flags[f"/{ACT}/ads"] is True
    assert flags[f"/{ACT}/adcreatives"] is False


def test_campaign_params_are_paused_adset_params_active(tmp_path):
    meta = FakeMeta()
    from tests.test_acl1761_api_create import g
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=50)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None)
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    assert creator.campaign_params(plan)["status"] == "PAUSED"
    assert creator.adset_params(plan, plan.ad_sets[0], "1", {})["status"] == "ACTIVE"


# ── политика записи ───────────────────────────────────────────────────────────────────

ADSET = {"name": "s", "campaign_id": "1", "status": "ACTIVE"}
AD = {"name": "a", "adset_id": "2", "creative": {"creative_id": "3"}, "status": "ACTIVE"}


@pytest.mark.parametrize("edge,params", [("adsets", ADSET), ("ads", AD)])
def test_policy_active_child_needs_the_confirmed_paused_parent_flag(edge, params):
    with pytest.raises(wp.WriteRefused, match="PAUSED"):
        wp.check(ACT, "POST", f"/{ACT}/{edge}", params)
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/{edge}", params, children_active=False)
    assert wp.check(ACT, "POST", f"/{ACT}/{edge}", params, children_active=True) == edge


def test_policy_campaign_is_never_active_even_with_the_flag():
    camp = {"name": "c", "objective": "OUTCOME_LEADS", "status": "ACTIVE"}
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/campaigns", camp, children_active=True)


def test_policy_flag_opens_only_the_top_level_status_of_ad_sets_and_ads():
    # вложенные статусы - только PAUSED, как раньше
    nested = dict(ADSET, targeting={"status": "ACTIVE"})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adsets", nested, children_active=True)
    nested_ad = dict(AD, adlabels=[{"name": "x", "effective_status": "ACTIVE"}])
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/ads", nested_ad, children_active=True)
    # другие значения статуса и forbidden-ключи
    for bad in ("ARCHIVED", "DELETED", "active", ""):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/adsets", dict(ADSET, status=bad), children_active=True)
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adsets", dict(ADSET, effective_status="ACTIVE"), children_active=True)
    # креатив и загрузки статуса не получают
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", {"name": "c", "status": "ACTIVE"}, children_active=True)
    # PAUSED у детей по-прежнему можно
    assert wp.check(ACT, "POST", f"/{ACT}/adsets", dict(ADSET, status="PAUSED")) == "adsets"


def test_policy_object_updates_keep_their_own_narrow_chain():
    # правка существующего объекта (ACL-1800) не зависит от флага создания
    assert wp.check_object_update("POST", "/123456", {"status": "ACTIVE"}) == "status"
    with pytest.raises(wp.WriteRefused):
        wp.check_object_update("POST", f"/{ACT}/adsets", {"status": "ACTIVE"})


def test_page_helper_policy_mirrors_the_child_edges():
    pol = wp.policy_for_page(ACT)
    assert pol["child_edges"] == ["ads", "adsets"] and "campaigns" not in pol["child_edges"]
    assert pol["paused_required"] == ["ads", "adsets", "campaigns"]


# ── сверка ────────────────────────────────────────────────────────────────────────────

def test_readback_accepts_children_that_read_campaign_paused(tmp_path):
    meta = FakeMeta()
    # Meta читает у детей effective_status=CAMPAIGN_PAUSED, status остаётся ACTIVE
    meta.mutate_on_read[("adset", "effective_status")] = "CAMPAIGN_PAUSED"
    meta.mutate_ads["effective_status"] = "CAMPAIGN_PAUSED"
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and out.readback["ok"] is True, out.mismatches
    assert not out.mismatches
    statuses = [c for c in out.readback["checks"] if c["field"] == "status"]
    assert {c["object"].split()[0] for c in statuses} == {"campaign", "ad"}
    by_obj = {c["object"]: (c["expected"], c["actual"]) for c in statuses}
    assert by_obj["campaign"] == ("PAUSED", "PAUSED")
    assert all(v == ("ACTIVE", "ACTIVE") for k, v in by_obj.items() if k != "campaign")


def test_readback_flags_a_child_that_is_not_active(tmp_path):
    meta = FakeMeta()
    meta.mutate_on_read[("adset", "status")] = "PAUSED"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert any(m["field"] == "status" and m["expected"] == "ACTIVE" for m in out.mismatches)


# ── найденные объекты ────────────────────────────────────────────────────────────────

def test_reuse_does_not_write_any_status(tmp_path):
    meta = FakeMeta()
    first, _ = _run(meta, _draft(tmp_path, ads=2))
    assert first.error == ""
    n = len(meta.posts)
    second, client = _run(meta, _draft(tmp_path, ads=2))
    assert second.error == "" and second.created == {}
    assert client.writes == 0 and len(meta.posts) == n
    assert second.reused.get("adsets") and second.reused.get("ads")
    assert second.readback["ok"] is True


def test_reused_child_paused_by_an_older_run_is_left_alone_and_accepted(tmp_path):
    meta = FakeMeta()
    _run(meta, _draft(tmp_path, ads=1))
    adset = _objs(meta, "adset")[0]
    ad = _objs(meta, "ad")[0]
    adset["status"] = ad["status"] = "PAUSED"           # так создавали прошлые версии
    n = len(meta.posts)
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == "MX Leads 1" and out.created == {}
    assert out.readback["ok"] is True, out.mismatches
    assert client.writes == 0 and len(meta.posts) == n
    assert adset["status"] == "PAUSED" and ad["status"] == "PAUSED"
    # без записи не нужно и подтверждение кампании
    assert out.campaign_confirmed_paused is False


def test_partial_rerun_adds_only_missing_children_as_active(tmp_path):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, _draft(tmp_path, ads=2))
    assert first.stop_kind == "unknown"
    old_adset = _objs(meta, "adset")[0]
    second, _ = _run(meta, _draft(tmp_path, ads=2))
    assert second.error == "" and second.readback["ok"] is True
    assert old_adset["status"] == "ACTIVE"
    assert [o["status"] for o in _objs(meta, "ad")] == ["ACTIVE", "ACTIVE"]
    assert second.campaign_confirmed_paused is True      # перечитана до записи нового объявления


# ── кампания не подтверждена PAUSED ───────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["ACTIVE", "", "ARCHIVED"])
def test_campaign_not_confirmed_paused_stops_before_any_child_write(tmp_path, status):
    meta = FakeMeta()
    meta.mutate_on_read[("campaign", "status")] = status
    out, client = _run(meta, _draft(tmp_path, ads=2))
    assert out.stop_kind == "campaign_not_paused" and "not confirmed PAUSED" in out.error
    assert _edges(meta) == ["campaigns"]                  # ни набора, ни креатива, ни объявления
    assert _objs(meta, "adset") == [] and _objs(meta, "ad") == []
    assert out.created == {"campaign": out.campaign_id}


def test_reused_campaign_turned_on_by_a_person_blocks_new_children(tmp_path):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, _draft(tmp_path, ads=2))
    assert first.stop_kind == "unknown"
    before = list(meta.posts)
    # человек включил кампанию; найденная кампания с ACTIVE уходит под новое имя (ACL-1902),
    # а не получает новых ACTIVE-детей
    _objs(meta, "campaign")[0]["status"] = "ACTIVE"
    second, _ = _run(meta, _draft(tmp_path, ads=2))
    assert second.published_as == "MX Leads 1_2"
    assert _objs(meta, "campaign")[0]["status"] == "ACTIVE"
    assert not any(p.endswith(("/adsets", "/ads")) and params.get("campaign_id") == _objs(meta, "campaign")[0]["id"]
                   for p, params in meta.posts[len(before):])


# ── предпроверка, план и сообщения ────────────────────────────────────────────────────

def test_dry_run_plan_and_summary_show_the_new_statuses(tmp_path):
    meta = FakeMeta()
    from tests.test_acl1761_api_create import g
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None)
    out = creator.plan_only(cr.plan_from_draft(_draft(tmp_path, video=True, ads=2)))
    assert out.error == "" and meta.posts == []
    plan = out.plan
    assert plan["campaign"]["status"] == "PAUSED"
    assert plan["ad_sets"][0]["status"] == "ACTIVE"
    assert all(a["status"] == "ACTIVE" for a in plan["ad_sets"][0]["ads"])
    summary = plan["summary"]
    assert summary["status"] == "PAUSED" and summary["children_status"] == "ACTIVE"
    assert summary["status_note"] == ("Campaign PAUSED — ad sets and ads are active and start when "
                                      "you turn the campaign on")
    assert all(s["status"] == "ACTIVE" and all(a["status"] == "ACTIVE" for a in s["ads"])
               for s in summary["ad_sets"])


def test_step_messages_say_created_active_under_a_paused_campaign(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert "campaign: created PAUSED, tag kept" in out.steps
    assert any("created ACTIVE (campaign PAUSED)" in s for s in out.steps if s.startswith("ad set"))
    assert any(s == "ad 1: created ACTIVE (campaign PAUSED)" for s in out.steps)
    assert not any("nothing was activated" in s for s in out.steps)


def test_dashboard_texts_use_the_new_message():
    page = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    new = "ad sets and ads are active and start when you turn the campaign on"
    assert page.count(new) >= 2 and page.count("Campaign PAUSED") >= 3
    assert "nothing was activated" not in page and "nothing is activated" not in page
    assert "Status: PAUSED</span>" not in page


# ── вторая линия: помощник страницы (JS) зеркалит политику ───────────────────────────

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
const ACT = ACTV;
const SET = { name: 's', campaign_id: '1', status: 'ACTIVE' };
const AD = { name: 'a', adset_id: '2', creative: { creative_id: '3' }, status: 'ACTIVE' };
(async () => {
  const out = {};
  const call = (edge, params, extra) => h.call(Object.assign({ method: 'POST', path: '/' + ACT + '/' + edge, params }, extra || {}));
  out.set_no_flag = await call('adsets', SET);
  out.set_flag = await call('adsets', SET, { children_active: true });
  out.ad_flag = await call('ads', AD, { children_active: true });
  out.ad_no_flag = await call('ads', AD);
  out.camp_flag = await call('campaigns', { name: 'c', status: 'ACTIVE' }, { children_active: true });
  out.nested = await call('adsets', Object.assign({}, SET, { targeting: { status: 'ACTIVE' } }), { children_active: true });
  out.creative = await call('adcreatives', { name: 'c', status: 'ACTIVE' }, { children_active: true });
  out.flag_string = await call('adsets', SET, { children_active: 'true' });
  out.paused = await call('adsets', Object.assign({}, SET, { status: 'PAUSED' }));
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_page_helper_allows_active_children_only_with_the_flag():
    import shutil
    import subprocess
    import tempfile

    from src.fb_api import token_capture as tc

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_2114"
    script = (_NODE.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20)).replace("NAME", json.dumps(name))
              .replace("ACTV", json.dumps(ACT))
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
    sent = {"body": {"id": "5"}}
    assert out["set_flag"].get("body") == sent["body"] and out["ad_flag"].get("body") == sent["body"]
    assert out["paused"].get("body") == sent["body"]
    for key in ("set_no_flag", "ad_no_flag", "camp_flag", "nested", "creative", "flag_string"):
        assert out[key].get("error") and "body" not in out[key], (key, out[key])
