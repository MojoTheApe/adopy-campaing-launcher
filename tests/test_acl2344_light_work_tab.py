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


def test_flag_default_off():
    assert light_tab.enabled({}) is False
    assert light_tab.enabled({"ACL_LIGHT_WORK_TAB": "1"}) is True
    assert light_tab.enabled({"ACL_LIGHT_WORK_TAB": "no"}) is False
