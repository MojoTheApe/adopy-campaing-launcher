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
FORBIDDEN = [r"fbtrace", r"error_subcode", r"Graph API", r"graph[.]facebook[.]com", r"\bv\d+[.]\d",
             r"\berror[ _]?code\b", r"http[ _]?\d{3}", r"\b\d{6,}\b",
             r"access_token", r"\bact_", r"OAuthException", r"\(#\d+\)", r"\bcode \d+", r"\[\w+="]  # linux-ok: regex data


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
            if k not in ("details", "raw"):      # a problem's `raw` is shown only in Recent runs, never in the panel
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
    problems = [cr.graph_problem(exc)] if isinstance(exc, (g.GraphError, g.GraphStop)) else []
    out = cr.Outcome(campaign_name="C", campaign_id="1", error=str(exc), stop_kind="unknown", failure=failure,
                     problems=problems)
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
        "Meta saved different values than planned: daily budget, targeting - see details in Recent runs"]
    assert res["publishable"] is False
    assert len(res["failure"]["details"]["readback_mismatches"]) == 2


def test_a_local_bug_is_not_blamed_on_meta():
    failure = pe.classify(KeyError("boom"))
    assert failure.line() == "Publishing stopped unexpectedly - see details in Recent runs"
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


def test_1870226_is_worded_as_a_rejection_but_the_gating_is_unchanged(tmp_path):
    meta = RejectingMeta()
    out, client = _run(meta, _draft(tmp_path, ads=1))
    # ACL-2392: classification only. Stop kind and the unreconciled mark are what they were before this task.
    assert out.stop_kind == "unknown"
    assert client.unreconciled is not None
    assert out.failure.key == "targeting_merged"
    assert out.failure.left == [
        "Left in Meta: an empty paused campaign. Publishing again reuses it, you do not need to delete it."]
    assert out.failure.stage.startswith('while creating ad set "')
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


def test_a_1870226_stop_still_blocks_the_next_campaign_of_the_job(tmp_path):
    from tests.test_acl1761_api_create import _publisher

    meta = RejectingMeta()
    pub = _publisher(meta)
    first = pub.publish_draft(_draft(tmp_path, ads=1))
    assert first["failure"]["key"] == "targeting_merged"
    assert pub.blocked, "the job stays blocked until the unknown write is reconciled (unchanged gating)"
    d2 = _draft(tmp_path, ads=1)
    d2.name = d2.ad_sets[0].name = "MX Leads 2"
    second = pub.publish_draft(d2)
    assert second["not_started"] is True


# -- owner delta: stage in the line, link to Recent runs, left text, history ----------------------

def test_stage_in_plain_words_from_the_write_label():
    assert pe.stage_of("campaign") == "while creating the campaign"
    assert pe.stage_of("ad set 'Kuwait 25-45'") == 'while creating ad set "Kuwait 25-45"'
    assert pe.stage_of("ad 2: image upload") == "while uploading ad 2 creative"
    assert pe.stage_of("ad 2: creative") == "while creating ad 2 creative"


@pytest.mark.parametrize("key,exc", [
    ("unknown", g.GraphError("odd", code=77)),
    ("graph_write_unconfirmed", g.WriteUnknown("lost", path="/x")),
    ("graph_read_timeout", publish_api.ApiPublishError("timed out", code="cdp_timeout")),
])
def test_staged_classes_name_the_stage_and_point_to_recent_runs(key, exc):
    exc.stage = "while creating ad set \"X\""
    failure = pe.classify(exc)
    assert failure.key == key
    assert '(while creating ad set "X")' in failure.short
    assert failure.details["stage"] == exc.stage
    if key == "unknown":
        assert failure.line() == 'Meta refused this step (while creating ad set "X") - see details in Recent runs'


def test_a_job_level_stop_names_the_phase():
    exc = publish_api.ApiPublishError("boom", stage="while opening the Multilogin profile")
    assert pe.classify(exc).stage == "while opening the Multilogin profile"


def test_left_text_names_the_reuse_rule_and_the_second_name():
    assert pe.left_in_meta(campaign=True, adsets=1, ads=2, name="MX") == [
        'Left in Meta: a paused campaign, 1 ad set, 2 ads. Publishing again reuses it if its settings match; '
        'otherwise the new one is published as a new copy, "MX copy 1" (or the next free number).']
    assert pe.left_in_meta(campaign=True, name="MX")[0].startswith("Left in Meta: a paused campaign. ")


def test_history_keeps_the_failure_and_old_lines_still_render(tmp_path, monkeypatch):
    from src.web import publish_history as ph

    failure = pe.classify(g.GraphError("odd token=SECRETSECRET123", code=77))
    job = {"job_id": "j1", "status": "done", "errors": ["Account logged out - log in and republish"],
           "error_details": [{"raw": "Bearer abcdefghijklmnop", "code": 190}],
           "results": [{"campaign_name": "C", "success": False, "error": failure.text(),
                        "failure": failure.as_dict()}]}
    entry = ph.entry_from_job(job)
    saved = entry["results"][0]["failure"]
    assert saved["line"] == failure.line() and "SECRETSECRET123" not in json.dumps(entry)
    assert "abcdefghijklmnop" not in json.dumps(entry["error_details"])
    assert saved["details"]["raw"]
    old = ph.entry_from_job({"job_id": "j0", "results": [{"campaign_name": "C", "error": "x"}]})
    assert old["results"][0]["failure"] is None and old["error_details"] == []


