"""ACL-2392: short plain errors in the panel, raw Meta text only in the details."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.fb_api import creator as cr
from src.fb_api import error_catalog
from src.fb_api import graph as g
from src.fb_api import plain_errors as pe
from src.web import publish_api, publish_jobs
from tests.test_acl1761_api_create import FakeMeta, _draft, _run

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "acl2392").glob("*.json"))
TEMPLATE = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"

#: What must never reach the panel text (it lives in `failure.details`).
FORBIDDEN = [r"fbtrace", r"error_subcode", r"Graph API", r"graph\.facebook\.com", r"\bv\d+\.\d",
             r"\berror[ _]?code\b", r"http[ _]?\d{3}", r"\b\d{6,}\b"]


def _exc(fx: dict) -> BaseException:
    typ, msg = fx["type"], fx["message"]
    if typ == "WriteUnknown":
        return g.WriteUnknown(msg, path="/x", code=fx.get("code"), subcode=fx.get("subcode"),
                              status=fx.get("status"), details=fx.get("details"))
    if typ == "GraphError":
        return g.GraphError(msg, code=fx.get("code"), subcode=fx.get("subcode"), details=fx.get("details"))
    if typ in ("LoggedOut", "Checkpoint", "PolicyBlocked", "RateLimited"):
        extra = {"retry_after_s": fx["retry_after_s"]} if "retry_after_s" in fx else {}
        return getattr(g, typ)(msg, code=fx.get("code"), subcode=fx.get("subcode"), **extra)
    if typ == "RulesViolation":
        return cr.RulesViolation(msg, [])
    if typ == "PlanError":
        return cr.PlanError(msg)
    return publish_api.ApiPublishError(msg, code=fx.get("code", ""))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_each_class_has_the_exact_plain_text(path):
    fx = json.loads(path.read_text(encoding="utf-8"))
    exp = fx["expect"]
    failure = pe.classify(_exc(fx))
    assert failure.key == exp["key"]
    if fx.get("expect_catalog"):
        x = error_catalog.explain(fx["code"], None, fx["message"])
        assert (failure.short, failure.next_step) == (x.message_en, x.action_en)
    else:
        if exp["short"] == "@mlx":  # the existing Multilogin wording is reused unchanged
            from src.fb_api import mlx_session
            exp["short"] = mlx_session.message_for(mlx_session.TOKEN_EXPIRED)
        assert failure.short == exp["short"]
        assert failure.next_step == exp["next"]
        assert failure.severity == exp["severity"] and failure.retry == exp["retry"]
    assert failure.details.get("raw"), "the raw text must stay in the details"
    assert "synthetic" in fx


def test_the_1870226_fixture_is_the_live_text_and_keeps_its_subcode():
    fx = json.loads((FIXTURES[0].parent / "targeting_merged.json").read_text(encoding="utf-8"))
    assert fx["synthetic"] is False
    failure = pe.classify(_exc(fx))
    assert failure.details["subcode"] == 1870226 and failure.details["http_status"] == 500
    assert failure.line() == "Targeting options were merged by Meta - replace the interests and republish"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k != "details":
                yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_panel_text_never_shows_raw_fields(path):
    fx = json.loads(path.read_text(encoding="utf-8"))
    exc = _exc(fx)
    failure = pe.classify(exc)
    failure.left = pe.left_in_meta(campaign=True, adsets=1, ads=2)
    out = cr.Outcome(campaign_name="C", campaign_id="1", error=str(exc), stop_kind="unknown", failure=failure)
    for payload in (publish_api.result_dict(out), publish_api.dry_run_result(out)):
        for key in ("error", "blocking_failures", "warnings", "problems", "failure"):
            for text in _strings(payload.get(key)):
                for pattern in FORBIDDEN:
                    assert not re.search(pattern, text, re.I), (pattern, text)
        assert payload["failure"]["details"].get("raw")
    # job errors: plain line in `errors`, raw in `error_details`
    text, details = publish_jobs._plain_error("j", f"Graph API publish error: {exc}", exc)
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text, re.I), (pattern, text)
    assert details and details.get("raw")


def test_readback_mismatches_are_one_summary_line_and_the_gate_is_unchanged():
    mism = [{"object": "ad set 'A'", "field": "daily_budget", "expected": "500", "actual": "400"},
            {"object": "ad set 'A'", "field": "targeting", "expected": "x", "actual": "y"}]
    out = cr.Outcome(campaign_name="C", campaign_id="1", readback={"ok": False, "mismatches": mism})
    res = publish_api.result_dict(out)
    assert res["blocking_failures"] == [
        "Meta saved different values than planned: daily budget, targeting - open details"]
    assert res["publishable"] is False
    assert len(res["failure"]["details"]["readback_mismatches"]) == 2


def test_a_local_bug_is_not_blamed_on_meta():
    failure = pe.classify(KeyError("boom"))
    assert failure.line() == "Publishing stopped unexpectedly - open details"
    assert failure.details["raw"]


def test_template_shows_raw_details_only_through_the_details_builder():
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function cbFailureDetailsHtml(")
    end = text.index("document.addEventListener('click'", start)
    rest = text[:start] + text[end:]
    for m in re.finditer(r"failure\.details|error_details", rest):
        around = rest[max(0, m.start() - 60):m.end() + 5]
        assert "cbFailureDetailsHtml(" in around, around
    assert "error_raw" not in text


# -- 1870226 end to end -------------------------------------------------------

class RejectingMeta(FakeMeta):
    """Meta answers the ad set write with code 1 / http 500 / subcode 1870226 and creates nothing."""

    def write(self, path, params, **kw):
        if path.endswith("/adsets"):
            self.posts.append((path, dict(params)))
            return {"status": 500, "body": {"error": {
                "code": 1, "message": "An unknown error occurred", "error_subcode": 1870226,
                "error_user_title": "Some detailed targeting options have been combined",
                "error_user_msg": "Some of your detailed targeting options have been combined.",
                "fbtrace_id": "AbCdEfGhIjK"}}}
        return super().write(path, params, **kw)


def test_1870226_is_a_rejection_after_the_readback_and_does_not_block_the_job(tmp_path):
    meta = RejectingMeta()
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "rejected"
    assert client.unreconciled is None
    assert out.failure.key == "targeting_merged"
    assert out.failure.left == [
        "Left in Meta: an empty paused campaign. Publishing again reuses it, you do not need to delete it."]
    res = publish_api.result_dict(out)
    assert res["error"].startswith("Targeting options were merged by Meta - replace the interests and republish")
    assert res["failure"]["details"]["subcode"] == 1870226
    assert any(p.endswith("/adsets") for p, _ in meta.gets), "the readback ran before the verdict"


def test_an_unknown_result_that_the_readback_finds_keeps_todays_path(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adsets"] = "lost"
    out, _client = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and any("reconciled" in s for s in out.steps)


def test_a_plain_unknown_result_still_blocks_the_job(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adsets"] = "unsent"
    out, _client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "unknown"
    assert out.failure.key == "graph_write_unconfirmed"


def test_a_rejection_does_not_block_the_next_campaign_of_the_job(tmp_path):
    from tests.test_acl1761_api_create import _publisher

    meta = RejectingMeta()
    pub = _publisher(meta)
    first = pub.publish_draft(_draft(tmp_path, ads=1))
    assert first["failure"]["key"] == "targeting_merged"
    assert pub.blocked in (None, "")
    d2 = _draft(tmp_path, ads=1)
    d2.name = d2.ad_sets[0].name = "MX Leads 2"
    second = pub.publish_draft(d2)
    assert not (second["error"] or "").startswith("Not started")
