"""ACL-1805: реестр правил Meta до первой записи API-пути и понятные ошибки Meta в дашборде.

Graph - `FakeMeta` в памяти (как в test_acl1761_api_create): ни Meta, ни Multilogin.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.web import publish_api as pa  # noqa: E402
from test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402


class RefusingMeta(FakeMeta):
    """«Meta» отказывает на записи кампании заданным телом ошибки."""

    def __init__(self, error: dict):
        super().__init__()
        self.error = error

    def write(self, path, params, files=None, filename="", on_ad=False):
        if path.rsplit("/", 1)[1] == "campaigns":
            self.posts.append((path, dict(params)))
            return {"status": 400, "body": {"error": self.error}}
        return super().write(path, params, files, filename, on_ad)


def test_clean_spec_passes_the_registry(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path))
    assert out.error == "" and out.problems == []
    assert "preflight: Meta rules registry" in out.steps


@pytest.mark.parametrize("name,bad", [("OPTIMIZATION_GOAL", "NOT_A_GOAL"), ("BILLING_EVENT", "NOT_AN_EVENT"),
                                      ("CTA_TYPE", "NOT_A_CTA"), ("OBJECTIVE", "NOT_AN_OBJECTIVE")])
def test_violating_spec_is_stopped_before_any_post(tmp_path, monkeypatch, name, bad):
    monkeypatch.setattr(cr, name, bad)
    if name == "CTA_TYPE":
        # Кнопки проверяет и список создателя, и политика записи: пускаем «чужую» кнопку через оба,
        # чтобы остановка шла именно реестром правил Meta (ACL-1805), а не списком кнопок (ACL-1767).
        from src.fb_api import write_policy as wp
        monkeypatch.setattr(cr, "CTA_WEBSITE", tuple(cr.CTA_WEBSITE) + (bad,))
        monkeypatch.setattr(wp, "CTA_WEBSITE", tuple(wp.CTA_WEBSITE) + (bad,))
    meta = FakeMeta()
    out, client = _run(meta, _draft(tmp_path))
    assert meta.posts == [] and client.writes == 0 and out.campaign_id == ""
    assert out.stop_kind == "plan" and out.error.startswith("Meta rules:") and bad in out.error
    assert out.error.endswith("Nothing more was created.")
    assert out.problems and all(p["severity"] == "blocking" and p["action"] for p in out.problems)


def test_dry_run_reports_the_same_violation_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "OPTIMIZATION_GOAL", "NOT_A_GOAL")
    meta = FakeMeta()
    from src.fb_api import graph as _g
    client = _g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None)
    out = creator.plan_only(cr.plan_from_draft(_draft(tmp_path)))
    assert meta.posts == [] and out.stop_kind == "plan" and out.problems
    res = pa.dry_run_result(out)
    assert res["problems"] == out.problems and res["success"] is False


def test_registry_failure_fails_open_with_a_log(tmp_path, monkeypatch, caplog):
    from src.campaign_bridge import meta_rules

    def boom(*a, **k):
        raise meta_rules.MetaRulesError("registry missing")

    monkeypatch.setattr(meta_rules, "load_rules", boom)
    meta = FakeMeta()
    with caplog.at_level("ERROR"):
        out, _ = _run(meta, _draft(tmp_path))
    assert out.error == "" and meta.posts
    assert "Meta rules check failed" in caplog.text


def test_checked_values_are_the_exact_request_values(tmp_path):
    meta = FakeMeta()
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda s: None)
    plan = cr.plan_from_draft(_draft(tmp_path, ads=2))
    targetings = [cr.targeting_for(plan, s, creator.resolver) for s in plan.ad_sets]
    jobs = creator.meta_rule_jobs(plan, targetings)
    assert len(jobs) == 2
    for _, job in jobs:
        assert job["objective"] == cr.OBJECTIVE and job["optimization_goal"] == cr.OPTIMIZATION_GOAL
        assert job["billing_event"] == cr.BILLING_EVENT and job["call_to_action"] == cr.CTA_TYPE


def test_known_meta_error_shows_catalog_message_and_action(tmp_path):
    meta = RefusingMeta({"code": 1885272, "message": "Budget too low raw"})
    out, _ = _run(meta, _draft(tmp_path))
    assert out.stop_kind == "graph_error"
    assert "The budget is too low." in out.error and "Budget too low raw" not in out.error
    (p,) = out.problems
    assert p["title"] == "The budget is too low." and p["action"] and "1885272" in p["raw"]
    res = pa.result_dict(out)
    assert res["problems"] == out.problems and res["error"] == out.error


def test_unknown_meta_code_shows_the_raw_text(tmp_path):
    meta = RefusingMeta({"code": 99999999, "message": "Strange new refusal"})
    out, _ = _run(meta, _draft(tmp_path))
    assert "Strange new refusal" in out.error
    (p,) = out.problems
    assert "Strange new refusal" in p["title"] and p["action"] == ""


@pytest.mark.parametrize("code", [1404078, 2859015, 1404163])
def test_blocked_codes_stop_the_client_and_explain(tmp_path, code):
    meta = RefusingMeta({"code": code, "message": "blocked raw"})
    out, client = _run(meta, _draft(tmp_path))
    assert out.stop_kind == "policy_block" and isinstance(client.stopped, g.PolicyBlocked)
    assert out.error.startswith("Graph API stopped:") and "blocked raw" not in out.error
    assert out.problems and out.problems[0]["action"]
    assert f"({code})" in str(client.stopped)


def test_blocked_code_as_subcode_also_stops_and_explains(tmp_path):
    meta = RefusingMeta({"code": 100, "error_subcode": 1404078, "message": "blocked raw"})
    out, client = _run(meta, _draft(tmp_path))
    assert out.stop_kind == "policy_block" and isinstance(client.stopped, g.PolicyBlocked)
    assert "temporarily blocked" in out.error