def test_recent_runs_popup_shows_details_and_copy_and_the_builder_only_links():
    js = (TEMPLATE.parent.parent / "static" / "recent_runs.js").read_text(encoding="utf-8")
    for needle in ("Copy details", "Hide details", "failureBlock", "error_details", "aclRecentRunsOpen"):
        assert needle in js, needle
    html = TEMPLATE.read_text(encoding="utf-8")
    start = html.index("function cbFailureDetailsHtml(")
    body = html[start:html.index("document.addEventListener('click'", start)]
    assert "Details in Recent runs" in body and "JSON.stringify" not in body and "Copy details" not in body


# -- round 2 (review of PR 1477) ------------------------------------------------------------------

import types  # noqa: E402

RAW_TRACE = "fbtrace_id=AbCdEf123"


def _halted_job_error(stop):
    from tests.test_acl1761_api_create import _publisher

    pub = _publisher(FakeMeta())
    try:
        pub._halt(stop)
    except publish_api.ApiPublishError as exc:
        assert exc.__cause__ is stop, "the Graph stop must stay chained so the job error is classified from it"
        return publish_jobs._plain_error("j", f"Graph API publish error: {exc}", exc)
    raise AssertionError("_halt did not raise")


@pytest.mark.parametrize("stop,key_text", [
    (g.RateLimited(f"Graph API rate limit (17): User request limit reached [type=OAuthException, {RAW_TRACE}]",
                   code=17), "Meta is limiting requests"),
    (g.PolicyBlocked(f"Graph API refused (368): temporarily blocked [{RAW_TRACE}]", code=368),
     "do not retry now"),
    (g.CallBudget(f"call budget used up [{RAW_TRACE}]", code=None), "Meta is limiting requests"),
    (g.PageFailed(f"page helper failed (#100) [{RAW_TRACE}]", code=None), "see details in Recent runs"),
])
def test_job_level_graph_stops_show_the_plain_line_not_the_meta_text(stop, key_text):
    text, details = _halted_job_error(stop)
    assert key_text in text
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text, re.I), (pattern, text)
    assert details and RAW_TRACE in json.dumps(details), "the raw text stays in the details"


def test_api_codes_of_graph_stops_map_even_without_the_chain():
    for code, key in (("rate_limited", "rate_limited"), ("call_budget", "rate_limited"),
                      ("policy_block", "policy_block")):
        assert pe.classify(publish_api.ApiPublishError(f"x [{RAW_TRACE}]", code=code)).key == key


@pytest.mark.parametrize("exc", [
    cr.TargetingUnresolved([{"term": "Proxy servers", "state": "unresolved", "candidates": []}]),
    cr.TargetingUnresolved([{"term": "Cars", "state": "unresolved", "candidates": [], "reason": "search timed out"}]),
    cr.TargetingUnresolved([{"term": "Cars", "state": "unresolved",
                             "candidates": [{"name": "checkpoint fans", "type": "interests"}]}]),
    cr.PlanError("Ad set 'Timeout promo': the start time 2026-01-01 is not in the future"),
])
def test_user_data_inside_our_own_text_is_not_sniffed(exc):
    failure = pe.classify(exc)
    assert failure.key == "plan_text"
    assert failure.short.startswith(str(exc)[:12])   # our own sentence, kept


def test_the_catalog_wins_over_a_text_match_in_meta_data():
    exc = g.GraphError("Graph API error 100: Invalid parameter [error_user_title=Instant Form privacy policy "
                       "link is missing]", code=100, details={"error_user_title": "Instant Form privacy link"})
    failure = pe.classify(exc)
    assert failure.key != "form_unavailable"
    assert "Instant Form" not in failure.short


def test_a_local_map_problem_is_not_shown_as_a_disabled_account():
    assert pe.classify(publish_api.ApiPublishError("Acc is not in the account map")).key == "plan_text"
    assert pe.classify(cr.PlanError("This profile has no access to the ad account from the account map "
                                    "(not in /me/adaccounts). Nothing was created.")).key == "plan_text"
    assert pe.classify(cr.PlanError("The ad account is not active (account_status 2). Nothing was created.")
                       ).key == "account_blocked"


def test_no_reopen_timer_promise_without_a_known_wait_and_gate_text_is_not_account_level():
    f = pe.classify(publish_api.ApiPublishError("Ads Manager did not finish loading (no Graph session in the page)"))
    assert f.key == "tab_unresponsive" and "reopened" not in f.line() and "10 min" not in f.line()
    assert "account" not in pe.classify(publish_api.ApiPublishError("busy", code="gate_wait")).line()


