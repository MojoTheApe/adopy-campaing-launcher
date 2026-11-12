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
    _by_kind, _count, _delete_campaign_tree, _go, _partial, journal, clock)


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
    clock.t += oj.NOT_LISTED_LAG_S + 60
    return meta, _by_kind(meta, "campaign")["id"]


@pytest.mark.parametrize("body", [
    {"code": 2, "message": "Service temporarily unavailable"},
    {"code": 1, "message": "An unknown error occurred"},
    {"code": 100, "message": "Invalid parameter"},                   # code 100 without subcode 33 is not "gone"
])
def test_transient_read_error_stops_without_a_write_or_new_operation(tmp_path, journal, clock, monkeypatch, body):
    meta, camp = _left_over(tmp_path, journal, clock, monkeypatch)
    _inject_read_error(meta, monkeypatch, camp, body)
    posts = len(meta.posts)
    out, client = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.stop_kind == "name_unsure" and "Meta did not answer" in out.error, (out.stop_kind, out.error)
    assert "publish again in a few minutes" in out.error
    assert len(meta.posts) == posts and client.writes == 0
    assert _count(meta, "campaign") == 1
    assert not list(journal.root.glob("op-*.superseded")), "no new operation was started"


@pytest.mark.parametrize("code", [4, 17])
def test_rate_limit_on_the_read_also_stops_without_a_write(tmp_path, journal, clock, monkeypatch, code):
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
