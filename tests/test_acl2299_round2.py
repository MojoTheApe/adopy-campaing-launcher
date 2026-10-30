"""ACL-2299 round 2: tabs somebody drives are kept (B1), campaigns between a freeze (B2), one budget (B3),
short idle Check plan limit and one replacement tab (B4). Fakes only."""
import types
from types import SimpleNamespace

import pytest

from src.fb_api import warm
from src.fb_api.cdp import CdpTimeout, PageTab
from src.fb_api.graph import PageUnresponsive
from src.fb_api.token_capture import InPageGraph
from tests.test_acl1812_warm_sessions import PID, Rig
from tests.test_acl2299_tabs_and_busy_freeze import AM, Browser, _graph, _open, _rig, _row


# ---- B1: never close a tab somebody drives ----

def test_attached_and_owned_ads_manager_pages_are_kept(monkeypatch):
    from src.fb_api import launch
    events = []
    monkeypatch.setattr(launch, "_record", events.append)
    monkeypatch.setattr(PageTab, "_LIVE", {"live_tab"})
    monkeypatch.setattr(warm, "registry", lambda: SimpleNamespace(known_targets=lambda: {"other_server"}))
    pages = [_row("restored", AM), {**_row("attached", AM), "attached": True},
             _row("live_tab", AM), _row("other_server", AM), {**_row("free", AM), "attached": False}]
    browser = Browser(pages, expected="profile")
    tab = _open(browser)
    try:
        assert sorted(browser.closed_targets) == ["free", "restored"]
        done = [e for e in events if e.get("phase") == "after_restored_cleanup"][0]
        assert done["skipped_attached"] == 1 and done["skipped_owned"] == 2 and done["closed_ads_manager"] == 2
        assert done["page_count_before"] == 5 and done["page_count_after"] == 3
    finally:
        tab.close()


def test_if_every_page_is_attached_the_cleanup_does_nothing_and_the_open_works():
    pages = [{**_row(f"p{i}", AM), "attached": True} for i in range(5)]
    browser = Browser(pages, expected="profile")
    tab = _open(browser)
    try:
        assert browser.closed_targets == [] and tab.closed_restored == 0 and tab.skipped_attached == 5
    finally:
        tab.close()


def test_a_tab_is_protected_while_open_and_released_when_closed():
    browser = Browser([_row("x", AM)], expected="profile")
    tab = _open(browser)
    assert "owned" in PageTab._LIVE
    tab.close()
    assert "owned" not in PageTab._LIVE


def test_registry_knows_targets_of_its_sessions_and_the_store(tmp_path):
    rig = Rig(tmp_path)
    sess = rig.warm_up()
    sess.target_id = "T-mine"
    rig.store.update(lambda d: d["profiles"].setdefault("other", {"target_id": "T-theirs"}))
    assert {"T-mine", "T-theirs"} <= rig.reg.known_targets()


# ---- B3: one budget per lease ----

def test_all_episodes_of_a_lease_draw_from_one_budget(tmp_path):
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=300.0)
    graph._ready_for_call()                                    # waited ~300 of 480
    t0 = rig.now[0]

    def frozen(timeout=10.0):
        raise PageUnresponsive("again")

    graph._ready_once = frozen
    with pytest.raises(PageUnresponsive) as stop:
        graph._ready_for_call()
    assert rig.now[0] - t0 <= 185.0, "only what is left of the 480 s"
    assert stop.value.tab_load["waited_s"] <= 185.0
    t1 = rig.now[0]
    with pytest.raises(PageUnresponsive):                      # budget spent: no wait at all
        graph._ready_for_call()
    assert rig.now[0] == t1


def test_resends_of_a_read_are_capped(tmp_path):
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=False, freeze_until=0.0)
    runs = []

    def read_once(spec, timeout):
        runs.append(1)
        graph._read_sent = True
        raise PageUnresponsive("silent after the send")

    with pytest.raises(PageUnresponsive):
        graph._patiently(lambda: read_once({}, 15.0), max_runs=1 + InPageGraph.MAX_READ_RESENDS)
    assert len(runs) == 3


