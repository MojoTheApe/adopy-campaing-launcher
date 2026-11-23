"""ACL-2791: the preview of a command and its execute share one light tab and one token hand-over.

Fakes only: no Meta, no Multilogin, no CDP.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend_api as api
from tests.test_acl2344_warm_light import LightGraph, rig  # noqa: F401  (the light-mode rig fixture)
from tests.test_acl1812_warm_sessions import ACT, PID

CMD = f"commands:{ACT}"


def test_execute_after_preview_reuses_the_light_tab_and_the_token(rig):  # noqa: F811
    rig.checkout(mode=CMD).release()                  # preview: first lease, one capture tab + one hand-over
    tabs_after_preview = len(rig.tabs)
    graph = rig.last_graph
    lease = rig.checkout(mode=CMD)                    # execute: same mode, same account
    assert len(rig.tabs) == tabs_after_preview        # no new capture tab
    assert lease.graph is graph and lease.graph.received == 1   # the token is not handed over again
    assert sum(t.navigations for t in rig.tabs) == 1  # Ads Manager was loaded once, for the preview
    lease.release()


def test_lost_light_tab_gives_execute_one_fresh_lease_as_today(rig):  # noqa: F811
    rig.checkout(mode=CMD).release()
    sess = rig.reg.session(PID)
    rig.reg._drop_tab(sess)                           # the tab was lost while the confirm dialog was open
    before = len(rig.tabs)
    lease = rig.checkout(mode=CMD)
    assert len(rig.tabs) == before + 2                # one capture tab + one light work tab, as at a first lease
    assert rig.reg.session(PID).mode_kind == "light"
    lease.release()


def test_warm_preview_asks_for_the_execute_mode_but_stays_without_writes(monkeypatch):
    seen = {}

    class Reg:
        def checkout(self, profile_id, *, account, act, mode, make_graph, **kw):
            seen["mode"] = mode
            seen["graph"] = make_graph(object())
            return SimpleNamespace(graph=object(), release=lambda stop=None: None)

    monkeypatch.setattr(api, "_warm_registry", lambda: Reg())
    monkeypatch.setattr(api, "_make_graph", lambda tab, commands_act=None, photo=False: ("graph", commands_act))
    monkeypatch.setattr(api, "GraphClient", lambda graph, **kw: SimpleNamespace(allow_writes=kw["allow_writes"],
                                                                                  stopped=None, calls=0))
    for commands, preview, writes in ((False, True, False), (True, False, True), (False, False, False)):
        seen.clear()
        with api._warm_session_held("Acc", ACT, "k", PID, commands, preview=preview) as sess:
            assert sess.client.allow_writes is writes
        want_cmd = commands or preview
        assert seen["mode"] == (CMD if want_cmd else "read")
        assert seen["graph"] == (("graph", ACT) if want_cmd else ("graph", None))


def test_command_preview_opens_its_session_in_preview_mode_and_execute_does_not(monkeypatch):
    from src.web import campaign_commands as cc

    calls = []

    @contextmanager
    def fake_session(account, **kw):
        calls.append(kw)
        yield "sess"

    @contextmanager
    def nothing(*a, **k):
        yield

    import src.analysis_agent.account_spend as _as
    import src.fb_ads_rpa.operations as ops
    import src.fb_api.connection as conn

    monkeypatch.setattr(api, "_session", fake_session)
    monkeypatch.setattr(_as, "_profile_key", lambda account: account)
    monkeypatch.setattr(ops, "profile_lock", nothing)
    monkeypatch.setattr(ops.browser_operation, "reading", nothing)
    monkeypatch.setattr(conn, "user_started_action", nothing)
    with cc.open_session("Acc", False):
        pass
    with cc.open_session("Acc", True):
        pass
    assert calls == [{"commands": False, "preview": True}, {"commands": True}]
