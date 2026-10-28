"""ACL-2272: a closed profile is opened only by an action the person started.

Owner rule 2026-10-28: page load, background and 08/13/18 reads never start a closed Multilogin profile.
ACL-2262 covered the warm path; this covers the COLD read path (`_open_gated`, RPA `_attach_only`) and the
callers that reach it. Nothing real is touched: profile, MLX and Meta are the fakes of the ACL-1760 rig.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_refresh as ar
from src.analysis_agent import account_spend as sp
from src.analysis_agent import account_spend_api as api
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_state import RefreshStore
from src.fb_api import connection
from tests.test_acl1760_api_read import ACCOUNT, D, World  # noqa: F401  (fixture rig)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """The ACL-1760 rig, but with the real rule: only a user-started action may start a closed profile."""
    real = connection.may_start_closed_profile
    rig = World(monkeypatch, tmp_path)
    monkeypatch.setattr(connection, "may_start_closed_profile", real)
    return rig


def _closed(w, kind="none"):
    w.local = (kind, None)
    w.started_now = True


def _assert_nothing_started(w):
    assert w.opened_profiles == []
    assert w.queue.calls == []           # no memory slot was asked for either
    assert w.soft_stops == []
    assert w.page.specs == []            # nothing was sent to Meta


# ───────────────────────── automatic reads leave a closed profile alone ─────────────────────────

@pytest.mark.parametrize("kind", ["none", "closing"])
def test_automatic_api_read_of_a_closed_profile_starts_nothing(world, kind):
    _closed(world, kind)
    out = api.read_via_api(ACCOUNT, D, D)
    assert out.state == "failed" and out.error_code == sp.PROFILE_CLOSED_CODE
    assert "profile is closed" in out.error and "nothing was read" in out.error
    _assert_nothing_started(world)


def test_automatic_country_split_and_inventory_do_not_start_it_either(world):
    _closed(world)
    split = api.read_country_split_via_api(ACCOUNT, D, D)
    assert split.state == "failed" and split.error_code == sp.PROFILE_CLOSED_CODE
    snap = ci.read_via_api(ACCOUNT, structure_only=True, trigger="schedule:2026-10-28/08:00")
    assert snap.state == "failed" and snap.error_code == sp.PROFILE_CLOSED_CODE
    _assert_nothing_started(world)


def test_an_already_open_profile_is_still_read_and_left_open(world):
    world.local = ("port", 9222)
    out = api.read_via_api(ACCOUNT, D, D)
    assert out.state == "ok", out.error
    assert world.soft_stops == []


def test_user_started_read_opens_a_closed_profile_and_closes_what_it_opened(world):
    _closed(world)
    with connection.user_started_action():
        out = api.read_via_api(ACCOUNT, D, D)
    assert out.state == "ok", out.error
    assert len(world.opened_profiles) == 1


def test_check_plan_flag_counts_as_a_user_action():
    assert connection.may_start_closed_profile() is False
    with connection.opening_closed_profile():
        assert connection.may_start_closed_profile() is True
    assert connection.may_start_closed_profile() is False


# ───────────────────────── refresh triggers ─────────────────────────

@pytest.mark.parametrize("trigger,opens", [
    ("schedule:2026-10-28/08:00", False), ("attention", False), ("retry", True), ("action", True)])
def test_refresh_trigger_decides_whether_a_closed_profile_may_start(world, monkeypatch, trigger, opens):
    _closed(world)
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci, "get_snapshot", lambda a: None)
    monkeypatch.setattr(ar, "is_fresh", lambda *a, **k: False)
    snap = ci._run_read(ACCOUNT, trigger=trigger, persist=False)
    if opens:
        assert len(world.opened_profiles) == 1
    else:
        assert snap.state == "failed" and snap.error_code == sp.PROFILE_CLOSED_CODE
        _assert_nothing_started(world)


def test_page_open_refresh_of_a_dashboard_with_closed_profiles_starts_none(world, monkeypatch, tmp_path):
    """Page open queues every dashboard account; none of the closed profiles is started; last data stays."""
    _closed(world)
    monkeypatch.setenv("ACL_AUTO_REFRESH", "1")
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "inventory.json")
    old = ci.Snapshot(account=ACCOUNT, state="ok", finished_at="yesterday",
                      finished_at_utc="2026-10-27T08:00:00+00:00", currency="USD")
    old.steps = {step: "ok" for step in ci.STRUCTURE_STEPS}
    ci.put_result(old)
    now = datetime(2026, 10, 28, 8, tzinfo=timezone.utc).timestamp()
    q = ar.RefreshQueue(lambda account, trigger: ci._run_read(account, trigger=trigger, persist=True),
                        clock=lambda: now, store=RefreshStore(tmp_path), accounts=lambda: [ACCOUNT])
    assert q.page_open(now=datetime(2026, 10, 28, 8), port=9095)["queued"] == 1
    q._drain_once()
    _assert_nothing_started(world)
    row = q.state()["rows"][ACCOUNT]
    assert row["state"] == "failed" and "profile is closed" in row["error"]     # clear note on the row
    assert ci.get_snapshot(ACCOUNT).finished_at == "yesterday"                   # stale list is kept


# ───────────────────────── user-started actions and the flag ─────────────────────────

def test_user_started_thread_keeps_the_flag_and_automatic_one_does_not(monkeypatch):
    seen = []

    def reader(account, a, b):
        seen.append(connection.may_start_closed_profile())
        return sp.SpendReading(account=account, date_from=a, date_to=b, state="ok")

    monkeypatch.setattr(sp, "_lock_for", lambda account: threading.RLock())
    sp._RUNNING.clear()
    with connection.user_started_action():
        sp.start_read("Flag1", "2026-10-01", "2026-10-01", reader=reader)
    _join("spend:Flag1")
    sp._RUNNING.clear()
    sp.start_read("Flag2", "2026-10-01", "2026-10-01", reader=reader)
    _join("spend:Flag2")
    assert seen == [True, False]


def _join(name):
    for t in threading.enumerate():
        if t.name == name:
            t.join(5)


class _Recorded(Exception):
    pass


def _record_flag_in_session(monkeypatch):
    seen = []

    @contextmanager
    def fake_session(account, *, commands=False):
        seen.append(connection.may_start_closed_profile())
        raise _Recorded()
        yield  # pragma: no cover

    monkeypatch.setattr(api, "_session", fake_session)
    return seen


def test_check_plan_session_is_a_user_started_path(monkeypatch):
    from src.web import publish_api

    seen = _record_flag_in_session(monkeypatch)
    with pytest.raises(_Recorded):
        publish_api._plan_in_read_session("Acc", [])
    assert seen == [True]


def test_campaign_commands_and_page_reads_are_user_started(monkeypatch):
    from src.web import campaign_commands

    seen = _record_flag_in_session(monkeypatch)
    monkeypatch.setattr(sp, "_profile_key", lambda account: account)
    monkeypatch.setattr("src.fb_ads_rpa.operations.profile_lock", lambda key: _null())
    monkeypatch.setattr("src.fb_ads_rpa.operations.browser_operation.reading", lambda: _null())
    with pytest.raises(_Recorded):
        with campaign_commands.open_session("Acc", False):
            pass
    assert seen == [True]
    seen.clear()
    monkeypatch.setattr("src.web.account_pages.cached_pages", lambda account: None)
    from src.web import account_pages

    monkeypatch.setattr(sp, "serialized_browser", lambda **kw: (lambda fn: fn))
    result = account_pages.read_pages("Acc", force=True)      # the Page control / Refresh pages button
    assert result["ok"] is False and seen == [True]


@contextmanager
def _null():
    yield


def test_turn_off_command_is_user_started(monkeypatch):
    seen = []

    def locked(*a, **k):
        seen.append(connection.may_start_closed_profile())
        return SimpleNamespace(state="failed")

    monkeypatch.setattr(api, "_turn_off_locked", locked)
    api.turn_off("Acc", "campaign", "120200000000001", expected_name="x")
    assert seen == [True]


@pytest.mark.parametrize("route,target,args", [
    ("/account-spend/read", ("src.analysis_agent.account_spend", "start_read"), {}),
    ("/account-spend/read-countries", ("src.analysis_agent.country_split", "start_split"), {}),
    ("/campaign-inventory/read", ("src.analysis_agent.campaign_inventory", "start_read"), {}),
])
def test_read_buttons_are_user_started(monkeypatch, route, target, args):
    from fastapi.testclient import TestClient

    from src.web import app as web

    seen = []
    module = __import__(target[0], fromlist=["x"])

    def start(*a, **k):
        seen.append(connection.may_start_closed_profile())
        return SimpleNamespace(to_dict=lambda: {"state": "running"}) if "split" in route or "spend" in route \
            else {"started": True}

    monkeypatch.setattr(module, target[1], start)
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(web, "_mlx_batch_refusal", lambda form, account: None)
    client = TestClient(web.app, base_url="http://127.0.0.1:9095")
    client.post(route, data={"account": "Acc", "date_from": "2026-10-01", "date_to": "2026-10-01"})
    assert seen == [True]


# ───────────────────────── RPA (non-API) read path ─────────────────────────

def test_rpa_attach_only_refuses_a_closed_profile_without_a_user_action(monkeypatch, tmp_path):
    from src.fb_ads_rpa import browser as br
    from src.fb_ads_rpa import profile_lease

    class Direct:
        def attach_existing_only(self, *a, **k):
            return None

        def profile_is_running(self, *a, **k):
            return False

    def boom(*a, **k):
        raise AssertionError("a closed profile must not be started")

    monkeypatch.setattr(profile_lease, "require_profile_lease", lambda pid: None)
    monkeypatch.setattr(br, "MultiloginDirectCDPProvider", Direct)
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(sp, "_dir_spellings", lambda p: [str(p)])
    monkeypatch.setattr(sp, "_start_closed", boom)
    entry = SimpleNamespace(profile_id="p1", folder_id="")
    with pytest.raises(sp.ProfileNotAttachable) as exc:
        with sp.opening_closed_profile(sp._ReadOpen("Acc")):
            sp._attach_only(entry, "Acc")
    assert exc.value.code == sp.PROFILE_CLOSED_CODE

    started = []
    monkeypatch.setattr(sp, "_start_closed", lambda *a, **k: started.append(1) or "conn")
    with connection.user_started_action(), sp.opening_closed_profile(sp._ReadOpen("Acc")):
        assert sp._attach_only(entry, "Acc") == "conn"
    assert started == [1]