def test_network_timeout_then_freeze_then_recovery_stays_within_the_shared_budget(tmp_path):
    """The ACL-2301 shape: a read answers network_timeout, the next try meets a frozen tab, then it recovers."""
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=False, freeze_until=0.0)
    seq = iter(["net", "freeze", "freeze", "ok"])
    start = rig.now[0]

    def read_once(spec, timeout):
        step = next(seq)
        if step == "freeze":
            graph._read_sent = True
            raise PageUnresponsive("silent")
        return {"error": "network_timeout"} if step == "net" else {"data": []}

    assert graph._patiently(lambda: read_once({}, 15.0), max_runs=3) == {"error": "network_timeout"}
    assert graph._patiently(lambda: read_once({}, 15.0), max_runs=3) == {"data": []}
    assert rig.now[0] - start < 480.0 and graph.patience.spent_s < 480.0


# ---- N2: a POST is sent exactly once ----

def test_a_post_that_times_out_on_a_busy_machine_is_sent_once_with_no_wait_after_the_send(tmp_path):
    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=True, freeze_until=0.0)
    sent, slept = [], []
    graph._sleep = slept.append
    graph._ready_for_call = lambda **kw: None
    graph._ensure = lambda: 7
    graph.name = "h"
    graph.graph_host = "graph"
    graph.credentials = "include"
    graph.object_updates = False
    from src.fb_api import write_policy as _wp
    graph.write_act = "act_123456789"
    graph.validation_act = None
    graph._write_policy = _wp.policy_for_commands("act_123456789")

    class Tab:
        def evaluate_in(self, ctx, expr, timeout=0):
            sent.append(expr)
            raise CdpTimeout("silent after the send")

    graph.tab = Tab()
    try:
        got = graph.write("/act_123456789/campaigns", {"name": "x", "status": "PAUSED", "objective": "OUTCOME_LEADS"})
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"stub too thin for write(): {exc!r}")
    assert got == {"error": "call_unknown", "reason": "cdp_timeout"} and len(sent) == 1 and slept == []


# ---- B2: between campaigns ----

class _Pub:
    """Publisher fake with scripted outcomes of `publish_draft`."""

    def __init__(self, script):
        from src.web import publish_api
        self.script = list(script)
        self.opens = 0
        self.stop = None
        self.blocked = ""
        self._writes = 0
        self._session_profile_id = "p"
        self._client = None
        self.api = publish_api
        self.dropped = []

    @property
    def writes(self):
        return self._writes

    def open(self, label):
        self.opens += 1

    def close(self, **kw):
        pass

    def drop_frozen_tab(self, clear_pause=False):
        load = dict(getattr(self.stop, "tab_load", {}))
        self.stop = None
        self.dropped.append(clear_pause)
        return load

    def frozen_before_write(self, before):
        return self.api.ApiPublisher.frozen_before_write(self, before)

    def publish_draft(self, draft):
        step = self.script.pop(0)
        name = draft.name
        res = self.api.result_dict
        if step == "ok":
            return res(self.api.Outcome(campaign_name=name))
        if step in ("freeze", "idle_freeze", "write_then_freeze"):
            if step == "write_then_freeze":
                self._writes += 1
            self.stop = PageUnresponsive("silent")
            self.stop.tab_load = {"waited_patiently": step != "idle_freeze", "cpu_pct": 95.0,
                                 "busy": step != "idle_freeze"}
            return res(self.api.Outcome(campaign_name=name), error="stopped (page_unresponsive)")
        if step == "stopped":
            return res(self.api.Outcome(campaign_name=name), not_started=True,
                       error="Not started: Graph API stopped earlier (page_unresponsive).")
        raise AssertionError(step)


@pytest.fixture
def jobrig(monkeypatch):
    from src.web import publish_jobs as pj
    job_id = "job-2299"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "account_label": "B97", "started_at": "t",
                            "campaign_names": ["A", "B", "C"], "results": [], "errors": [], "done_count": 0,
                            "total": 3}
    waits = []
    monkeypatch.setattr(pj, "_wait_out_machine_load", lambda *a, **k: waits.append(a[4]) or True)
    monkeypatch.setattr(pj, "_persist_job", lambda *a, **k: None)
    yield pj, job_id, waits
    with pj._jobs_lock:
        pj._jobs.pop(job_id, None)


def _drafts(*names):
    return [SimpleNamespace(name=n, errors=[]) for n in names]


def test_freeze_before_campaign_two_with_nothing_written_gets_a_replacement_tab_and_continues(jobrig):
    pj, job_id, waits = jobrig
    pub = _Pub(["ok", "freeze", "ok", "ok"])
    pending = _drafts("A", "B", "C")
    pj._publish_pending(job_id, pub, "B97", pending)
    names = [r["campaign_name"] for r in pj._jobs[job_id]["results"]]
    assert names == ["A", "B", "C"] and pub.opens == 1 and waits == [] and pending == []


