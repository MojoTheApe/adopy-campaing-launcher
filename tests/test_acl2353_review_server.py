"""ACL-2353: «Before create review» на сервере — отпечаток, замороженные черновики, отказ по одной кампании.

Ни Meta, ни профиля: Graph — `FakeMeta` из теста ACL-1761, издатель задания — подделка.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from src.acl_event_log import events as ev
from src.fb_api import creator as cr
from src.web import publish_api, publish_review
from src.web import publish_jobs as pj
from src.web.app import app
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run


# ───────────────────────── 14. отпечаток ─────────────────────────

def _fp(draft):
    return publish_review.review_fingerprint(cr.plan_from_draft(draft))


def test_same_input_gives_the_same_fingerprint(tmp_path):
    draft = _draft(tmp_path)
    assert _fp(draft) == _fp(draft)
    assert publish_review.fingerprints([draft]) == {"MX Leads 1": _fp(draft)}


def test_changed_text_link_map_value_or_template_changes_the_fingerprint(tmp_path):
    draft = _draft(tmp_path)
    base = _fp(draft)
    ad = draft.ad_sets[0].ads[0]
    for obj, attr, value in ((ad, "ad_body", "Another body"), (draft, "link", "https://example.invalid/other"),
                             (draft, "page_id", "999")):
        old = getattr(obj, attr)
        setattr(obj, attr, value)
        assert _fp(draft) != base, attr
        setattr(obj, attr, old)
        assert _fp(draft) == base, "restored input gives the value back"
    plan = cr.plan_from_draft(draft)
    plan.ad_sets[0].ads[0].form_template = {"id": "t1", "name": "Form A"}
    with_a = publish_review.review_fingerprint(plan)
    plan.ad_sets[0].ads[0].form_template = {"id": "t1", "name": "Form B"}
    assert publish_review.review_fingerprint(plan) != with_a, "the template dict is part of the plan"


def test_a_replaced_file_changes_the_fingerprint_but_warnings_do_not(tmp_path):
    draft = _draft(tmp_path)
    base = _fp(draft)
    draft.warnings = ["a new warning"]
    assert _fp(draft) == base, "warnings are not part of what is created"
    path = draft.ad_sets[0].ads[0].creative_path
    with open(path, "ab") as fh:
        fh.write(b"more bytes")
    assert _fp(draft) != base, "file size is part of the fingerprint"
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    after_size = _fp(draft)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 9_000_000_000))
    assert _fp(draft) != after_size, "mtime is part of the fingerprint"


def test_a_draft_that_cannot_become_a_plan_is_left_out(tmp_path):
    bad = _draft(tmp_path)
    bad.ad_sets = []
    assert publish_review.fingerprints([bad]) == {}


# ───────────────────────── разбор review_json ─────────────────────────

def _review(drafts, **extra):
    fps = publish_review.fingerprints(drafts)
    return {"v": 1, "campaigns": {n: {"fp": f, "page_id": "111", "pixel_id": "222"} for n, f in fps.items()},
            "warnings": {"media_vertical": 2}, "checked": 3, "not_checked": 0, "open_s": 41, **extra}


def test_review_json_is_parsed_and_bad_input_refused(tmp_path):
    good = publish_review.parse_review_json(json.dumps(_review([_draft(tmp_path)])))
    assert good["campaigns"]["MX Leads 1"]["pixel_id"] == "222" and good["open_s"] == 41
    for bad in ("not json", "[]", json.dumps({"v": 2, "campaigns": {"a": {"fp": "x"}}}),
                json.dumps({"v": 1, "campaigns": {}}), json.dumps({"v": 1, "campaigns": {"a": {}}})):
        with pytest.raises(publish_review.ReviewBad):
            publish_review.parse_review_json(bad)


# ───────────────────────── 15-16. замороженные черновики ─────────────────────────

class _FrozenPublisher:
    """Издатель-подделка: построение черновиков запрещено; публикация только записывает, что получила."""

    instances: list = []

    def __init__(self, **_kw):
        self.units = None
        self.reviewed = "unset"
        self.writes = 0
        self.started_profile = False
        self.stop = None
        self.cancel_event = SimpleNamespace(set=lambda: None, is_set=lambda: False)
        type(self).instances.append(self)

    def build_drafts(self, *a, **k):
        raise AssertionError("build_drafts must not run again for a reviewed job")

    def cancel(self):
        pass

    def finish_job(self, *a, **k):
        return "closed"

    def close(self, **k):
        pass


@pytest.fixture
def run_api(monkeypatch):
    seen = SimpleNamespace(pending=None, publisher=None)
    _FrozenPublisher.instances = []
    monkeypatch.setattr(publish_api, "ApiPublisher", _FrozenPublisher)
    monkeypatch.setattr(pj, "_open_with_load_requeue", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_publish_pending",
                        lambda job_id, pub, label, pending: (setattr(seen, "pending", list(pending)),
                                                            setattr(seen, "publisher", pub), pending.clear()))
    monkeypatch.setattr(pj, "_persist_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_finish", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_profile_has_other_job", lambda *a, **k: False)
    monkeypatch.setattr(pj, "_job_is_clean", lambda *a, **k: True)
    with pj._jobs_lock:
        pj._jobs["j2353"] = {"job_id": "j2353", "status": "running", "account_label": "Test", "results": [],
                             "errors": [], "campaign_names": [], "done_count": 0, "total": 0}
    yield seen
    with pj._jobs_lock:
        pj._jobs.pop("j2353", None)


def test_a_reviewed_job_uses_the_frozen_drafts_and_never_builds_again(tmp_path, run_api):
    frozen = [_draft(tmp_path)]
    review = _review(frozen)
    pj._run_api("j2353", [{"name": "x"}], account_label="Test", country="MX", frozen_drafts=frozen, review=review)
    assert run_api.pending == frozen and run_api.pending[0] is frozen[0]
    assert run_api.publisher.reviewed == review
    assert pj._jobs["j2353"]["campaign_names"] == ["MX Leads 1"]


def test_a_library_edit_after_start_does_not_reach_the_frozen_draft(tmp_path, run_api):
    """Текст в черновике уже разобран при ревью; «правка библиотеки» после старта - это правка источника,
    а черновик - собственный объект задания и от источника не зависит."""
    source = {"body": "Body as reviewed"}
    draft = _draft(tmp_path)
    draft.ad_sets[0].ads[0].ad_body = source["body"]
    source["body"] = "Body edited while the job waits"
    pj._run_api("j2353", [], account_label="Test", country="MX", frozen_drafts=[draft], review=_review([draft]))
    assert run_api.pending[0].ad_sets[0].ads[0].ad_body == "Body as reviewed"


def test_without_review_the_old_path_builds_the_drafts(tmp_path, monkeypatch, run_api):
    built = []

    class Old(_FrozenPublisher):
        def build_drafts(self, slots, *, account_label, country):
            built.append(slots)
            return [_draft(tmp_path)]

    monkeypatch.setattr(publish_api, "ApiPublisher", Old)
    pj._run_api("j2353", [{"name": "x"}], account_label="Test", country="MX")
    assert built == [[{"name": "x"}]] and run_api.publisher.reviewed is None


# ───────────────────────── 17. отказ по одной кампании ─────────────────────────

def test_check_submit_drops_only_the_changed_campaign(tmp_path):
    a, b = _draft(tmp_path), _draft(tmp_path)
    a.name = "A"
    b.name = "B"
    review = _review([a, b])
    b.ad_sets[0].ads[0].ad_title = "Edited headline"
    kept, changed = publish_review.check_submit([a, b], review)
    assert [d.name for d in kept] == ["A"] and changed == ["B"]
    kept, changed = publish_review.check_submit([a], {**review, "campaigns": {}})
    assert kept == [] and changed == ["A"], "a campaign that was not reviewed is not created"


def test_publish_draft_refuses_a_changed_plan_before_any_write(tmp_path):
    draft = _draft(tmp_path)
    pub = publish_api.ApiPublisher()
    pub.reviewed = _review([draft])
    draft.ad_sets[0].ads[0].ad_body = "Changed in the queue"          # шаблон формы / файл за время очереди
    res = pub.publish_draft(draft)
    assert res["success"] is False and res["campaign_id"] is None
    assert res["error"] == ("Not created: MX Leads 1 changed after the review (form template or file). "
                            "Nothing was created.")
    assert pub.writes == 0


# ───────────────────────── 18. Page / пиксель ─────────────────────────

def test_a_page_or_pixel_that_differs_from_the_review_stops_before_the_first_write(tmp_path):
    draft = _draft(tmp_path)
    plan = cr.plan_from_draft(draft)
    review = _review([draft])
    review["campaigns"]["MX Leads 1"]["page_id"] = "777"
    meta = FakeMeta()
    from src.fb_api import graph as g

    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None)
    creator.reviewed = review
    out = creator.run(plan)
    assert out.stop_kind == "review_stale" and "the Page changed after the review (777 → 111)" in out.error
    assert client.writes == 0 and meta.posts == []

    review["campaigns"]["MX Leads 1"].update(page_id="111", pixel_id="555")
    client = g.GraphClient(FakeMeta(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda s: None)
    creator.reviewed = review
    out = creator.run(cr.plan_from_draft(draft))
    assert "the pixel changed after the review (555 → 222)" in out.error and client.writes == 0


def test_an_empty_reviewed_id_claims_nothing(tmp_path):
    review = _review([_draft(tmp_path)])
    review["campaigns"]["MX Leads 1"].update(page_id="", pixel_id="")
    out, client = _run(FakeMeta(), _draft(tmp_path), )
    assert out.error == ""
    assert publish_review.ids_mismatch(review, "MX Leads 1", page_id="1", pixel_id="2") == ""


# ───────────────────────── 19-20. /publish-jobs ─────────────────────────

async def _post(path, data):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.post(path, data=data)


_FORM = {"user_confirmed": "1", "country": "MX", "account_label": "Test", "request_id": "r-2353"}


@pytest.fixture
def jobs_endpoint(monkeypatch, tmp_path):
    state = SimpleNamespace(started=[], builds=0, drafts=[], emitted=[])
    state.drafts = [_draft(tmp_path)]

    def build(slots, *, account_label, country):
        state.builds += 1
        return list(state.drafts)

    def start(account, country, slots, **kw):
        state.started.append(kw)
        return "job-new"

    monkeypatch.setattr("src.web.app._parse_build_upload_form",
                        lambda form: ("MX", "Test", [{"name": "MX Leads 1"}]))
    monkeypatch.setattr("src.web.app._mlx_batch_refusal", lambda form, label: None)
    monkeypatch.setattr("src.web.app.start_job", start)
    monkeypatch.setattr("src.web.app.get_job", lambda job_id: {"total": len(state.started[-1]["frozen_drafts"] or [1])
                                                               if state.started else 1})
    monkeypatch.setattr(publish_api, "build_drafts", build)
    monkeypatch.setattr(publish_api, "backend", lambda: "api")
    import src.acl_event_log as log
    monkeypatch.setattr(log, "emit", lambda name, payload=None, **kw: state.emitted.append((name, payload, kw)))
    return state


def test_matching_review_starts_the_job_with_frozen_drafts_and_builds_once(jobs_endpoint):
    review = _review(jobs_endpoint.drafts)
    res = asyncio.run(_post("/publish-jobs", {**_FORM, "review_json": json.dumps(review)}))
    assert res.status_code == 200 and res.json()["job_id"] == "job-new" and "refused" not in res.json()
    assert jobs_endpoint.builds == 1
    kw = jobs_endpoint.started[0]
    assert kw["frozen_drafts"] == jobs_endpoint.drafts and kw["reviewed"]["campaigns"]
    assert [e[0] for e in jobs_endpoint.emitted] == ["review.confirmed"]


def test_one_changed_campaign_is_refused_and_the_rest_runs(jobs_endpoint, tmp_path):
    a, b = _draft(tmp_path), _draft(tmp_path)
    a.name, b.name = "A", "B"
    review = _review([a, b])
    b.ad_sets[0].ads[0].ad_body = "library text changed"
    jobs_endpoint.drafts = [a, b]
    res = asyncio.run(_post("/publish-jobs", {**_FORM, "review_json": json.dumps(review)}))
    body = res.json()
    assert res.status_code == 200 and body["refused"] == [{"name": "B", "code": "review_stale"}]
    assert body["refused_text"].startswith("Not created: B changed after the review")
    assert [d.name for d in jobs_endpoint.started[0]["frozen_drafts"]] == ["A"]
    assert [e[0] for e in jobs_endpoint.emitted] == ["review.refused", "review.confirmed"]


def test_all_changed_gives_409_and_no_job(jobs_endpoint):
    review = _review(jobs_endpoint.drafts)
    jobs_endpoint.drafts[0].link = "https://example.invalid/new"
    res = asyncio.run(_post("/publish-jobs", {**_FORM, "review_json": json.dumps(review)}))
    assert res.status_code == 409 and res.json()["code"] == "review_stale"
    assert res.json()["changed"] == ["MX Leads 1"] and jobs_endpoint.started == []


def test_review_json_with_the_rpa_backend_is_refused(jobs_endpoint, monkeypatch):
    monkeypatch.setattr(publish_api, "backend", lambda: "rpa")
    res = asyncio.run(_post("/publish-jobs", {**_FORM, "review_json": json.dumps(_review(jobs_endpoint.drafts))}))
    assert res.status_code == 409 and "Nothing was created" in res.json()["error"]
    assert jobs_endpoint.started == [] and jobs_endpoint.builds == 0


def test_a_broken_review_json_is_refused_with_400(jobs_endpoint):
    res = asyncio.run(_post("/publish-jobs", {**_FORM, "review_json": "{broken"}))
    assert res.status_code == 400 and jobs_endpoint.started == []


def test_without_review_json_the_old_road_runs_with_reviewed_false(jobs_endpoint):
    res = asyncio.run(_post("/publish-jobs", dict(_FORM)))
    assert res.status_code == 200
    kw = jobs_endpoint.started[0]
    assert kw["reviewed"] is None and kw["frozen_drafts"] is None and jobs_endpoint.builds == 0


def test_a_resend_with_the_same_request_id_returns_the_same_job(monkeypatch, tmp_path):
    monkeypatch.setattr(pj, "_start_job", lambda *a, **k: "job-" + os.urandom(3).hex())
    first = pj.start_job("Test", "MX", [], request_id="same-key", reviewed={"campaigns": {}}, frozen_drafts=[])
    with pj._jobs_lock:
        pj._jobs[first] = {"job_id": first}
    try:
        assert pj.start_job("Test", "MX", [], request_id="same-key", reviewed={"campaigns": {}},
                            frozen_drafts=[]) == first
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(first, None)


def test_the_job_record_says_whether_it_was_reviewed():
    from src.web import publish_history

    assert publish_history.entry_from_job({"job_id": "x", "reviewed": True})["reviewed"] is True
    assert publish_history.entry_from_job({"job_id": "x"})["reviewed"] is False


# ───────────────────────── 21. события ─────────────────────────

def test_ui_events_carry_only_counters_and_a_whitelisted_reason():
    assert ev.review_ui_payload("review.opened", {"accounts": 3, "campaigns": "4", "link": "https://x/?t=1"}) == {
        "accounts": 3, "campaigns": 4}
    closed = ev.review_ui_payload("review.closed", {"reason": "https://secret", "open_s": 41, "text": "body"})
    assert closed == {"reason": "cancel", "open_s": 41}
    assert ev.review_ui_payload("job.created", {"x": 1}) is None
    assert ev.review_ui_payload("review.opened", "nope") is None


def test_server_events_hold_no_link_and_no_text(tmp_path):
    draft = _draft(tmp_path)
    review = _review([draft])
    payloads = [ev.review_confirmed_payload(review, publish_review.fingerprints([draft])),
                ev.review_refused_payload("publish", ["MX Leads 1"])]
    blob = json.dumps(payloads)
    assert "example.invalid" not in blob and "Body 0" not in blob and "Head 0" not in blob


def test_event_route_accepts_only_the_two_names(monkeypatch):
    got = []
    import src.acl_event_log as log
    monkeypatch.setattr(log, "emit", lambda name, payload=None, **kw: got.append((name, payload)))

    async def post(body):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.post("/api/publish-review/event", json=body)

    assert asyncio.run(post({"event": "review.opened", "accounts": 2, "campaigns": 5})).status_code == 200
    assert asyncio.run(post({"event": "job.created"})).status_code == 400
    assert got == [("review.opened", {"accounts": 2, "campaigns": 5})]
