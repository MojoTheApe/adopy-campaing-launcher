"""ACL-2344: the light work tab - origin-only interception, marker, hand-over. Offline: local Chromium behind a
dead proxy and local HTTP servers only; nothing goes to Meta."""
import http.server
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from src.fb_api import light_tab
from src.fb_api.cdp import CdpError, PageIdentityError, PageTab
from src.fb_api.token_capture import InPageGraph
from tests.test_acl2344_light_probe import _chromium


@pytest.fixture()
def browser():
    """Local Chromium behind a dead proxy. Local Network Access checks are off ONLY here: the light document is
    served by DevTools (no address space), and a test 'Graph' server on 127.0.0.1 would otherwise be a
    public-to-local request; the real Graph host is public."""
    exe = _chromium()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    args = [exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={tempfile.mkdtemp()}",
            "--proxy-server=http://127.0.0.1:9", "--no-first-run", "--disable-gpu",
            "--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests,"
            "PrivateNetworkAccessSendPreflights"]
    if sys.platform.startswith("linux"):  # linux-ok: CI runners are root, Chromium needs --no-sandbox
        args.append("--no-sandbox")
    proc = subprocess.Popen(args + ["about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                urllib.request.urlopen(url + "/json/version", timeout=2)
                break
            except Exception:  # noqa: BLE001
                if time.monotonic() > deadline or proc.poll() is not None:
                    pytest.fail("Chromium never answered /json/version")
                time.sleep(0.2)
        yield url
    finally:
        proc.kill()

TOKEN = "EAA" + "x1Y2" * 8


class _Server:
    """A local HTTP server that counts every request it gets."""

    def __init__(self):
        self.hits = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()


@pytest.fixture()
def servers(monkeypatch):
    page, graph = _Server(), _Server()
    monkeypatch.setattr(light_tab, "LIGHT_ORIGIN", page.origin)
    monkeypatch.setattr(light_tab, "LIGHT_URL", page.origin + light_tab.LIGHT_PATH)
    yield page, graph
    page.close()
    graph.close()


def _eval(tab, expr):
    got = tab.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True},
                   timeout=60.0)
    return (got.get("result") or {}).get("value")


def test_origin_requests_never_leave_and_other_hosts_still_do(browser, servers):  # noqa: F811
    page, graph = servers
    tab = PageTab.open(browser, call_s=60.0)
    try:
        doc, reason = light_tab.open_light_document(tab)
        assert reason == "" and doc is not None and doc.fulfilled == 1
        assert doc.document_intact() == ""
        ctx = tab.create_isolated_world("t")
        statuses = tab.evaluate_in(ctx, """Promise.allSettled([
            fetch('/favicon.ico'), fetch('/anything?x=1'), fetch(%s)
        ]).then(r => r.map(x => x.status))""" % json.dumps(graph.origin + "/v19.0/me"))
        assert statuses == ["rejected", "rejected", "fulfilled"]
        assert page.hits == []                    # the page origin got nothing: no favicon, no fetch, no document
        assert graph.hits == ["/v19.0/me"]        # the "Graph" host is NOT intercepted: it reaches the network
        assert doc.blocked >= 2
        # a reload is answered locally again - and noticed as a replaced document (the token would be gone)
        tab.call("Page.reload", {}, timeout=30.0)
        for _ in range(50):
            if doc.fulfilled == 2:
                break
            tab.pump(0.2)
        assert doc.fulfilled == 2 and page.hits == []
        assert doc.document_intact() == "light_doc_replaced"
        with pytest.raises(CdpError):
            tab.reload()                          # our own code never reloads the light tab
    finally:
        light_tab.settle_and_close(tab)


def test_interception_survives_a_reattach(browser, servers):  # noqa: F811
    page, _ = servers
    tab = PageTab.open(browser, call_s=60.0)
    try:
        doc, reason = light_tab.open_light_document(tab)
        assert reason == ""
        tab.recover_session(light_tab.LIGHT_URL)     # new CDP session: interception is enabled again on it
        ctx = tab.create_isolated_world("t2")
        assert tab.evaluate_in(ctx, "fetch('/x').then(() => 'ok', () => 'blocked')") == "blocked"
        assert page.hits == []
    finally:
        light_tab.settle_and_close(tab)


