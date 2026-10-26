"""ACL-1896: Instagram-площадки без Instagram-аккаунта у Страницы - отказ в Check plan, до записи.

Живой прогон Doc120 (2026-10-14): Meta отказала объявлению (100/1772103 «Instagram Account Is
Missing»), когда кампания, набор и креатив уже были созданы. Теперь создатель читает
Instagram-идентичность Страницы (только чтения), при её отсутствии не пишет ничего, а при наличии
шлёт её в `object_story_spec.instagram_user_id` и читает обратно. Политика записи (Python и
помощник страницы) пускает ровно одно числовое поле.
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

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import instagram as ig  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402
from tests.test_acl1819_preflight import AuditMeta, _audit, _by  # noqa: E402

DOC120_RESULT = ROOT / "tests" / "fixtures" / "acl1896_doc120_publish_result.json"
IG_ID = "17841400000000001"
NO_IG_TEXT = ("This Facebook Page has no Instagram account, and the ad is set for Instagram placements. "
              "Connect an Instagram account to the Page, or choose Manual Facebook-only placements.")


def _creatives(meta):
    return [o for o in meta.objects.values() if o["_kind"] == "creative"]


def test_the_doc120_case_is_in_the_fixture():
    """Отказ Meta из живого прогона: тот самый код, подкод и текст; кампания и набор уже были созданы."""
    got = json.loads(DOC120_RESULT.read_text(encoding="utf-8"))
    assert "error_subcode=1772103" in got["error"] and "Instagram Account Is Missing" in got["error"]
    assert got["creative_id"] is None and got["campaign_id"] and got["adset_id"]


# ───────────────────────── Check plan и создание ─────────────────────────

def test_a_page_without_linked_instagram_plans_page_backed_preparation(tmp_path):
    # ACL-2221 owner-authorized successor: Check remains read-only, preparation is on Publish.
    from tests.test_acl2221_page_backed import dry
    meta = FakeMeta(instagram="none")
    out = dry(meta, _draft(tmp_path, ads=1))
    assert not out.error and meta.posts == [] and out.created == {}
    assert _by(out)["instagram_identity"]["status"] == "warning"


def test_check_plan_warns_about_preparation_and_still_writes_nothing(tmp_path):
    meta = AuditMeta(instagram="none")
    out = _audit(meta, _draft(tmp_path, ads=1))
    row = _by(out)["instagram_identity"]
    assert row["status"] == "warning" and "Facebook Page" in row["detail"]
    assert not pf.blocks(out.audit) and meta.posts == []


def test_a_failed_instagram_read_is_never_a_pass(tmp_path):
    meta = FakeMeta(instagram="error")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "plan" and "did not confirm an Instagram account" in out.error
    assert meta.posts == []
    audit = _by(_audit(AuditMeta(instagram="error"), _draft(tmp_path, ads=1)))["instagram_identity"]
    assert audit["status"] == "error" and "did not confirm" in audit["detail"]


def test_an_ad_account_list_alone_does_not_confirm_the_page(tmp_path):
    from tests.test_acl2221_page_backed import dry
    meta = FakeMeta(instagram="ad_account_only")
    out = dry(meta, _draft(tmp_path, ads=1))
    assert not out.error and not meta.posts
    assert _by(out)["instagram_identity"]["status"] == "warning"
    assert "17841499999999999" not in json.dumps(out.plan)


def test_a_page_with_an_instagram_account_creates_paused_and_reads_it_back(tmp_path):
    meta = FakeMeta(instagram="one")
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and out.readback["ok"] is True
    creatives = _creatives(meta)
    assert creatives and all(c["object_story_spec"]["instagram_user_id"] == IG_ID for c in creatives)
    # ACL-2114: the campaign is PAUSED; its ad sets and ads are ACTIVE under it.
    assert all(o.get("status") == "PAUSED" for o in meta.objects.values() if o["_kind"] == "campaign")
    fields = [c for c in out.readback["checks"] if c["field"] == "instagram_user_id"]
    assert len(fields) == 2


def test_the_readback_flags_a_different_instagram_account(tmp_path):
    meta = FakeMeta(instagram="one")
    meta.mutate_ads = {"creative.object_story_spec.instagram_user_id": "17841400000000999"}
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.readback["ok"] is False
    assert any("instagram_user_id" in str(m) for m in out.mismatches)


def test_the_identity_is_read_once_per_page_and_costs_at_most_two_reads(tmp_path):
    meta = FakeMeta(instagram="none")
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=60)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda s: None)
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    before = len(meta.gets)
    first = creator.instagram_identity(plan)
    second = creator.instagram_identity(plan)
    assert first is second and ig.READS_PER_PAGE == 2
    assert [p for p, _ in meta.gets[before:]] == [f"/{plan.page_id}", f"/{plan.page_id}/page_backed_instagram_accounts"]


def test_without_a_settled_page_the_identity_is_not_checked(tmp_path):
    """Страница не определена (на кабинете их нет) - строка Page не OK (ACL-2105: предупреждение), Instagram не читается."""
    d = _draft(tmp_path, ads=1)
    d.page_id = ""
    meta = AuditMeta(pages=(), instagram="none")
    out = _audit(meta, d)
    assert _by(out)["page_ok"]["status"] in ("warning", "error")
    row = _by(out)["instagram_identity"]
    assert row["status"] == "unknown" and "not settled" in row["detail"]


def test_the_rule_is_in_the_shared_table_and_the_json():
    rule = next(r for r in pf.RULES if r["id"] == "instagram_identity")
    assert rule["side"] == "server" and rule["severity"] == "error"
    assert pf.JSON_PATH.read_text(encoding="utf-8") == pf.export_json()
    assert any(r["id"] == "instagram_identity" for r in pf.load_json()["rules"])


# ───────────────────────── политика записи ─────────────────────────

def _creative(**story):
    return {"name": "x", "object_story_spec": {"page_id": "1", **story}}


def test_policy_allows_one_numeric_instagram_id_in_the_story():
    wp.check(ACT, "POST", f"/{ACT}/adcreatives", _creative(instagram_user_id=IG_ID))


@pytest.mark.parametrize("bad", ["abc", "", "123", "1784140000000000x", IG_ID + " ", 17841400000000001, None,
                                 [IG_ID], {"id": IG_ID}, True])
def test_policy_refuses_a_non_numeric_instagram_id(bad):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", _creative(instagram_user_id=bad))


@pytest.mark.parametrize("params", [
    {"name": "x", "instagram_user_id": IG_ID},                                         # не в object_story_spec
    {"name": "x", "object_story_spec": {"page_id": "1", "link_data": {"instagram_user_id": IG_ID}}},
    {"name": "x", "object_story_spec": {"page_id": "1", "instagram_actor_id": IG_ID}},   # старое поле
])
def test_policy_refuses_the_id_anywhere_else_and_the_old_field(params):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", params)


def test_policy_refuses_the_id_on_other_edges():
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adsets", {"name": "s", "status": "PAUSED", "instagram_user_id": IG_ID})


# ───────────────────────── помощник страницы (node) ─────────────────────────

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
  for (const [k, c] of Object.entries(CASES)) {
    out[k] = await h.call({ method: 'POST', path: c.path, params: c.params });
  }
  process.stdout.write(JSON.stringify(out));
})();
"""

