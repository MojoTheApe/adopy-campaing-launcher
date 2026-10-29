"""ACL-2316: a tab that stops answering at 60-80 % CPU (below the busy line) is waited for on the Check plan open
path (account_spend) and on the publish path; one replacement tab; a write is never resent. Fakes only."""
import types

import pytest

from src.analysis_agent import account_spend as sp
from src.analysis_agent import account_spend_api as api
from src.fb_api import token_capture as tc
from src.fb_api import warm
from src.fb_api.graph import PageUnresponsive
from tests.test_acl1760_api_read import ACCOUNT as API_ACCOUNT, World
from tests.test_acl2299_tabs_and_busy_freeze import _graph, _rig
from tests.test_acl2299_round2 import _Pub, _drafts, jobrig  # noqa: F401

T0 = 1_800_000_000.0


# ---- Check plan open / read through account_spend (not the warm registry) ----

@pytest.fixture
def plan_world(monkeypatch, tmp_path):
    w = World(monkeypatch, tmp_path)
    w.local = ("none", None)
    w.started_now = True
    w.rig = _rig(tmp_path / "rig", 62.0)      # below the busy line: the 2299 busy wait does not apply
    w.rig.warm_up()
    monkeypatch.setattr(api, "_warm_registry", lambda: w.rig.reg)
    w.rows = []
    monkeypatch.setattr(sp, "_record", lambda event, ok, dur, diag=None: w.rows.append((event, diag or {})))
    w.beats = []
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "beat", lambda *a, **k: w.beats.append(w.rig.now[0] - T0))
    w.graphs = []
    w.freezes = [10_000.0]        # per graph: frozen until this many seconds on the fake clock

    def make(tab):
        until = w.freezes[len(w.graphs)] if len(w.graphs) < len(w.freezes) else 0.0
        graph = tc.InPageGraph(tab, sleep=w.rig._sleep_fake)
        real = graph.wait_ready

        def wait_ready(timeout=90.0):
            if w.rig.now[0] - T0 < until:
                w.rig.now[0] += 3.0                  # the tab burns its own timeout
                raise PageUnresponsive("the Ads Manager tab stopped answering")
            return real(timeout=timeout)

        graph.wait_ready = wait_ready
        w.graphs.append(graph)
        return graph

    monkeypatch.setattr(api, "_make_graph", lambda tab: make(tab))
    return w


def _read(w, patient=True):
    if patient:
        with warm.read_patience(), api._session(API_ACCOUNT) as sess:
            return sess
    with api._session(API_ACCOUNT) as sess:
        return sess


def test_check_plan_tab_frozen_once_on_an_idle_machine_waits_takes_one_replacement_and_finishes(plan_world):
    w = plan_world
    sess = _read(w)
    assert sess is not None and len(w.graphs) == 2, "one replacement tab"
    waited = w.rig.now[0] - T0
    assert 75.0 <= waited < 100.0, "the idle wait (75 s), then the replacement"
    assert w.beats, "the wait beats the silence watcher: no step_stuck"
    assert max(b2 - b1 for b1, b2 in zip(w.beats, w.beats[1:])) <= 10.0


def test_check_plan_second_freeze_stops_with_the_current_error_and_logs_the_readings(plan_world):
    w = plan_world
    w.freezes = [10_000.0, 10_000.0]
    with pytest.raises(PageUnresponsive) as stop:
        _read(w)
    assert len(w.graphs) == 2, "one replacement only"
    load = stop.value.tab_load
    assert load["cpu_pct"] == 62.0 and load["busy"] is False and "memory_pct" in load
    assert "page_count_before" in load and "page_count_after" in load
    row = [d for e, d in w.rows if e == "spend_profile_kept_for_problem"][0]
    assert row["problem"] == "page_unresponsive" and row["cpu_pct"] == 62.0 and "busy" in row
    # the next read is paused, as before
    with pytest.raises(api.ApiReadError, match="stopped answering"):
        _read(w)