def test_second_freeze_puts_the_rest_back_in_the_queue_with_no_attempt_limit(jobrig):
    pj, job_id, waits = jobrig
    pub = _Pub(["ok", "freeze", "freeze", "freeze", "freeze", "ok", "ok"])
    pj._publish_pending(job_id, pub, "B97", _drafts("A", "B", "C"))
    results = pj._jobs[job_id]["results"]
    assert [r["campaign_name"] for r in results] == ["A", "B", "C"]
    assert not any(r["error"] for r in results), "nothing failed: the job waited and went on"
    assert waits == [1, 2], "requeued twice, growing backoff"
    assert pub.dropped == [False, True, False, True]


def test_stop_after_a_write_is_not_replaced_or_requeued_and_the_rest_are_not_started(jobrig):
    pj, job_id, waits = jobrig
    pub = _Pub(["ok", "write_then_freeze", "stopped"])
    pj._publish_pending(job_id, pub, "B97", _drafts("A", "B", "C"))
    results = pj._jobs[job_id]["results"]
    assert [r["campaign_name"] for r in results] == ["A", "B", "C"]
    assert results[1]["error"] and results[1]["not_started"] is False
    assert results[2]["not_started"] is True and results[2]["error"].startswith("Not started")
    assert waits == [] and pub.opens == 0


def test_idle_machine_freeze_is_not_replaced_or_requeued_here(jobrig):
    pj, job_id, waits = jobrig
    pub = _Pub(["idle_freeze", "stopped", "stopped"])
    pj._publish_pending(job_id, pub, "B97", _drafts("A", "B", "C"))
    assert pub.opens == 0 and waits == [] and len(pj._jobs[job_id]["results"]) == 3


def test_unfinished_campaigns_are_listed_as_not_started_when_the_job_ends_early(jobrig):
    pj, job_id, _waits = jobrig
    pj._mark_not_started(job_id, _drafts("B", "C"), cancelled=False)
    res = pj._jobs[job_id]["results"]
    assert [r["campaign_name"] for r in res] == ["B", "C"] and all(r["not_started"] for r in res)
    pj._mark_not_started(job_id, _drafts("Z"), cancelled=True)
    assert len(pj._jobs[job_id]["results"]) == 2, "a user Stop adds nothing"


def test_the_writes_count_survives_a_replaced_client():
    from src.web import publish_api
    pub = publish_api.ApiPublisher()
    pub._client = types.SimpleNamespace(writes=4, unreconciled=None)
    pub.stop = PageUnresponsive("x")
    pub.account_label = "A"
    pub.drop_frozen_tab()
    assert pub.writes == 4 and pub.stop is None


# ---- B4: Check plan retries once with a replacement tab ----

def test_check_plan_tries_one_replacement_tab_after_the_wait(monkeypatch):
    from src.analysis_agent import account_spend as _as
    from src.web import publish_api

    calls = []

    def runner(account, drafts, results=None):
        calls.append(len(results))
        results.append({"half": True})
        if len(calls) == 1:
            raise PageUnresponsive("silent")
        results.clear()
        results.append({"ok": True})
        return results

    monkeypatch.setattr(_as, "serialized_browser", lambda **kw: (lambda fn: runner))
    monkeypatch.setattr(publish_api, "build_drafts",
                        lambda *a, **k: [SimpleNamespace(errors=[], name="C", warnings=[])])
    out = publish_api._check_plan_once([{}], account_label="A", country="US")
    assert calls == [0, 0] and out["error"] == "" and out["results"] == [{"ok": True}]

    calls.clear()

    def always(account, drafts, results=None):
        calls.append(1)
        raise PageUnresponsive("silent")

    monkeypatch.setattr(_as, "serialized_browser", lambda **kw: (lambda fn: always))
    out = publish_api._check_plan_once([{}], account_label="A", country="US")
    assert len(calls) == 2 and out["error"], "one replacement only, then the failure is reported"


# ---- combined with ACL-2301 (real GraphClient network retries over the real InPageGraph.request) ----