def test_document_check_and_identity():
    PageTab._check_url(light_tab.LIGHT_URL, light_tab.LIGHT_URL)
    for actual, expected in ((light_tab.LIGHT_URL + "?x", light_tab.LIGHT_URL),
                             ("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1", light_tab.LIGHT_URL),
                             (light_tab.LIGHT_URL, "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1")):
        with pytest.raises(PageIdentityError):
            PageTab._check_url(actual, expected)
    with pytest.raises(PageIdentityError) as err:
        PageTab._check_url("https://www.facebook.com/checkpoint/1", light_tab.LIGHT_URL)
    assert err.value.kind == "checkpoint"


def _light_pair(browser, monkeypatch):  # noqa: F811
    """Two light tabs; the 'capture' one has a fake token in its HTML (scripts never run: CSP)."""
    work = PageTab.open(browser, call_s=60.0)
    capture = PageTab.open(browser, call_s=60.0)
    assert light_tab.open_light_document(work)[1] == ""
    plain = light_tab._body
    monkeypatch.setattr(light_tab, "_body", lambda m: plain(m) + (
        '<script>var c={"accessToken":"%s"};</script>' % TOKEN).encode())
    assert light_tab.open_light_document(capture)[1] == ""
    monkeypatch.setattr(light_tab, "_body", plain)
    ig_c, ig_w = InPageGraph(capture), InPageGraph(work)
    assert ig_c._eval_helper(".probe()")["has_token"] is True
    return work, capture, ig_w, ig_c


def test_handover_token_never_in_python(browser, monkeypatch):  # noqa: F811
    work, capture, ig_w, ig_c = _light_pair(browser, monkeypatch)
    try:
        st = ig_w.state()
        assert st["has_token"] is False and st["light"] is True and st["kind"] == "ads_manager"
        assert ig_w.receive_from(ig_c) == ""
        got = ig_w.state()
        assert got["has_token"] is True and got["source"] == "handover"
        assert ig_w._eval_helper(".listenStatus()") == "received"
        assert TOKEN not in repr(got) and TOKEN not in repr(ig_w.__dict__) and TOKEN not in repr(work.light.__dict__)
        # a second listen on a world that has a token is refused; the old channel is closed
        assert ig_w._eval_helper('.listen("a"+"b".repeat(20),"c".repeat(20),"d".repeat(20),2000)') == "has_token"
        reply = ig_w.request("GET", "/me/adaccounts", {"fields": "id"}, timeout=5)
        assert reply.get("error") in ("network", "network_timeout") and TOKEN not in repr(reply)
    finally:
        for t in (capture, work):
            light_tab.settle_and_close(t)


def test_wrong_nonce_ignored_bad_payload_closes_and_ttl_expires(browser, monkeypatch):  # noqa: F811
    work, capture, ig_w, ig_c = _light_pair(browser, monkeypatch)
    try:
        n = lambda c: json.dumps(c * 22)  # noqa: E731
        args = ",".join((n("a"), n("b"), n("c")))
        assert ig_w._eval_helper(f".listen({args},5000)") == "listening"
        # wrong nonce: ignored, no ack, the receiver keeps listening
        assert ig_c._eval_helper(f".handOver({n('a')},{n('z')},{n('c')},800)", timeout=20) == "no_ack"
        assert ig_w._eval_helper(".listenStatus()") == "listening"
        # right nonce, but a foreign host from a forged sender: rejected and the channel closes
        cctx = capture.create_isolated_world("forger")
        capture.evaluate_in(cctx, "new BroadcastChannel(%s).postMessage({n: %s, t: %s, h: 'evil.example.com', v: ''})"
                            % (n("a"), n("b"), json.dumps(TOKEN)))
        for _ in range(20):
            if ig_w._eval_helper(".listenStatus()") != "listening":
                break
            work.pump(0.1)
        assert ig_w._eval_helper(".listenStatus()") == "rejected"
        assert ig_w.state()["has_token"] is False
        # the real sender with the same nonce comes too late: nobody listens any more
        assert ig_c._eval_helper(f".handOver({args},800)", timeout=20) == "no_ack"
        assert ig_w.state()["has_token"] is False
        # a fresh listen expires by itself
        args2 = ",".join((n("d"), n("e"), n("f")))
        assert ig_w._eval_helper(f".listen({args2},1000)") == "listening"
        for _ in range(30):
            if ig_w._eval_helper(".listenStatus()") == "expired":
                break
            work.pump(0.1)
        assert ig_w._eval_helper(".listenStatus()") == "expired"
        # retire closes a listening channel
        args3 = ",".join((n("g"), n("h"), n("i")))
        assert ig_w._eval_helper(f".listen({args3},5000)") == "listening"
        assert ig_w._eval_helper(".retire()") is True
        assert ig_w._eval_helper(".listenStatus()") == "retired"
        assert ig_c._eval_helper(f".handOver({args3},800)", timeout=20) == "no_ack"
    finally:
        for t in (capture, work):
            light_tab.settle_and_close(t)


