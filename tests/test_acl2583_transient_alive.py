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
    Crashy, _by_kind, _count, _creator, _delete_campaign_tree, _go, _partial, journal, clock)


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


def test_unknown_campaign_write_is_never_rewritten_under_the_same_name(tmp_path, journal, clock, monkeypatch):
    """The campaign POST went through, the run died, and the name search lags: the rerun a minute later must not create
    a second campaign with the same name. It continues as a new operation under "<name> copy 1"."""
    meta = Crashy()
    meta.crash_at, meta.phase = 1, "after"
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert meta.posts[-1][0].endswith("/campaigns")
    assert [s["state"] for s in first.operation["stages"] if s["label"] == "campaign"] == [oj.SENT]
    meta.crash_at = None
    # the name search is exactly what lags behind Meta
    monkeypatch.setattr(cr.CampaignCreator, "_campaign_rows", lambda self, act, name: [])
    monkeypatch.setattr(cr.CampaignCreator, "_find_campaign", lambda self, plan, after_unknown=False: None)
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])
    creator, _client = _creator(meta, journal)
    creator.sleep = lambda s: setattr(clock, "t", clock.t + s)
    clock.t += 60
    events = []
    monkeypatch.setattr(cr.CampaignCreator, "_journal_event", staticmethod(lambda e, **d: events.append((e, d))))
    again = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert again.error == "", again.error
    names = sorted(o["name"] for o in meta.objects.values() if o["_kind"] == "campaign")
    assert len(names) == 2 and names[0] != names[1]
    assert names[1] == f"{names[0]} copy 1"
    assert again.published_as == names[1] and again.copy_number == 1
    assert any(e == "unknown_rewritten" and d.get("renamed_to") == names[1] for e, d in events)
    assert any("duplicate" in w and "PAUSED" in w and names[0] in w for w in again.warnings)
    text = " ".join(again.warnings + list(getattr(again, "steps", []) or []))
    assert "Wait" not in text and "few minutes" not in text
    assert len(list(journal.root.glob("op-*.superseded"))) == 1


def _lagging_search(monkeypatch):
    monkeypatch.setattr(cr.CampaignCreator, "_campaign_rows", lambda self, act, name: [])
    monkeypatch.setattr(cr.CampaignCreator, "_find_campaign", lambda self, plan, after_unknown=False: None)
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])


def test_settled_not_found_campaign_stage_is_never_sent_under_the_same_name(tmp_path, journal, clock, monkeypatch):
    """Run 1: the campaign POST lands but the answer is lost and the readback finds nothing (stage unknown/not_found).
    The rerun must continue as "<name> copy 1", not write the same name again."""
    meta = Crashy()
    meta.fail_next["campaigns"] = "lost"
    _lagging_search(monkeypatch)
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert first.stop_kind == "unknown"
    saved = " ".join(f.read_text(encoding="utf-8") for f in journal.root.glob("op-*.json"))
    assert '"not_found"' in saved          # the stage is settled as not found
    creator, _client = _creator(meta, journal)
    creator.sleep = lambda s: setattr(clock, "t", clock.t + s)
    clock.t += 60
    again = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert again.error == "", again.error
    names = sorted(o["name"] for o in meta.objects.values() if o["_kind"] == "campaign")
    assert len(names) == 2 and names[1] == f"{names[0]} copy 1"
    assert any("duplicate" in w and "PAUSED" in w for w in again.warnings)


class _AnswerLost(Crashy):
    """The first campaign POST is created at Meta, the answer never arrives (network_timeout)."""
    hits = 0

    def write(self, path, params, files=None, filename="", **kw):
        body = super().write(path, params, files, filename, **kw)
        if path.endswith("/campaigns") and self.hits == 0:
            self.hits += 1
            return {"error": "network_timeout"}
        return body


def test_in_run_resend_of_the_campaign_goes_out_under_the_next_copy_name(tmp_path, journal, clock, monkeypatch):
    meta = _AnswerLost()
    _lagging_search(monkeypatch)
    out, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert out.error == "", out.error
    names = sorted(o["name"] for o in meta.objects.values() if o["_kind"] == "campaign")
    assert len(names) == 2 and names[1] == f"{names[0]} copy 1"
    assert out.published_as == names[1] and out.copy_number == 1
    assert any("duplicate" in w and "PAUSED" in w for w in out.warnings)


@pytest.mark.parametrize("suffix", ["_105", "_105_105", "_1_2_3"])
def test_stored_image_name_accepts_repeated_suffixes(suffix):
    assert cr.stored_image_name("acl-aa5e593c3fc5d36f.png", "acl-aa5e593c3fc5d36f.png" + suffix) == "acl-aa5e593c3fc5d36f.png"


def test_stored_image_name_still_flags_a_different_image():
    other = "acl-bbbbbbbbbbbbbbbb.png_105"
    assert cr.stored_image_name("acl-aa5e593c3fc5d36f.png", other) == other