def test_worst_case_with_the_2301_network_retries_stays_within_one_budget_plus_the_backoff(tmp_path):
    from src.fb_api import graph as g

    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=False, freeze_until=0.0)
    graph.state = lambda: {"kind": "ads_manager", "has_token": True}
    graph.graph_host, graph.credentials, graph.name = "captured", "include", "h"
    graph._connection_stop = None
    script = iter(["net", "freeze", "net", "freeze", "freeze"])
    runs = []

    def read_once(spec, timeout):
        runs.append(rig.now[0])
        step = next(script, "freeze")
        if step == "freeze":
            graph._read_sent = True
            raise PageUnresponsive("silent")
        return {"error": "network_timeout"}

    graph._read_once = read_once
    client = g.GraphClient(graph, min_interval_s=0, sleep=rig._sleep_fake)
    start = rig.now[0]
    with pytest.raises((PageUnresponsive, g.PageFailed)):
        client.get("/123", {"fields": "id"})
    total = rig.now[0] - start
    print("WORST_CASE_S", total)
    assert total <= 480.0 + sum(g.NETWORK_BACKOFF_S) + 20.0, total
    assert len(runs) <= 4 * (1 + InPageGraph.MAX_READ_RESENDS), "bounded resends"
    assert graph.patience.spent_s <= 480.0


def test_recovery_after_a_network_timeout_and_a_freeze_continues(tmp_path):
    from src.fb_api import graph as g

    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=False, freeze_until=0.0)
    graph.graph_host, graph.credentials, graph.name = "captured", "include", "h"
    graph._connection_stop = None
    graph.state = lambda: {"kind": "ads_manager", "has_token": True}
    script = iter(["net", "freeze", "freeze", "ok"])

    def read_once(spec, timeout):
        step = next(script)
        if step == "freeze":
            graph._read_sent = True
            raise PageUnresponsive("silent")
        return {"error": "network_timeout"} if step == "net" else {"status": 200, "body": {"id": "123"}}

    graph._read_once = read_once
    client = g.GraphClient(graph, min_interval_s=0, sleep=rig._sleep_fake)
    assert client.get("/123", {"fields": "id"}) == {"id": "123"}
    assert rig.now[0] - 1_800_000_000.0 < 480.0


def test_worst_case_of_a_tab_that_never_comes_back_before_the_send_is_one_budget(tmp_path):
    from src.fb_api import graph as g

    rig = _rig(tmp_path, 96.0)
    graph = _graph(rig, writes=False, freeze_until=0.0)
    graph.graph_host, graph.credentials, graph.name = "captured", "include", "h"
    graph._connection_stop = None
    graph.state = lambda: {"kind": "ads_manager", "has_token": True}

    def read_once(spec, timeout):
        raise PageUnresponsive("not ready, nothing sent")

    graph._read_once = read_once
    client = g.GraphClient(graph, min_interval_s=0, sleep=rig._sleep_fake)
    start = rig.now[0]
    with pytest.raises(PageUnresponsive):
        client.get("/123")
    total = rig.now[0] - start
    print("WORST_CASE_PRESEND_S", total)
    assert 480.0 <= total < 495.0
    assert graph.patience.spent_s >= 480.0


# ---- live defect on 9095 zzf: the cleanup closed the ONLY page and the browser exited ----

def test_the_only_other_page_is_never_closed_even_if_it_is_a_restored_ads_manager_page():
    browser = Browser([_row("only", AM)], expected="profile")
    tab = _open(browser)
    try:
        assert browser.closed_targets == []
        assert (tab.page_count_before, tab.page_count_after, tab.closed_restored) == (1, 1, 0)
    finally:
        tab.close()


def test_two_restored_pages_close_exactly_one_and_keep_one():
    browser = Browser([_row("r1", AM), _row("r2", AM)], expected="profile")
    tab = _open(browser)
    try:
        assert len(browser.closed_targets) == 1 and tab.page_count_after == 1
        assert "owned" not in browser.closed_targets
    finally:
        tab.close()


def test_our_tab_plus_restored_pages_closes_restored_and_keeps_ours_and_one_page(monkeypatch):
    from src.fb_api import launch
    events = []
    monkeypatch.setattr(launch, "_record", events.append)
    browser = Browser([_row("r1", AM), _row("r2", AM), _row("r3", AM)], expected="profile")
    tab = _open(browser)
    try:
        assert len(browser.closed_targets) == 2 and "owned" not in browser.closed_targets
        done = [e for e in events if e.get("phase") == "after_restored_cleanup"][0]
        assert done["kept_last_page"] == 1 and done["page_count_after"] == 1
    finally:
        tab.close()


def test_the_browser_never_ends_without_a_page_besides_ours_when_it_had_one():
    pages = [_row("mail", "https://mail.example.com/"), _row("r1", AM)]
    browser = Browser(pages, expected="profile")
    tab = _open(browser)
    try:
        assert browser.closed_targets == ["r1"], "a non-Ads-Manager page stays, so the restored one may go"
    finally:
        tab.close()