def test_a_freeze_that_clears_during_the_wait_needs_no_replacement(plan_world):
    w = plan_world
    w.freezes = [40.0]
    _read(w)
    assert len(w.graphs) == 1 and 40.0 <= w.rig.now[0] - T0 < 60.0


def test_a_plain_background_read_keeps_todays_no_wait_behaviour(plan_world):
    w = plan_world
    with pytest.raises(PageUnresponsive):
        _read(w, patient=False)
    assert len(w.graphs) == 1 and w.rig.now[0] - T0 < 10.0


# ---- publish path ----

def test_publish_freeze_on_an_idle_machine_waits_the_short_limit_then_continues(tmp_path):
    rig = _rig(tmp_path, 62.0)
    graph = _graph(rig, writes=True, freeze_until=60.0)
    graph._ready_for_call()
    assert 60.0 <= rig.now[0] - T0 < 75.0


def test_publish_freeze_on_an_idle_machine_stops_after_75s_with_the_readings(tmp_path):
    rig = _rig(tmp_path, 62.0)
    graph = _graph(rig, writes=True, freeze_until=10_000.0)
    with pytest.raises(PageUnresponsive) as stop:
        graph._ready_for_call()
    assert 75.0 <= rig.now[0] - T0 < 90.0
    load = stop.value.tab_load
    assert load["waited_patiently"] and load["waited_s"] >= 75.0
    assert load["cpu_pct"] == 62.0 and load["busy"] is False and "memory_pct" in load


def test_idle_waits_of_one_lease_never_add_up_to_more_than_one(tmp_path):
    rig = _rig(tmp_path, 62.0)
    graph = _graph(rig, writes=True, freeze_until=10_000.0)
    for _ in range(3):
        with pytest.raises(PageUnresponsive):
            graph._ready_for_call()
    assert rig.now[0] - T0 < 100.0


def test_publish_freeze_with_nothing_written_takes_one_replacement_and_continues(jobrig):
    pj, job_id, waits = jobrig
    pub = _Pub(["ok", "freeze", "ok", "ok"])
    pj._publish_pending(job_id, pub, "B97", _drafts("A", "B", "C"))
    assert [r["campaign_name"] for r in pj._jobs[job_id]["results"]] == ["A", "B", "C"]
    assert pub.opens == 1 and waits == []


def test_publish_freeze_after_a_write_reconciles_first_nothing_is_replaced_or_resent(jobrig):
    pj, job_id, waits = jobrig
    pub = _Pub(["write_then_freeze", "stopped"])
    pj._publish_pending(job_id, pub, "B97", _drafts("A", "B"))
    assert pub.opens == 0 and pub.dropped == [] and waits == []
    from src.web import publish_api
    probe = types.SimpleNamespace(stop=PageUnresponsive("x"), writes=1, blocked="",
                                  _client=types.SimpleNamespace(unreconciled={"op": "create"}))
    probe.stop.tab_load = {"waited_patiently": True}
    assert publish_api.ApiPublisher.frozen_before_write(probe, 1) is False, "an unreconciled write is never replaced"


def test_a_frozen_replacement_on_an_idle_machine_keeps_the_2262_pause(jobrig):
    pj, job_id, waits = jobrig
    pub = _Pub(["freeze_idle_machine", "freeze_idle_machine", "stopped"])
    orig = pub.publish_draft

    def publish(draft):
        if pub.script[0] != "freeze_idle_machine":
            return orig(draft)
        pub.script.pop(0)
        res = pub.api.result_dict(pub.api.Outcome(campaign_name=draft.name), error="stopped (page_unresponsive)")
        pub.stop = PageUnresponsive("silent")
        pub.stop.tab_load = {"waited_patiently": True, "busy": False, "cpu_pct": 62.0}
        return res

    pub.publish_draft = publish
    pj._publish_pending(job_id, pub, "B97", _drafts("A", "B"))
    assert pub.opens == 1 and waits == [], "one replacement, no requeue"
    assert pub.dropped == [False], "the pause is never cleared"
    results = pj._jobs[job_id]["results"]
    assert results[0]["error"] and results[1]["not_started"] is True