def test_pin_page_hands_over_world_to_world_without_loading(browser, monkeypatch):  # noqa: F811
    work, capture, _, ig_c = _light_pair(browser, monkeypatch)
    try:
        ig_w = InPageGraph(work, allow_writes=True, write_act="act_123456")
        ig_w.bind_act("act_123456")
        assert ig_w.receive_from(ig_c) == ""
        old_world = ig_w.world_name
        calls = []
        ig_w.light_recapture = lambda g, t: calls.append(1) or {"has_token": False}
        got = ig_w.pin_page("1234567")
        assert got.get("has_token") is True and got.get("pinned") is True
        assert ig_w.world_name != old_world and calls == []           # no re-capture, no Ads Manager load
        old_ctx = work.create_isolated_world(old_world)
        assert work.evaluate_in(old_ctx, "globalThis[%s].state().retired" % json.dumps(ig_w.name)) is True
    finally:
        for t in (capture, work):
            light_tab.settle_and_close(t)


def test_reacquire_on_light_tab_recaptures_and_never_reloads(browser):  # noqa: F811
    work = PageTab.open(browser, call_s=60.0)
    try:
        assert light_tab.open_light_document(work)[1] == ""
        ig = InPageGraph(work)
        assert ig.state()["has_token"] is False
        seen = []
        ig.light_recapture = lambda g, t: seen.append(g.world_name) or {"kind": "login", "has_token": False}
        got = ig.reacquire(timeout=5)
        assert got["kind"] == "login" and len(seen) == 1
        assert work.light.fulfilled == 1                                 # the tab was not reloaded
    finally:
        light_tab.settle_and_close(work)


def test_call_on_lost_token_recaptures_once_then_stops(browser):  # noqa: F811
    from src.fb_api.graph import PageFailed

    work = PageTab.open(browser, call_s=60.0)
    try:
        assert light_tab.open_light_document(work)[1] == ""
        ig = InPageGraph(work)
        n = []
        ig.light_recapture = lambda g, t: n.append(1) or {"kind": "ads_manager", "has_token": False}
        with pytest.raises(PageFailed):
            ig.request("GET", "/me", {"fields": "id"}, timeout=5)
        assert n == [1]                                                   # one Ads Manager load, no loop
    finally:
        light_tab.settle_and_close(work)


def test_recapture_bound_holds_on_every_path(browser):  # noqa: F811
    from src.fb_api.graph import PageFailed

    work = PageTab.open(browser, call_s=60.0)
    try:
        assert light_tab.open_light_document(work)[1] == ""
        ig = InPageGraph(work)
        n = []
        ig.light_recapture = lambda g, t: n.append(1) or {"kind": "ads_manager", "has_token": False}
        ig.reacquire(timeout=5)
        ig.reacquire(timeout=5)
        with pytest.raises(PageFailed):
            ig.reacquire(timeout=5)                                      # the third load is refused
        with pytest.raises(PageFailed):
            ig.request("GET", "/me", {"fields": "id"}, timeout=5)        # and the call path shares the bound
        assert n == [1, 1]
    finally:
        light_tab.settle_and_close(work)


