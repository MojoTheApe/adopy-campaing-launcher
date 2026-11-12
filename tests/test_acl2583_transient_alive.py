"""ACL-2583: a transient read error on the journal's campaign never counts as "campaign gone"."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import name_receipt as nr  # noqa: E402
from src.fb_api import operation_journal as oj  # noqa: E402
from tests.test_acl1761_api_create import ACT, _draft  # noqa: E402
from tests.test_acl1856_operation_journal import (  # noqa: E402,F401
    _by_kind, _count, _creator, _delete_campaign_tree, _go, _partial, journal, clock)


def _inject_read_error(meta, monkeypatch, camp_id, body):
    real = meta.request

    def request(method, path, params=None):
        # only the journal's liveness read (id,name,status,effective_status), not the other reads of the campaign
        if (method == "GET" and path.strip("/").split("/")[-1] == camp_id
                and (params or {}).get("fields") == "id,name,status,effective_status"):
            return {"status": 500, "body": {"error": body}}
        return real(method, path, params)
    monkeypatch.setattr(meta, "request", request)


def _left_over(tmp_path, journal, clock, monkeypatch):
    """A journal entry with the campaign, no name receipt, the name search misses it, the lag window is over."""
    meta, _first = _partial(tmp_path, journal)
    nr.delete(ACT, "MX Leads 1")
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])
    clock.t += 11 * 60
    return meta, _by_kind(meta, "campaign")["id"]


def _inject_failures(meta, monkeypatch, camp_id, body, times):
    """The journal's liveness read fails `times` times (None = always); returns the count of reads."""
    real = meta.request
    left = {"n": times}
    seen = {"reads": 0}

    def request(method, path, params=None):
        if (method == "GET" and path.strip("/").split("/")[-1] == camp_id
                and (params or {}).get("fields") == "id,name,status,effective_status"):
            seen["reads"] += 1
            if left["n"] is None or left["n"] > 0:
                if left["n"] is not None:
                    left["n"] -= 1
                return {"status": 500, "body": {"error": body}}
        return real(method, path, params)
    monkeypatch.setattr(meta, "request", request)
    return seen


def _go_recording(meta, draft, journal):
    creator, client = _creator(meta, journal)
    sleeps = []
    creator.sleep = sleeps.append
    return creator.run(cr.plan_from_draft(draft)), client, sleeps


BODIES = [
    {"code": 2, "message": "Service temporarily unavailable"},
    {"code": 1, "message": "An unknown error occurred"},
    {"code": 100, "message": "Invalid parameter"},                   # code 100 without subcode 33 is not "gone"
]


@pytest.mark.parametrize("body", BODIES)
def test_persistent_read_failure_continues_as_a_new_operation_with_a_copy_name(
        tmp_path, journal, clock, monkeypatch, body):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    old_name = meta.objects[camp]["name"]
    seen = _inject_failures(meta, monkeypatch, camp, body, None)
    out, client, sleeps = _go_recording(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "" and not out.stop_kind, (out.stop_kind, out.error)
    assert seen["reads"] == len(cr.TRANSIENT_READ_WAITS_S) + 1       # one try and the retries, no busy loop
    assert 25 <= sum(sleeps) <= 35, sleeps                           # about 30 s, through the injected sleep
    assert out.campaign_id != camp and out.published_as == old_name + " copy 1"
    assert meta.objects[out.campaign_id]["name"] == old_name + " copy 1"
    assert meta.objects[camp]["name"] == old_name, "the earlier campaign is left untouched"
    assert len(list(journal.root.glob("op-*.superseded"))) == 1
    assert any("continues as a new campaign" in w for w in out.warnings)
    assert "few minutes" not in " ".join([out.error, *out.warnings])


def test_the_same_name_is_never_reused(tmp_path, journal, clock, monkeypatch):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    old_name = meta.objects[camp]["name"]
    _inject_failures(meta, monkeypatch, camp, BODIES[0], None)
    first, _c, _s = _go_recording(meta, _draft(tmp_path, ads=2), journal)
    assert first.error == "" and first.published_as == old_name + " copy 1"
    names = [o["name"] for o in meta.objects.values() if o["_kind"] == "campaign"]
    assert sorted(names) == sorted([old_name, old_name + " copy 1"]) and len(set(names)) == len(names)


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_transient_failure_then_success_resumes_the_same_campaign(tmp_path, journal, clock, monkeypatch, failures):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    _inject_failures(meta, monkeypatch, camp, BODIES[0], failures)
    out, _c, sleeps = _go_recording(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "", out.error
    assert out.campaign_id == camp and _count(meta, "campaign") == 1
    assert " copy " not in out.published_as
    assert len(sleeps) >= failures * 2                               # sliced pauses, all injected
    assert not list(journal.root.glob("op-*.superseded"))
    assert out.operation["state"] == oj.OP_COMPLETE


@pytest.mark.parametrize("code", [4, 17])
def test_rate_limit_on_the_read_stays_a_stop_without_a_write(tmp_path, journal, clock, monkeypatch, code):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    _inject_read_error(meta, monkeypatch, camp, {"code": code, "message": "request limit reached"})
    posts = len(meta.posts)
    out, client = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.stop_kind == "rate_limited" and len(meta.posts) == posts and client.writes == 0
    assert not list(journal.root.glob("op-*.superseded"))


def test_does_not_exist_answer_starts_a_new_operation(tmp_path, journal, clock, monkeypatch):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    _delete_campaign_tree(meta, camp)                   # Meta answers code 100 / subcode 33 for the read by id
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "", out.error
    assert out.campaign_id != camp
    assert len(list(journal.root.glob("op-*.superseded"))) == 1


@pytest.mark.parametrize("status", ["DELETED", "ARCHIVED"])
def test_deleted_or_archived_starts_a_new_operation(tmp_path, journal, clock, monkeypatch, status):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    meta.objects[camp]["status"] = status
    meta.objects[camp]["effective_status"] = status
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "" and out.campaign_id != camp
    assert len(list(journal.root.glob("op-*.superseded"))) == 1


def test_alive_campaign_is_resumed(tmp_path, journal, clock, monkeypatch):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "", out.error
    assert out.campaign_id == camp and _count(meta, "campaign") == 1
    assert out.operation["state"] == oj.OP_COMPLETE


def test_each_retry_read_is_added_to_the_call_budget(tmp_path, journal, clock, monkeypatch):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    _inject_failures(meta, monkeypatch, camp, BODIES[0], 2)          # two failures, then the real answer
    creator, client = _creator(meta, journal)
    creator.sleep = lambda s: None
    before = client.max_calls
    row = creator._get_retrying(f"/{camp}", {"fields": "id,name,status,effective_status"})
    assert row.get("id") == camp
    assert client.max_calls == before + 2
