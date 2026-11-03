"""ACL-2266: Check plan через кэш вердиктов над поддельной Meta: повтор — ноль чтений и без профиля."""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager

import pytest

from src.fb_api import graph as g
from src.web import plan_cache as pc
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft
from tests.test_acl1761_plan_check import _read_session


def _two(tmp_path):
    a = _draft(tmp_path, ads=1)
    b = _draft(tmp_path, ads=1)
    b.name = b.ad_sets[0].name = "MX Leads 2"
    return a, b


def _setup(monkeypatch, drafts, meta=None):
    """build_drafts отдаёт копии (как настоящий: каждый раз собирает заново); возвращает (api, opened, meta)."""
    from src.web import publish_api

    meta = meta or FakeMeta()
    monkeypatch.setattr(publish_api, "build_drafts",
                        lambda slots, **kw: copy.deepcopy(drafts))
    opened = _read_session(monkeypatch, meta)
    return publish_api, opened, meta


def _check(api, **kw):
    return api.check_plan([{"name": "x"}], account_label="Test", country="MX", **kw)


def test_a_clean_check_is_cached_and_the_repeat_sends_nothing_to_meta(tmp_path, monkeypatch):
    drafts = list(_two(tmp_path))
    api, opened, meta = _setup(monkeypatch, drafts)
    first = _check(api)
    assert first["error"] == "" and [r["campaign_name"] for r in first["results"]] == ["MX Leads 1", "MX Leads 2"]
    assert first["cache"]["hits"] == 0 and first["cache"]["read"] == 2
    assert all(r.get("checked_at") and not r.get("cached") for r in first["results"])
    assert len(opened) == 1

    # Повтор: ни сессии чтения, ни профиля, ни вызова Graph.
    def boom(*a, **k):
        raise AssertionError("a cache hit must not open a read session")

    from src.analysis_agent import account_spend as _as

    monkeypatch.setattr(_as, "serialized_browser", boom)
    second = _check(api)
    assert len(opened) == 1 and second["error"] == "" and second["error_kind"] == ""
    assert second["account_audit"] is None
    assert [r["campaign_name"] for r in second["results"]] == ["MX Leads 1", "MX Leads 2"]
    assert all(r["cached"] is True for r in second["results"])
    assert [r["checked_at"] for r in second["results"]] == [r["checked_at"] for r in first["results"]]
    assert second["cache"]["hits"] == 2 and second["cache"]["read"] == 0 and second["cache"]["forced"] is False
    assert meta.posts == []


def test_one_edited_campaign_is_the_only_one_read_and_the_order_is_kept(tmp_path, monkeypatch):
    a, b = _two(tmp_path)
    api, opened, meta = _setup(monkeypatch, [a, b])
    _check(api)
    seen = []
    real = api._plan_in_read_session

    def spy(label, drafts, **kw):
        seen.append([d.name for d in drafts])
        return real(label, drafts, **kw)

    monkeypatch.setattr(api, "_plan_in_read_session", spy)
    a2 = copy.deepcopy(a)
    a2.ad_sets[0].ads[0].ad_body = "Edited text from the library"
    monkeypatch.setattr(api, "build_drafts", lambda slots, **kw: copy.deepcopy([a2, b]))
    out = _check(api)
    assert seen == [["MX Leads 1"]], "прочитана должна быть только изменённая кампания"
    assert [r["campaign_name"] for r in out["results"]] == ["MX Leads 1", "MX Leads 2"]
    assert [bool(r.get("cached")) for r in out["results"]] == [False, True]
    assert out["cache"]["hits"] == 1 and out["cache"]["read"] == 1


def test_force_reads_everything_and_refreshes_the_cache(tmp_path, monkeypatch):
    drafts = list(_two(tmp_path))
    api, opened, _ = _setup(monkeypatch, drafts)
    first = _check(api)
    forced = _check(api, force=True)
    assert len(opened) == 2 and forced["cache"] == {**forced["cache"], "hits": 0, "read": 2, "forced": True}
    assert not any(r.get("cached") for r in forced["results"])
    after = _check(api)
    assert len(opened) == 2 and all(r["cached"] for r in after["results"])
    assert [r["checked_at"] for r in after["results"]] == [r["checked_at"] for r in forced["results"]]
    assert first["cache"]["forced"] is False


