"""ACL-1807: билдер предлагает только цели/CTA/destination, которые Meta принимает.

Данные — из реестра `config/meta_rules/` (ACL-1781), только статус `ga`. Проверяется:
функция `capabilities`, ручка `/api/meta-rules/capabilities`, страница (cbMetaCaps в Node
на живом коде шаблона) и что причина на странице совпадает по тексту с серверной
проверкой `check_job` (она остаётся последним барьером).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.campaign_bridge import meta_rules  # noqa: E402

SKIP = ("required_field", "text_limit")  # выбор в форме не несёт обязательных полей задания
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _rule(rid, kind, allowed, status="ga", applies=None):
    return {"id": rid, "kind": kind, "status": status, "allowed": allowed, "applies_to": applies or {},
            "source_url": "https://developers.facebook.com/x", "checked_date": "2026-10-11"}


def _ruleset(rules):
    meta = {"api_version_current": "v26.0", "checked_date": "2026-10-11", "sources": [],
            "api_versions": [{"version": "v26.0", "released": "2026-01-01", "end_of_support": None}]}
    return meta_rules.RuleSet(meta=meta, rules=tuple(rules))


@pytest.fixture
def small():
    return _ruleset([
        _rule("o", "objective", ["OUTCOME_LEADS", "OUTCOME_TRAFFIC"]),
        _rule("o.old", "objective", ["LEAD_GENERATION"], status="deprecated"),
        _rule("g.leads", "optimization_goal_by_objective", ["LEAD_GENERATION", "LINK_CLICKS"],
              applies={"objective": "OUTCOME_LEADS"}),
        _rule("g.beta", "optimization_goal_by_objective", ["VALUE"], status="beta",
              applies={"objective": "OUTCOME_LEADS"}),
        _rule("g.enum", "optimization_goal_enum", ["LEAD_GENERATION", "LINK_CLICKS", "REACH", "VALUE"]),
        _rule("b.lg", "billing_event_by_goal", ["IMPRESSIONS"], applies={"optimization_goal": "LEAD_GENERATION"}),
        _rule("b.enum", "billing_event_enum", ["IMPRESSIONS", "LINK_CLICKS"]),
        _rule("c.enum", "cta_enum", ["LEARN_MORE", "SIGN_UP"]),
        _rule("d.enum", "destination_type_enum", ["WEBSITE"]),
    ])


def test_capabilities_only_ga_values(small):
    caps = meta_rules.capabilities(small)
    assert set(caps["objectives"]) == {"OUTCOME_LEADS", "OUTCOME_TRAFFIC"}
    leads = caps["objectives"]["OUTCOME_LEADS"]
    assert list(leads["goals"]) == ["LEAD_GENERATION", "LINK_CLICKS"]  # VALUE (beta) не предлагается
    assert leads["goals"]["LEAD_GENERATION"] == ["IMPRESSIONS"]
    assert leads["goals"]["LINK_CLICKS"] == ["IMPRESSIONS", "LINK_CLICKS"]  # нет своего правила -> общий enum
    assert leads["ctas"] == ["LEARN_MORE", "SIGN_UP"]
    assert leads["restricted_goals"] is True and leads["restricted_ctas"] is False


def test_objective_without_own_goal_rule_gets_enum(small):
    traffic = meta_rules.capabilities(small)["objectives"]["OUTCOME_TRAFFIC"]
    assert list(traffic["goals"]) == ["LEAD_GENERATION", "LINK_CLICKS", "REACH", "VALUE"]
    assert traffic["restricted_goals"] is False


def test_builder_destinations_follow_registry(small):
    caps = meta_rules.capabilities(small)
    assert caps["builder_destinations"]["website"]["allowed"] is True
    assert caps["builder_destinations"]["instant_form"] == {"destination_type": "ON_AD", "allowed": False}


def test_real_registry_leads_goals():
    leads = meta_rules.capabilities()["objectives"]["OUTCOME_LEADS"]
    assert "LEAD_GENERATION" in leads["goals"]
    assert "REACH" not in leads["goals"]
    assert all(c in leads["ctas"] for c in ("LEARN_MORE", "SIGN_UP"))


def test_every_offered_combination_passes_check_job():
    """Что билдер предлагает, `check_job` не отклоняет (один источник правды)."""
    rs = meta_rules.load_rules()
    caps = meta_rules.capabilities(rs)
    for obj, entry in caps["objectives"].items():
        for goal, events in entry["goals"].items():
            for ev in events:
                job = {"objective": obj, "optimization_goal": goal, "billing_event": ev}
                assert meta_rules.check_job(job, rs, skip_kinds=SKIP) == [], (obj, goal, ev)
        for cta in entry["ctas"]:
            assert meta_rules.check_job({"objective": obj, "call_to_action": cta}, rs, skip_kinds=SKIP) == []


def test_endpoint_returns_capabilities():
    from src.web import app as web

    resp = asyncio.run(web.api_meta_rules_capabilities())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert "OUTCOME_LEADS" in body["objectives"] and body["api_version"]


def test_endpoint_503_when_registry_unreadable(monkeypatch):
    from src.web import app as web

    def boom(*a, **k):
        raise meta_rules.MetaRulesError("cannot read")

    monkeypatch.setattr(meta_rules, "capabilities", boom)
    resp = asyncio.run(web.api_meta_rules_capabilities())
    assert resp.status_code == 503
    assert web._meta_capabilities() == {}


def test_page_embeds_capabilities_global():
    page = TPL.read_text(encoding="utf-8")
    assert "window.CB_META_CAPS" in page and "meta_caps_json" in page


# ── страница (Node) ─────────────────────────────────────────────────────

def _run(tmp_path, caps, cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    cp = tmp_path / "cases.json"
    cp.write_text(json.dumps(cases), encoding="utf-8")
    cap_arg = "-"
    if caps is not None:
        pp = tmp_path / "caps.json"
        pp.write_text(json.dumps(caps), encoding="utf-8")
        cap_arg = str(pp)
    r = subprocess.run([node, str(ROOT / "tests" / "meta_caps_harness.cjs"), str(TPL), cap_arg, str(cp)],
                       capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_page_limits_goal_and_cta_by_objective(tmp_path):
    caps = meta_rules.capabilities()
    out = _run(tmp_path, caps, {
        "goals": {"fn": "options", "args": ["optimization_goal", {"objective": "Leads"}]},
        "ctas": {"fn": "options", "args": ["call_to_action", {"objective": "leads"}]},
        "billing": {"fn": "options", "args": ["billing_event", {"objective": "leads", "optimization_goal": "LEAD_GENERATION"}]},
        "none": {"fn": "options", "args": ["optimization_goal", {"objective": "unknown label"}]},
    })
    assert out["goals"] == list(caps["objectives"]["OUTCOME_LEADS"]["goals"])
    assert "REACH" not in out["goals"]
    assert out["ctas"] == caps["objectives"]["OUTCOME_LEADS"]["ctas"]
    assert out["billing"] == ["IMPRESSIONS"]
    assert out["none"] is None  # неизвестное не режем


def test_page_reason_equals_server_reason(tmp_path):
    rs = meta_rules.load_rules()
    sel = {"objective": "OUTCOME_LEADS", "optimization_goal": "REACH"}
    server = meta_rules.check_job(sel, rs, skip_kinds=SKIP)
    assert len(server) == 1
    out = _run(tmp_path, meta_rules.capabilities(rs), {"v": {"fn": "violations", "args": [sel]}})
    assert [x["message"] for x in out["v"]] == [v.message_en for v in server]


def test_page_blocks_forbidden_cta_and_destination(tmp_path):
    small_caps = meta_rules.capabilities(_ruleset([
        _rule("o", "objective", ["OUTCOME_LEADS"]),
        _rule("c", "cta_by_objective", ["LEARN_MORE"], applies={"objective": "OUTCOME_LEADS"}),
        _rule("d", "destination_type_enum", ["WEBSITE"]),
    ]))
    inp = {"destination": "instant_form", "objective": "leads", "ads": [{"cta": "LEARN_MORE"}, {"cta": "SHOP_NOW"}]}
    out = _run(tmp_path, small_caps, {
        "pf": {"fn": "preflight", "args": [inp]},
        "okdest": {"fn": "destinationAllowed", "args": ["website"]},
        "baddest": {"fn": "destinationAllowed", "args": ["instant_form"]},
    })
    assert out["pf"]["blocks"] is True
    details = [x["detail"] for x in out["pf"]["metaLines"]]
    assert "Call to action 'SHOP_NOW' is not an accepted Meta value for this campaign." in details
    assert "Destination type 'ON_AD' is not an accepted Meta value for this campaign." in details
    assert [x["ads"] for x in out["pf"]["metaLines"] if x["detail"].startswith("Call")] == [[2]]
    assert out["okdest"] is True and out["baddest"] is False


def test_page_valid_selection_adds_no_line(tmp_path):
    out = _run(tmp_path, meta_rules.capabilities(), {
        "pf": {"fn": "preflight", "args": [{"destination": "website", "objective": "leads", "ads": [{"cta": "LEARN_MORE"}]}]},
    })
    assert out["pf"]["metaLines"] == []


def test_page_without_capabilities_restricts_nothing(tmp_path):
    out = _run(tmp_path, None, {
        "v": {"fn": "violations", "args": [{"objective": "leads", "optimization_goal": "REACH", "call_to_action": "NOPE"}]},
        "d": {"fn": "destinationAllowed", "args": ["instant_form"]},
    })
    assert out["v"] == [] and out["d"] is True
