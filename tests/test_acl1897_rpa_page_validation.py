"""The RPA profile fallback uses the existing guarded no-creation transport."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.fb_api import graph
from src.web import account_pages


ACT = "act_1234567890"


def _validation(monkeypatch, *, reply=None, session_act=ACT, allow_writes=False):
    from src.analysis_agent import account_spend as accounts, account_spend_api as api

    calls, sessions, locks = [], [], []

    class Transport:
        def validate_page(self, act, page_id, link):
            calls.append((act, page_id, link))
            return reply if reply is not None else {"status": 200, "body": {"success": True}}

        def write(self, *a, **kw):
            pytest.fail("ordinary writes must never be used for Page proof")

    client = graph.GraphClient(Transport(), allow_writes=allow_writes, min_interval_s=0,
                               sleep=lambda _: None)

    @contextmanager
    def session(account):
        sessions.append(account)
        yield SimpleNamespace(client=client, act=session_act)

    def serialized(**kw):
        locks.append(kw)
        return lambda fn: fn

    monkeypatch.setattr(api, "_session", session)
    monkeypatch.setattr(accounts, "serialized_browser", serialized)
    return client, calls, sessions, locks


def _check():
    return account_pages.validate_profile_page("Demo", act=ACT, page_id="777",
        page_name="Profile Page", link="https://example.com/")


def test_profile_validation_is_bound_to_the_actual_account_page_and_link_without_writes(monkeypatch):
    client, calls, sessions, locks = _validation(monkeypatch)
    assert _check() == ""
    assert calls == [(ACT, "777", "https://example.com/")]
    assert sessions == ["Demo"] and locks[0]["reading"] is True
    assert client.allow_writes is False and client.writes == 0 and client.calls == 1


@pytest.mark.parametrize("body", [{}, {"success": False}, {"success": "true"},
    {"id": "123"}, {"success": True, "id": "123"}, {"success": True, "error": None},
    {"error": {"code": 100, "message": "This Page is refused for the account"}}])
def test_unknown_or_refused_proof_names_the_page_and_never_creates(monkeypatch, body):
    client, calls, _, _ = _validation(monkeypatch, reply={"status": 200, "body": body})
    error = _check()
    assert "Profile Page" in error and "777" in error and "Nothing was created" in error
    assert len(calls) == 1 and client.writes == 0
    if "error" in body and body["error"]:
        assert "This Page is refused for the account" in error


@pytest.mark.parametrize("session_act,allow_writes", [("act_1234567890123456", False), (ACT, True)])
def test_changed_account_or_enabled_ordinary_writes_refuses_before_the_validation_call(monkeypatch, session_act, allow_writes):
    client, calls, _, _ = _validation(monkeypatch, session_act=session_act, allow_writes=allow_writes)
    error = _check()
    assert error and "Nothing was created" in error
    assert calls == [] and client.writes == 0


def test_a_later_operation_obtains_new_proof_instead_of_reusing_ui_cache(monkeypatch):
    client, calls, sessions, _ = _validation(monkeypatch)
    assert _check() == "" and _check() == ""
    assert len(calls) == 2 and sessions == ["Demo", "Demo"] and client.writes == 0


def test_an_unresolved_ordinary_write_remains_a_validation_fence(monkeypatch):
    client, calls, _, _ = _validation(monkeypatch)
    client.unreconciled = {"operation": "synthetic unresolved write"}
    error = _check()
    assert "unresolved earlier write" in error and "Nothing was created" in error
    assert calls == [] and client.writes == 0


def test_actual_job_cancellation_is_not_converted_to_a_page_error(monkeypatch):
    from src.analysis_agent import account_spend_api as api
    from src.fb_ads_rpa.job_cancel import JobCancelled
    _validation(monkeypatch)

    @contextmanager
    def stopped(account):
        raise JobCancelled()
        yield  # pragma: no cover - context manager never enters

    monkeypatch.setattr(api, "_session", stopped)
    with pytest.raises(JobCancelled):
        _check()