def test_duplicate_names_are_never_cached_and_everything_is_read(tmp_path, monkeypatch):
    a = _draft(tmp_path, ads=1)
    api, opened, _ = _setup(monkeypatch, [a, copy.deepcopy(a)])
    first = _check(api)
    again = _check(api)
    assert len(opened) == 2 and again["cache"]["hits"] == 0 and pc.counters()["puts"] == 0
    # Review 2 / B2: same-name drafts give exactly one row per draft, in draft order (as before the cache).
    for out in (first, again):
        assert [r["campaign_name"] for r in out["results"]] == [a.name, a.name]


def test_cache_off_reads_every_time(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.TTL_ENV, "0")
    api, opened, _ = _setup(monkeypatch, [_draft(tmp_path, ads=1)])
    _check(api)
    _check(api)
    assert len(opened) == 2


def test_a_campaign_error_or_empty_audit_is_not_cached(tmp_path, monkeypatch):
    bad = _draft(tmp_path, ads=1)
    bad.link = ""
    api, opened, _ = _setup(monkeypatch, [bad])
    out = _check(api)
    assert "Tracking link is empty" in out["results"][0]["error"]
    _check(api)
    assert len(opened) == 2, "строка с ошибкой кампании не вердикт"
    assert pc.counters()["rejected"].get("error") == 2

    from src.fb_api import creator as cr
    ok = _draft(tmp_path, ads=1)
    monkeypatch.setattr(api, "build_drafts", lambda slots, **kw: copy.deepcopy([ok]))
    monkeypatch.setattr("src.fb_api.preflight_rules.server_verdicts",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit crashed")))
    pc.reset_for_tests()
    out = _check(api)
    assert out["results"][0]["success"] is True and out["results"][0]["audit"] == []
    assert pc.counters()["rejected"] == {"audit_empty": 1} and pc.counters()["puts"] == 0
    assert cr  # модуль нужен подмене выше


def test_account_error_caches_nothing(tmp_path, monkeypatch):
    api, opened, _ = _setup(monkeypatch, [_draft(tmp_path, ads=1)])
    from src.web.publish_api import ApiPublishError

    def fail(*a, **k):
        raise ApiPublishError("Test: ad account is disabled", code="account")

    monkeypatch.setattr(api, "_plan_in_read_session", fail)
    out = _check(api)
    assert out["error_kind"] == "account" and out["account_audit"]["status"] == "error"
    assert pc.counters()["puts"] == 0


def test_a_partial_failure_caches_the_finished_clean_prefix_only(tmp_path, monkeypatch):
    a, b = _two(tmp_path)
    api, opened, _ = _setup(monkeypatch, [a, b])
    real = api._plan_in_read_session

    def flaky(label, drafts, *, results=None, session_info=None):
        results = [] if results is None else results
        results.extend(real(label, drafts[:1], session_info=session_info))   # первая кампания прочитана
        raise OSError("network broke")

    monkeypatch.setattr(api, "_plan_in_read_session", flaky)
    out = _check(api)
    assert out["error_kind"] == "plan" and [r["campaign_name"] for r in out["results"]] == ["MX Leads 1"]
    assert pc.counters()["puts"] == 1
    monkeypatch.setattr(api, "_plan_in_read_session", real)
    again = _check(api)
    assert [bool(r.get("cached")) for r in again["results"]] == [True, False]
    assert again["cache"] == {**again["cache"], "hits": 1, "read": 1}


def test_a_contract_violation_caches_nothing_and_clears_rows(tmp_path, monkeypatch):
    api, opened, _ = _setup(monkeypatch, [_draft(tmp_path, ads=1)])
    _read_session(monkeypatch, FakeMeta(), writes=True)      # сессия с правом записи
    out = _check(api)
    assert out["error_kind"] == "plan" and out["results"] == []
    assert pc.counters()["puts"] == 0


def test_a_graph_stop_in_the_session_caches_nothing(tmp_path, monkeypatch):
    a, b = _two(tmp_path)
    api, opened, _ = _setup(monkeypatch, [a, b])
    real = api._plan_in_read_session

    def stopped(label, drafts, *, results=None, session_info=None):
        out = real(label, drafts, results=results, session_info=session_info)
        session_info["stopped"] = True
        return out

    monkeypatch.setattr(api, "_plan_in_read_session", stopped)
    _check(api)
    assert pc.counters()["puts"] == 0 and pc.counters()["rejected"] == {"stopped": 2}


def test_a_hit_never_touches_the_profile_cooldown_or_budget(tmp_path, monkeypatch):
    api, opened, _ = _setup(monkeypatch, [_draft(tmp_path, ads=1)])
    _check(api)
    before = (len(opened), opened[0].calls)
    calls = []
    from src.analysis_agent import account_spend_api as _api

    monkeypatch.setattr(_api, "_session", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(g.GraphClient, "reset_budget", lambda *a, **k: calls.append("budget"))
    out = _check(api)
    assert calls == [] and (len(opened), opened[0].calls) == before and out["cache"]["hits"] == 1


def test_a_changed_account_map_entry_does_not_reuse_the_verdict(tmp_path, monkeypatch):
    from types import SimpleNamespace

    drafts = [_draft(tmp_path, ads=1)]
    api, opened, _ = _setup(monkeypatch, drafts)
    monkeypatch.setattr(api, "_resolve_entry", lambda label: SimpleNamespace(ad_account_id="act_1"))
    _check(api)
    monkeypatch.setattr(api, "_resolve_entry", lambda label: SimpleNamespace(ad_account_id="act_2"))
    _check(api)
    assert len(opened) == 2


def test_force_and_plain_requests_do_not_merge_in_the_flight(monkeypatch):
    from src.web import publish_api as api

    gate, runs = threading.Event(), []

    def once(slots, *, account_label, country, force=False):
        runs.append(force)
        gate.wait(5)
        return {"account": account_label, "results": [], "dry_run": True, "error": ""}

    monkeypatch.setattr(api, "_check_plan_once", once)
    ts = [threading.Thread(target=lambda f=f: api.check_plan([{"name": "C1"}], account_label="Acc",
                                                             country="MX", force=f)) for f in (False, True)]
    for t in ts:
        t.start()
    end = __import__("time").monotonic() + 3
    while len(runs) < 2 and __import__("time").monotonic() < end:
        __import__("time").sleep(0.01)
    gate.set()
    for t in ts:
        t.join(5)
    assert sorted(runs) == [False, True]


def test_the_telemetry_diag_and_the_event_payload_carry_the_cache_fields(tmp_path, monkeypatch):
    from src.acl_event_log.events import check_payload
    from src.web import publish_api

    recorded = []
    drafts = list(_two(tmp_path))
    api, opened, _ = _setup(monkeypatch, drafts)
    monkeypatch.setattr(publish_api, "_record", lambda step, ok, campaign="", diag=None, duration_s=None:
                        recorded.append((step, ok, diag)))
    first = _check(api)
    second = _check(api)
    (_, ok1, d1), (_, ok2, d2) = [r for r in recorded if r[0] == "api_plan_check"]
    assert d1["cached"] == 0 and d1["read"] == 2 and d1["forced"] is False and "build_s" in d1 and "read_s" in d1
    assert d1["graph_calls"] > 0
    assert d2["cached"] == 2 and d2["read"] == 0 and d2["graph_calls"] == 0
    p1 = check_payload(first, plan_hash_value="h", duration_s=1.0)
    p2 = check_payload(second, plan_hash_value="h", duration_s=0.1)
    assert (p1["cached_n"], p1["read_n"], p1["forced"]) == (0, 2, False)
    assert (p2["cached_n"], p2["read_n"], p2["ttl_min"]) == (2, 0, 60.0)
    assert check_payload({"results": []}, plan_hash_value="h", duration_s=0)["cached_n"] == 0


# ───────────────────────── маршруты и сбросы ─────────────────────────

def _cached_endpoint(monkeypatch, fields, backend="api"):
    import asyncio
    import json

    from src.web import app as web
    from tests.test_acl1761_plan_check import _FormReq

    monkeypatch.setenv("ACL_FB_BACKEND", backend)
    monkeypatch.setattr(web, "_parse_build_upload_form", lambda form: ("MX", "Test", [{"name": "x"}]))
    resp = asyncio.run(web.publish_plan_cached(_FormReq(fields)))
    return resp.status_code, json.loads(resp.body)


def test_cached_route_returns_only_hits_and_never_calls_check_plan_or_the_profile(tmp_path, monkeypatch):
    a, b = _two(tmp_path)
    api, opened, _ = _setup(monkeypatch, [a, b])
    _check(api)
    b2 = copy.deepcopy(b)
    b2.link = "https://example.invalid/changed"
    monkeypatch.setattr(api, "build_drafts", lambda slots, **kw: copy.deepcopy([a, b2]))

    def boom(*a_, **k):
        raise AssertionError("the cached route must not check")

    monkeypatch.setattr(api, "check_plan", boom)
    monkeypatch.setattr(api, "_check_plan_once", boom)
    code, body = _cached_endpoint(monkeypatch, {"dry_run": "1"})
    assert code == 200 and body["error"] == "" and body["dry_run"] is True and body["account_audit"] is None
    assert [r["campaign_name"] for r in body["results"]] == ["MX Leads 1"] and body["results"][0]["cached"] is True
    assert len(opened) == 1


def test_cached_route_has_the_same_guards_as_the_plan_route(monkeypatch):
    assert _cached_endpoint(monkeypatch, {})[0] == 400
    assert _cached_endpoint(monkeypatch, {"dry_run": "1"}, backend="rpa")[0] == 409


def test_a_finished_publish_job_forgets_the_account_verdicts(monkeypatch):
    from src.web import publish_jobs

    pc.reset_for_tests()
    from tests.test_acl2266_plan_cache import _put

    _put(acct="Acc", digest="a")
    _put(acct="Other", digest="b")
    publish_jobs._drop_plan_verdicts("acc")
    assert pc.peek("Acc", "MX", ACT, "a") is None and pc.peek("Other", "MX", ACT, "b") is not None


def test_job_finish_hook_runs_for_every_outcome(monkeypatch):
    from src.web import publish_jobs

    seen = []
    monkeypatch.setattr(publish_jobs, "_drop_plan_verdicts", lambda label: seen.append(label))
    monkeypatch.setattr(publish_jobs, "_persist_job", lambda *a, **k: None)
    monkeypatch.setattr(publish_jobs, "_record_history", lambda *a, **k: None)
    monkeypatch.setattr(publish_jobs, "_log_job_finished", lambda *a, **k: None)
    with publish_jobs._jobs_lock:
        publish_jobs._jobs["j-2266"] = {"job_id": "j-2266", "account_label": "Acc", "status": "running",
                                        "results": [], "errors": []}
    try:
        publish_jobs._finish("j-2266")
        publish_jobs._finish("j-2266", cancelled=True)
    finally:
        with publish_jobs._jobs_lock:
            publish_jobs._jobs.pop("j-2266", None)
    assert seen == ["Acc", "Acc"]


def test_targeting_confirm_and_clear_drop_all_verdicts(monkeypatch, tmp_path):
    import asyncio

    from src.web import app as web

    class Req:
        def __init__(self, body):
            self._b = body

        async def json(self):
            return self._b

    monkeypatch.setattr(web, "_targeting_gate", lambda: None)
    from src.fb_api import targeting_resolver as tr

    class Store:
        def __init__(self, *a, **k):
            pass

        def put(self, *a, **k):
            return {"term": "t"}

        def clear(self):
            return 1

        def remove(self, term):
            return True

    monkeypatch.setattr(tr, "ConfirmedStore", Store)
    monkeypatch.setattr(tr, "candidate_for", lambda *a: {"id": "1", "type": "interests", "name": "N"})
    from tests.test_acl2266_plan_cache import _put

    for call, body in ((web.targeting_resolver_confirm, {"term": "t", "id": "1", "type": "interests"}),
                       (web.targeting_resolver_clear, {"all": True}),
                       (web.targeting_resolver_clear, {"term": "t"})):
        _put()
        assert pc.peek("Acc", "MX", ACT, "d1") is not None
        resp = asyncio.run(call(Req(body)))
        assert resp.status_code == 200
        assert pc.peek("Acc", "MX", ACT, "d1") is None, call.__name__


@pytest.mark.parametrize("failure", ["graph_error", "call_budget"])
def test_an_unread_audit_rule_of_a_non_last_campaign_is_never_cached(tmp_path, monkeypatch, failure):
    """Review B1: GraphError / CallBudget inside the audit leave the rule `unknown`; the row is not a verdict."""
    import inspect

    from src.fb_api import creator as cr

    a, b = _two(tmp_path)
    api, opened, _ = _setup(monkeypatch, [a, b])
    real = cr.resolve_pixel

    def flaky(plan, facts):
        in_audit = any(f.function == "server_verdicts" for f in inspect.stack())
        if in_audit and plan.name == "MX Leads 1":
            raise (g.GraphError("Meta 500") if failure == "graph_error" else g.CallBudget("call budget of 5 reached"))
        return real(plan, facts)

    monkeypatch.setattr(cr, "resolve_pixel", flaky)
    first = _check(api)
    row = first["results"][0]
    assert any(x["status"] == "unknown" for x in row["audit"]), "the fixture must produce an unread rule"
    assert pc.counters()["rejected"].get("audit_unknown") == 1
    assert pc.counters()["puts"] == 1, "only the cleanly read second campaign is cached"
    again = _check(api)
    assert [bool(r.get("cached")) for r in again["results"]] == [False, True]


def test_an_account_error_drops_the_cached_verdicts_of_that_account(tmp_path, monkeypatch):
    """Review N1: a disabled account must not keep showing cached green rows."""
    a, b = _two(tmp_path)
    api, opened, _ = _setup(monkeypatch, [a, b])
    _check(api)
    assert pc.counters()["puts"] == 2
    edited = copy.deepcopy(a)
    edited.ad_sets[0].ads[0].ad_body = "Edited"
    monkeypatch.setattr(api, "build_drafts", lambda slots, **kw: copy.deepcopy([edited, b]))
    from src.web.publish_api import ApiPublishError

    def fail(*a_, **k):
        raise ApiPublishError("Test: ad account is disabled", code="account")

    monkeypatch.setattr(api, "_plan_in_read_session", fail)
    out = _check(api)
    assert out["error_kind"] == "account"
    monkeypatch.setattr(api, "build_drafts", lambda slots, **kw: copy.deepcopy([a, b]))
    assert pc.peek("Test", "MX", pc.act_of("Test"), pc.draft_digest(b)) is None


def test_an_unreadable_form_terms_rule_is_never_cached(tmp_path, monkeypatch):
    """Review 2 / B1b: forms.page_terms_accepted swallows GraphError -> `form_terms unknown` (no known prefix)."""
    from src.fb_api import forms as _forms

    a = _draft(tmp_path, ads=1)
    api, opened, _ = _setup(monkeypatch, [a])
    monkeypatch.setattr(_forms, "page_terms_accepted", lambda *x, **k: None)   # Graph answer swallowed -> None
    from src.fb_api import creator as cr
    from src.fb_api import preflight_rules as pf

    real_verdicts = pf.server_verdicts

    def templated(creator, plan, **k):
        for s_ in plan.ad_sets:                     # the audit reads the terms only for an instant form to be created
            s_.destination = "instant_form"
            for ad in s_.ads:
                ad.form_template = {"name": "tpl"}
        return real_verdicts(creator, plan, **k)

    monkeypatch.setattr(pf, "server_verdicts", templated)
    monkeypatch.setattr(cr.CampaignCreator, "_check_forms", lambda self, plan: None)
    out = _check(api)
    assert out["results"], out
    row = out["results"][0]
    terms = [x for x in row["audit"] if x["rule"] == "form_terms"]
    assert terms and terms[0]["status"] == "unknown", "the fixture must produce the unread rule"
    assert pc.counters()["rejected"].get("audit_unknown") == 1 and pc.counters()["puts"] == 0
    assert _check(api)["cache"]["hits"] == 0