class _FakeHelperGraph:
    """`_eval_helper` replies scripted per call; nothing runs in a browser."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def _eval_helper(self, tail, timeout=10.0):
        self.calls.append(tail.split("(")[0])
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def test_handover_succeeds_when_the_receiver_got_it_although_the_ack_was_lost():
    dst = _FakeHelperGraph(["listening", "received"])
    src = _FakeHelperGraph([TimeoutError("busy machine")])
    assert light_tab.hand_over(src, dst, wait_s=0.1) == ""
    dst = _FakeHelperGraph(["listening", "listening", True])
    src = _FakeHelperGraph(["no_ack"])
    assert light_tab.hand_over(src, dst, wait_s=0.1) == "handover_timeout"
    assert dst.calls[-1] == ".stopListen"                               # a channel nobody used is closed


class _FakeCaptureTab:
    def __init__(self):
        self.closed = False
        self.light = None

    def pump(self, s):
        pass

    def close(self):
        self.closed = True


def _capture_rig(monkeypatch, *, state=None, navigate_exc=None, receive=""):
    from src.fb_api import token_capture

    rows, tabs = [], []
    monkeypatch.setattr(light_tab, "_record", rows.append)

    class Cap:
        def __init__(self, tab):
            self.tab = tab

        def navigate(self, act, timeout=60.0):
            if navigate_exc is not None:
                raise navigate_exc

        def wait_ready(self, timeout=90.0):
            return state or {"kind": "ads_manager", "has_token": True}

    monkeypatch.setattr(token_capture, "InPageGraph", Cap)

    class Work:
        light_recaptures = 1
        tab = SimpleNamespace(light=None)

        def receive_from(self, src):
            return receive

        def state(self):
            return {"kind": "ads_manager", "has_token": not receive, "light": True}

    def open_tab():
        tabs.append(_FakeCaptureTab())
        return tabs[-1]
    return Work(), open_tab, rows, tabs


@pytest.mark.parametrize("kind", ["login", "checkpoint"])
def test_capture_into_login_or_checkpoint_closes_the_capture_tab(monkeypatch, kind):
    work, open_tab, rows, tabs = _capture_rig(monkeypatch, state={"kind": kind, "has_token": False})
    got = light_tab.capture_into(work, open_tab, "act_1", timeout=1)
    assert got["kind"] == kind and not got["has_token"]
    assert tabs[0].closed and rows[-1]["fallback_reason"] == "capture_" + kind and not rows[-1]["handover_ok"]


def test_capture_into_reraises_a_stop_and_closes_the_capture_tab(monkeypatch):
    from src.fb_api.graph import LoggedOut

    work, open_tab, rows, tabs = _capture_rig(monkeypatch, navigate_exc=LoggedOut("login"))
    with pytest.raises(LoggedOut):
        light_tab.capture_into(work, open_tab, "act_1", timeout=1)
    assert tabs[0].closed and rows[-1]["phase"] == "light_recapture"


def test_capture_into_success_and_failed_handover(monkeypatch):
    work, open_tab, rows, tabs = _capture_rig(monkeypatch)
    assert light_tab.capture_into(work, open_tab, "act_1", timeout=1)["has_token"] is True
    assert tabs[0].closed and rows[-1]["handover_ok"] is True and rows[-1]["recapture_count"] == 1
    assert not any(isinstance(v, str) and ("://" in v or "act_" in v) for v in rows[-1].values())
    work, open_tab, rows, tabs = _capture_rig(monkeypatch, receive="handover_rejected")
    assert light_tab.capture_into(work, open_tab, "act_1", timeout=1)["has_token"] is False
    assert tabs[0].closed and rows[-1]["fallback_reason"] == "handover_rejected"


def test_restored_cleanup_closes_an_orphan_light_page_and_keeps_others():
    from src.fb_api.cdp import is_light_work_url
    from tests.test_acl2299_tabs_and_busy_freeze import Browser, _open, _row

    assert is_light_work_url(light_tab.LIGHT_URL)
    assert not is_light_work_url("https://www.facebook.com/__acl_work")
    assert not is_light_work_url(light_tab.LIGHT_ORIGIN + "/__acl_workx")
    browser = Browser([_row("orphan", light_tab.LIGHT_URL), _row("mail", "https://mail.example.com/")],
                      expected="profile")
    tab = _open(browser)
    try:
        assert browser.closed_targets == ["orphan"]
    finally:
        tab.close()


def test_light_tabs_are_closed_when_the_process_exits():
    tab = _FakeCaptureTab()
    light_tab._OPEN.add(tab)
    light_tab._close_all()
    assert tab.closed
    light_tab.settle_and_close(tab)
    assert tab not in light_tab._OPEN


def test_p5_header_probe_reads_headers_and_sends_nothing(browser, servers):  # noqa: F811
    from src.fb_ads_rpa import light_probe

    page, graph = servers
    graph_host = graph.origin.split("://", 1)[1]
    other = _Server()                                         # an ordinary page, standing in for Ads Manager
    am = PageTab.open(browser, call_s=60.0)
    work = PageTab.open(browser, call_s=60.0)
    try:
        am.call("Page.navigate", {"url": other.origin + "/adsmanager?act=123456789"}, timeout=30)
        time.sleep(1.0)
        assert light_tab.open_light_document(work)[1] == ""
        h_am = light_probe.request_headers(am, graph_host)
        h_light = light_probe.request_headers(work, graph_host)
        assert graph.hits == [] and page.hits == []            # the sentinel never left either tab
        assert h_am and h_light and h_am.get("user-agent") == h_light.get("user-agent")
        cmp = light_probe.compare_headers(h_am, h_light)
        assert "123456789" not in json.dumps(cmp)              # the referer path (account id) is never kept
        assert cmp["values"]["referer"]["light"] and cmp["values"]["referer"]["ads_manager"]
        # the light tab still answers its own origin locally afterwards
        ctx = work.create_isolated_world("after")
        assert work.evaluate_in(ctx, "fetch('/x').then(() => 'ok', () => 'blocked')") == "blocked"
        assert page.hits == []
    finally:
        for t in (work, am):
            light_tab.settle_and_close(t)
        other.close()


def test_pilot_gate(tmp_path, monkeypatch):
    from src.fb_ads_rpa import light_probe

    mp = tmp_path / "map.csv"
    mp.write_text("account_name,ad_account_id,profile_id\nFacebookDoc283 - X,act_1,PROF-283\n"
                  "FacebookDoc121 - Y,act_2,PROF-121\n", encoding="utf-8")
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(mp))
    light_probe.require_pilot("http://127.0.0.1:9222", "PROF-283")
    for url, pid in (("http://127.0.0.1:9222", "PROF-121"), ("http://10.0.0.5:9222", "PROF-283"),
                     ("http://127.0.0.1:9222", "")):
        with pytest.raises(light_probe.ProbeGateError):
            light_probe.require_pilot(url, pid)
    with pytest.raises(light_probe.ProbeGateError):
        light_probe.run_p5("http://127.0.0.1:9222", "act_1", "PROF-121")


def test_final_p1_probe_offline(browser):  # noqa: F811
    from src.fb_ads_rpa import light_probe

    got = light_probe.run_final_p1(browser, _gate=False)
    assert got["light_doc"] == "ok" and got["intact_after_open"] == "ok", got
    assert got["origin_fetch"] == "blocked"
    assert got["reload_served_locally"] is True and got["reload_noticed"] is True, got
    assert got["counts"]["fulfilled"] == 2 and got["counts"]["errors"] == 0
    with pytest.raises(light_probe.ProbeGateError):
        light_probe.run_final_p3("http://127.0.0.1:9222", "act_1", "PROF-121")


def test_flag_default_off():
    assert light_tab.enabled({}) is False
    assert light_tab.enabled({"ACL_LIGHT_WORK_TAB": "1"}) is True
    assert light_tab.enabled({"ACL_LIGHT_WORK_TAB": "no"}) is False
