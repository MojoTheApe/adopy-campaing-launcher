"""ACL-2301: a Graph read that times out is retried per call; a write is resent only if it did not land.

Fakes only: no Meta, no Multilogin, no real waits.
"""
import pytest

from src.fb_ads_rpa import run_telemetry as rt
from src.fb_api import graph as g
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run

OK = {"status": 200, "body": {"id": "123"}}
TIMEOUT = {"error": "network_timeout"}


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.reads = 0

    def request(self, method, path, params):
        assert method == "GET"
        self.reads += 1
        return self.replies.pop(0) if self.replies else OK

    def state(self):
        return {"kind": "ads_manager", "has_token": True}


def _client(replies, sleeps=None):
    t = Transport(replies)
    c = g.GraphClient(t, min_interval_s=0, sleep=(sleeps.append if sleeps is not None else (lambda _: None)))
    return c, t


@pytest.mark.parametrize("fails", [1, 2, 3])
def test_get_timeout_then_success_continues(fails):
    sleeps = []
    c, t = _client([TIMEOUT] * fails + [OK], sleeps)
    assert c.get("/123", {"fields": "id"}) == {"id": "123"}
    assert t.reads == fails + 1 and c.writes == 0
    assert sum(sleeps) == sum(g.NETWORK_BACKOFF_S[:fails])


def test_three_get_timeouts_still_stop_with_the_same_message_after_backoff_5_10_20():
    sleeps = []
    c, t = _client([TIMEOUT] * 4, sleeps)
    with pytest.raises(g.PageFailed, match=r"the page could not call Graph API \(network_timeout\)"):
        c.get("/123")
    assert t.reads == 4 and sum(sleeps) == 35
    assert isinstance(c.stopped, g.PageFailed)


def test_early_blip_does_not_consume_the_retry_of_a_later_get():
    c, t = _client([TIMEOUT, OK, TIMEOUT, TIMEOUT, OK])
    assert c.get("/1") == {"id": "123"}
    assert c.get("/2") == {"id": "123"}
    assert t.reads == 5 and c.network_retries == 3


def test_session_cap_still_stops_a_flapping_connection(monkeypatch):
    monkeypatch.setattr(g, "NETWORK_RETRIES_PER_SESSION", 2)
    c, t = _client([TIMEOUT, OK, TIMEOUT, OK, TIMEOUT, OK])
    c.get("/1")
    c.get("/2")
    with pytest.raises(g.PageFailed):
        c.get("/3")


def test_graph_error_codes_are_not_retried():
    c, t = _client([{"status": 400, "body": {"error": {"code": 100, "message": "bad"}}}])
    with pytest.raises(g.GraphError):
        c.get("/1")
    assert t.reads == 1


def test_no_step_stuck_during_the_backoff(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(rt, "_clock", lambda: now["t"])
    rt.set_current_job("acl2301-job")
    rt._touch_job("acl2301-job", "api_create_step")
    idle = []

    def sleep(secs):
        idle.append(now["t"] - rt.last_activity("acl2301-job")[0])
        now["t"] += secs

    monkeypatch.setattr(rt, "record", lambda *a, **k: None)
    try:
        c, t = _client([TIMEOUT] * 3 + [OK])
        c._sleep = sleep
        c.get("/1")
        assert idle and max(idle) <= g.BACKOFF_SLICE_S
        assert now["t"] - 1000.0 == 35
    finally:
        rt.set_current_job("")
        rt.forget_job("acl2301-job")


def test_retry_is_logged_in_telemetry(monkeypatch):
    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw.get("diag"))))
    c, t = _client([TIMEOUT, TIMEOUT, OK])
    c.get("/act_123456789/ads")
    assert [r[0] for r in rows] == ["graph_retry", "graph_retry"]
    assert rows[1][1]["attempt"] == 2 and rows[1][1]["error"] == "network_timeout"
    assert "123456789" not in str(rows)


# -- writes -------------------------------------------------------------------------------------
class NetMeta(FakeMeta):
    """`net_lost`: created, reply lost to a timeout. `net_unsent`: not created, timeout."""

    def write(self, path, params, files=None, filename="", **kw):
        edge = path.rsplit("/", 1)[1]
        mode = self.fail_next.get(edge)
        if mode in ("net_lost", "net_unsent"):
            self.fail_next[edge] = "lost" if mode == "net_lost" else "unsent"
            super().write(path, params, files=files, filename=filename, **kw)
            return {"error": "network_timeout"}
        return super().write(path, params, files=files, filename=filename, **kw)


def _edges(meta, edge):
    return [p for p, _ in meta.posts if p.endswith("/" + edge)]


def test_post_timeout_where_the_object_exists_is_not_resent(tmp_path):
    meta = NetMeta()
    meta.fail_next["ads"] = "net_lost"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 1
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 1


def test_post_timeout_where_the_object_is_missing_is_resent_once(tmp_path):
    meta = NetMeta()
    meta.fail_next["campaigns"] = "net_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "campaigns")) == 2
    assert len([o for o in meta.objects.values() if o["_kind"] == "campaign"]) == 1


def test_second_timeout_after_resend_stops_without_a_third_post(tmp_path):
    class Twice(NetMeta):
        def write(self, path, params, **kw):
            if path.endswith("/campaigns"):
                self.fail_next["campaigns"] = "net_unsent"
            return super().write(path, params, **kw)

    meta = Twice()
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert len(_edges(meta, "campaigns")) == 2


def test_unknown_result_without_network_error_is_never_resent(tmp_path):
    meta = FakeMeta()
    meta.fail_next["campaigns"] = "unsent"          # call_unknown: Meta may have it
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert len(_edges(meta, "campaigns")) == 1
