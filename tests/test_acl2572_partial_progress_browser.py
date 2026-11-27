"""ACL-2572: the builder result shows what a partly created publish already made, the stage that stopped
and the plain next action (real DOM, offline Chromium, fake publish answers)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser, page  # noqa: E402,F401

REPORT = Path(__file__).resolve().parents[1] / "src/web/templates/report.html"


def _stage(label, state, oid=""):
    return {"label": label, "state": state, "id": oid}


def _op(state, stages, nxt, partial=None):
    ids = {s["label"]: s["id"] for s in stages if s["state"] == "confirmed" and s["id"]}
    return {"id": "op1", "state": state, "stages": stages,
            "partial_ids": ids if partial is None else partial, "next_action": nxt}


PARTIAL = _op("stopped", [_stage("campaign", "confirmed", "111"), _stage("ad set 1", "confirmed", "222"),
                          _stage("ad set 2", "unknown"), _stage("ad 1", "sent")],
              "Publish again - the last step is read back first and sent again only if Meta does not have it.")
COMPLETE = _op("complete", [_stage("campaign", "confirmed", "111"), _stage("ad set 1", "confirmed", "222"),
                            _stage("ad 1", "confirmed", "333")],
               "Nothing to do - everything was created and checked (campaign is PAUSED).")


def _html(page, result):
    return page.evaluate("(r) => cbOperationProgressHtml(r)", result)


def _text(page, result):
    return page.evaluate("""(r) => { const d = document.createElement('div');
        d.innerHTML = cbOperationProgressHtml(r); return d.innerText; }""", result)


def test_partial_campaign_shows_created_objects_stage_and_next_action(page):
    t = _text(page, {"operation": PARTIAL})
    assert "campaign 111" in t and "1 ad set 222" in t
    assert "ad set 2 (result unknown)" in t and "ad 1 (sent, not confirmed)" in t
    assert "Next: Publish again - the last step is read back first" in t


def test_ids_are_copyable_text_not_links(page):
    h = _html(page, {"operation": PARTIAL})
    assert "<a " not in h and "<button" not in h and 'user-select:all">111<' in h


def test_unknown_or_sent_stage_is_not_counted_as_created(page):
    t = _text(page, {"operation": PARTIAL})
    assert "ad object" not in t and "2 ad sets" not in t
    only_unknown = _op("open", [_stage("campaign", "unknown")], "Wait 10 minutes, then publish again.")
    t = _text(page, {"operation": only_unknown})
    assert "Nothing was confirmed as created." in t and "campaign (result unknown)" in t
    assert "Created so far" not in t and "Next: Wait 10 minutes, then publish again." in t


def test_an_unknown_stage_with_an_id_is_still_not_created(page):
    op = _op("open", [_stage("ad 1", "unknown", "999")], "Publish again.")
    assert "999" not in _html(page, {"operation": op})


def test_complete_campaign_adds_nothing(page):
    assert _html(page, {"operation": COMPLETE, "success": True}) == ""


def test_missing_or_odd_operation_adds_nothing(page):
    for r in ({}, {"operation": None}, {"operation": ""}, {"operation": []}, {"operation": 5}, {"operation": {}},
              {"operation": {"state": "stopped"}}, {"operation": {"state": "stopped", "stages": [], "partial_ids": {},
                                                                    "next_action": "  "}}):
        assert _html(page, r) == "", r


def test_odd_values_inside_operation_do_not_break_or_inject(page):
    op = {"state": "stopped", "stages": [None, 5, "x", {"label": None, "state": "weird", "id": {"a": 1}},
                                         {"label": "campaign", "state": "confirmed", "id": 0},
                                         {"label": "ad set 1", "state": "confirmed", "id": "<img src=x onerror=alert(1)>"}],
          "partial_ids": ["a"], "next_action": 7}
    h = _html(page, {"operation": op})
    assert "<img" not in h and "&lt;img" in h
    t = _text(page, {"operation": op})
    assert "? (not confirmed)" in t and "Next:" not in t
    op2 = {"state": "failed", "stages": "nope", "partial_ids": {"campaign": "55", "ad 1": "", "ad set 1": None},
           "next_action": "Publish again."}
    t = _text(page, {"operation": op2})
    assert "campaign 55" in t and "ad object" not in t and "Next: Publish again." in t


def test_partial_ids_alone_are_enough_and_not_duplicated(page):
    op = {"state": "stopped", "stages": [_stage("campaign", "confirmed", "111")],
          "partial_ids": {"campaign": "111", "ad set 1": "222"}, "next_action": "Publish again."}
    t = _text(page, {"operation": op})
    assert t.count("111") == 1 and "1 ad set 222" in t


def test_problem_block_and_job_row_both_render_the_progress(page):
    base = {"campaign_name": "C1", "backend": "api", "success": False, "operation": PARTIAL}
    structured = dict(base, problems=[{"severity": "blocking", "title": "Ad set 2 failed", "step": "adset"}])
    assert "Created so far: campaign 111" in page.evaluate(
        "(r) => { const d = document.createElement('div'); d.innerHTML = cbProblemsHtml(r); return d.innerText; }", structured)
    plain = dict(base, error="Publish stopped")
    job = {"job_id": "jjjjjjjjjjjj", "status": "done", "done_count": 1, "total": 1, "results": [plain]}
    page.route("**/publish-jobs/jjjjjjjjjjjj", lambda r: r.fulfill(content_type="application/json", body=json.dumps(job)))
    page.evaluate("""() => { const p = document.createElement('div'); p.id = 'fakepanel'; document.body.appendChild(p);
        cbPollJob('jjjjjjjjjjjj', p, null, null, null, true); }""")
    page.wait_for_function("document.getElementById('fakepanel').innerText.includes('Created so far')")
    t = page.inner_text("#fakepanel")
    assert "Next: Publish again" in t and "Publish stopped" in t


def test_complete_job_row_looks_as_today(page):
    ok = {"campaign_name": "C1", "backend": "api", "success": True, "publishable": True, "status": "PAUSED",
          "campaign_id": "111", "operation": COMPLETE}
    job = {"job_id": "kkkkkkkkkkkk", "status": "done", "done_count": 1, "total": 1, "results": [ok]}
    page.route("**/publish-jobs/kkkkkkkkkkkk", lambda r: r.fulfill(content_type="application/json", body=json.dumps(job)))
    page.evaluate("""() => { const p = document.createElement('div'); p.id = 'okpanel'; document.body.appendChild(p);
        cbPollJob('kkkkkkkkkkkk', p, null, null, null, true); }""")
    page.wait_for_function("document.getElementById('okpanel').innerText.includes('C1')")
    assert page.locator("#okpanel .cb-op-progress").count() == 0


def test_no_new_route_or_write_in_the_progress_function():
    src = REPORT.read_text(encoding="utf-8")
    body = src[src.index("function cbOperationProgressHtml("):src.index("// ACL-1761: итог кампании")]
    assert "fetch(" not in body and "localStorage" not in body and "XMLHttpRequest" not in body