def test_memory_text_is_the_acl2400_text():
    expected = "Not enough free memory - close a profile and retry"     # warm.MEMORY_HOLD_MESSAGE (ACL-2400)
    for code in ("below_reserve", "memory_low"):
        assert pe.classify(publish_api.ApiPublishError("x", code=code)).line() == expected


def test_unrecorded_meta_codes_are_not_guessed():
    assert set(pe._CODE_KEYS) == {1870226}, "only the recorded subcode may map to a class by itself"
    for sub in (1404163, 1404078, 2859015, 1815629, 3858258):
        assert pe.classify(g.GraphError("odd", code=77, subcode=sub)).key in ("unknown", "catalog"), sub


def test_an_empty_failure_text_can_never_turn_a_failed_campaign_into_success():
    empty = pe.Failure("x", "")
    out = cr.Outcome(campaign_name="C", campaign_id="1", error="raw stop text", failure=empty)
    res = publish_api.result_dict(out)
    assert res["success"] is False and res["error"] == "raw stop text"
    assert publish_api.dry_run_result(out)["error"] == "raw stop text"


def test_builder_panel_does_not_render_the_raw_problem_text():
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function cbProblemsHtml(")
    body = text[start:text.index("\n}\n", start)]
    assert "p.raw" not in body


def test_pin_page_stop_and_earlier_stop_are_plain():
    from tests.test_acl1761_api_create import _publisher

    pub = _publisher(FakeMeta())
    pub.stop = g.PolicyBlocked(f"Graph API refused (368): blocked [{RAW_TRACE}]", code=368)
    res = pub.publish_draft(types.SimpleNamespace(name="C"))
    assert res["not_started"] is True and RAW_TRACE not in json.dumps(res["error"]) and "(368)" not in res["error"]


def test_the_event_log_keeps_the_raw_stop_text_and_the_raw_readback_lines():
    from src.acl_event_log.events import result_payload

    raw = "ad set 'A': the write result is unknown (Graph API error 1 ... error_subcode=1870226) and nothing was found"
    out = cr.Outcome(campaign_name="C", campaign_id="1", error=raw, stop_kind="unknown",
                     failure=pe.classify(g.WriteUnknown("x", path="/p", code=1, subcode=1870226)))
    payload = result_payload(publish_api.result_dict(out))
    assert payload["error"] == raw          # what the event log always received
    mism = [{"object": "ad set 'A'", "field": "daily_budget", "expected": "500", "actual": "400"}]
    out2 = cr.Outcome(campaign_name="C", campaign_id="1", readback={"ok": False, "mismatches": mism})
    lines = result_payload(publish_api.result_dict(out2))["blocking_failures"]
    assert lines and lines[0].startswith("readback mismatch: ad set 'A' daily_budget")
    ok = result_payload(publish_api.result_dict(cr.Outcome(campaign_name="C")))
    assert ok["error"] is None


# -- round 3 --------------------------------------------------------------------------------------

def test_real_preflight_no_access_is_not_shown_as_a_disabled_account():
    from tests.test_acl1761_api_create import ACT2

    client = g.GraphClient(FakeMeta(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(cr.PlanError) as ei:
        cr.preflight_account(client, ACT2)          # the profile's /me/adaccounts does not list this account
    failure = pe.classify(ei.value)
    assert failure.key == "plan_text" and "no access to the ad account" in failure.line()
    assert "disabled" not in failure.line() and "spending limit" not in failure.line()


def _main_shaped_blocking(mismatches, raw_error):
    """What `result_dict` produced before ACL-2392 (main): raw readback lines, then the raw "not finished"."""
    lines = [f"readback mismatch: {m['object']} {m['field']} — expected {m['expected']!r}, "
             f"got {'Not read' if m.get('not_read') else repr(m['actual'])}" for m in mismatches]
    return lines + (["not finished: " + raw_error] if raw_error else [])


@pytest.mark.parametrize("with_mismatch,raw_error", [
    (True, "ad set 'A': the write result is unknown (Graph API error 1) and nothing was found by readback."),
    (False, "Graph API stopped: rate limit (17)"),
    (True, ""),
])
def test_event_log_blocking_failures_match_main(with_mismatch, raw_error):
    from src.acl_event_log.events import result_payload

    mism = [{"object": "ad set 'A'", "field": "daily_budget", "expected": "500", "actual": "400"},
            {"object": "ad", "field": "name", "expected": "x", "actual": "y", "not_read": True}] if with_mismatch else []
    failure = pe.classify(g.WriteUnknown("x", path="/p", code=1, subcode=1870226)) if raw_error else None
    out = cr.Outcome(campaign_name="C", campaign_id="1", error=raw_error, stop_kind="unknown" if raw_error else "",
                     failure=failure, readback={"ok": False, "mismatches": mism} if mism else {})
    payload = result_payload(publish_api.result_dict(out))
    assert payload["blocking_failures"] == _main_shaped_blocking(mism, raw_error)
    assert payload["error"] == (raw_error or None)