_CASES = {
    "good": {"path": f"/{ACT}/adcreatives", "params": _creative(instagram_user_id=IG_ID)},
    "good_string": {"path": f"/{ACT}/adcreatives", "params": {
        "name": "x", "object_story_spec": json.dumps({"page_id": "1", "instagram_user_id": IG_ID})}},
    "letters": {"path": f"/{ACT}/adcreatives", "params": _creative(instagram_user_id="abc")},
    "number": {"path": f"/{ACT}/adcreatives", "params": _creative(instagram_user_id=17841400000000001)},
    "short": {"path": f"/{ACT}/adcreatives", "params": _creative(instagram_user_id="123")},
    "top_level": {"path": f"/{ACT}/adcreatives", "params": {"name": "x", "instagram_user_id": IG_ID}},
    "nested": {"path": f"/{ACT}/adcreatives", "params": {"name": "x", "object_story_spec": {
        "page_id": "1", "link_data": {"instagram_user_id": IG_ID}}}},
    "actor": {"path": f"/{ACT}/adcreatives", "params": _creative(instagram_actor_id=IG_ID)},
    "on_adset": {"path": f"/{ACT}/adsets", "params": {"name": "s", "status": "PAUSED", "instagram_user_id": IG_ID}},
}


def _node_results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = (_NODE.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20)).replace("NAME", json.dumps("__acl_ig"))
              .replace("CASES", "(" + json.dumps(_CASES) + ")")
              .replace("HELPER", json.dumps(tc.helper_source("__acl_ig", wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _python_accepts(case) -> bool:
    try:
        wp.check(ACT, "POST", case["path"], case["params"])
        return True
    except wp.WriteRefused:
        return False


def test_the_page_helper_and_python_policy_agree_on_every_instagram_case():
    out = _node_results()
    for key, case in _CASES.items():
        accepted_js = out[key].get("body") == {"id": "5"}
        assert accepted_js == _python_accepts(case), key
    assert out["good"].get("body") == {"id": "5"} and out["good_string"].get("body") == {"id": "5"}
    for key in ("letters", "number", "short", "top_level", "nested", "actor", "on_adset"):
        assert out[key].get("error") == "write_refused", key
