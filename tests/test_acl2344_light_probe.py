"""ACL-2344 P1/P2(A'): synthetic same-origin document + dummy hand-over; no network, no Meta."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from src.fb_api import light_probe
from src.fb_ads_rpa.tab_watchdog import CdpError, RawCdp


def _chromium():
    """Same lookup as the other real-Chromium tests: ACL_BROWSER_EXECUTABLE, else Playwright's own path."""
    exe = os.environ.get("ACL_BROWSER_EXECUTABLE", "")
    if not exe:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                exe = pw.chromium.executable_path
        except Exception:  # noqa: BLE001
            exe = ""
    if exe and os.path.exists(exe):
        return exe
    if os.environ.get("CI"):
        pytest.fail("CI must provide Chromium (playwright install chromium or ACL_BROWSER_EXECUTABLE)")
    pytest.skip("no local Chromium")


@pytest.fixture()
def browser():
    exe = _chromium()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    args = [exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={tempfile.mkdtemp()}",
            "--proxy-server=http://127.0.0.1:9", "--no-first-run", "--disable-gpu"]   # dead proxy: nothing leaves
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


def _raw(messages):
    sent = []
    cdp = RawCdp.__new__(RawCdp)
    cdp._id = 0
    cdp._send_frame = lambda op, payload, timeout: sent.append(json.loads(payload))
    cdp._recv_message = lambda deadline: json.dumps(messages.pop(0)).encode()
    return cdp, sent


def test_rawcdp_nested_call_in_event_handler_keeps_reply_id():
    cdp, _ = _raw([{"method": "Fetch.requestPaused", "params": {}}, {"id": 1, "result": {"ok": 1}}])
    nested = []

    def on_event(msg):
        cdp._id += 1          # what a nested call() does
        nested.append(msg["method"])

    assert cdp.call("Page.navigate", {}, on_event=on_event) == {"ok": 1}
    assert nested == ["Fetch.requestPaused"]


def test_rawcdp_nested_call_never_drops_outer_reply_or_later_events():
    # the nested call sees the outer reply and a second paused event before its own reply
    msgs = [{"method": "Fetch.requestPaused", "params": {"n": 1}},
            {"method": "Fetch.requestPaused", "params": {"n": 2}},
            {"id": 1, "result": {"outer": 1}},
            {"id": 2, "result": {}}]
    cdp, sent = _raw(msgs)
    handled = []

    def on_event(msg):
        handled.append(msg["params"]["n"])
        if msg["params"]["n"] == 1:
            cdp.call("Fetch.failRequest", {})        # nested, no handler of its own

    assert cdp.call("Page.navigate", {}, on_event=on_event) == {"outer": 1}
    assert handled == [1, 2]                         # event 2 was deferred, not dropped
    assert [m["method"] for m in sent] == ["Page.navigate", "Fetch.failRequest"]


def test_rawcdp_drain_handles_events_and_returns_on_deadline():
    cdp = RawCdp.__new__(RawCdp)
    cdp._deferred = [{"method": "Fetch.requestPaused", "params": {"n": 1}}]
    cdp._buf = b""
    cdp._sock = object()
    got = []
    import select as _sel
    orig = _sel.select
    _sel.select = lambda r, w, x, t: ([], [], [])
    try:
        assert cdp.drain(0.05, got.append) == 1
    finally:
        _sel.select = orig
    assert got[0]["params"]["n"] == 1


class FakeTab:
    on_event = None

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def call(self, method, params=None, *, timeout=0, on_event=None):
        self.calls.append((method, params))
        if method == self.fail_on:
            raise CdpError("secret text must not leak")
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": light_probe.LIGHT_URL}}}
        return {}

    def create_isolated_world(self, name):
        return 1

    def evaluate_in(self, ctx, expr, timeout=0):
        return "complete"

    def pump(self, seconds):
        return 0


def _paused(rid, url, rtype):
    return {"method": "Fetch.requestPaused",
            "params": {"requestId": rid, "request": {"url": url}, "resourceType": rtype}}


def test_interception_covers_every_request_and_is_never_disabled():
    tab = FakeTab()
    light_probe.open_light_document(tab)
    methods = [m for m, _ in tab.calls]
    enable = dict(tab.calls)["Fetch.enable"]
    assert enable["patterns"] == [{"urlPattern": "*", "requestStage": "Request"}]
    assert ("Network.setBypassServiceWorker", {"bypass": True}) in tab.calls
    assert "Fetch.disable" not in methods
    assert methods.index("Network.setBypassServiceWorker") < methods.index("Page.navigate")
    assert tab.on_event is not None


def test_paused_requests_other_than_the_document_are_failed():
    tab = FakeTab()
    light_probe.open_light_document(tab)
    tab.on_event(_paused("d", light_probe.LIGHT_URL, "Document"))      # the one document is served
    assert ("Fetch.fulfillRequest") in [m for m, _ in tab.calls]
    tab.on_event(_paused("a", "https://adsmanager.facebook.com/favicon.ico", "Other"))
    tab.on_event(_paused("b", "https://example.com/x", "Fetch"))
    tab.on_event(_paused("c", light_probe.LIGHT_URL, "Document"))      # a second document is not served either
    failed = [p["requestId"] for m, p in tab.calls if m == "Fetch.failRequest"]
    assert failed == ["a", "b", "c"]
    assert tab.light_seen["blocked"] == 3


def test_failure_reason_names_the_method_only():
    res = light_probe.open_light_document(FakeTab(fail_on="Fetch.enable"))
    assert res["ok"] is False and res["reason"] == "light_doc_failed:Fetch.enable"
    assert "secret" not in repr(res)


def test_gate_refuses_everything_but_doc120(tmp_path, monkeypatch):
    mp = tmp_path / "map.csv"
    mp.write_text("account_name,ad_account_id,profile_id\nFacebookDoc120 - X,act_1,PROF-120\n"
                  "FacebookDoc121 - Y,act_2,PROF-121\n", encoding="utf-8")
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(mp))
    light_probe.require_doc120("http://127.0.0.1:9222", "PROF-120")          # passes
    for url, pid, code in (("http://127.0.0.1:9222", "PROF-121", "probe_not_doc120"),
                           ("http://127.0.0.1:9222", "", "probe_not_doc120"),
                           ("http://10.0.0.5:9222", "PROF-120", "probe_not_loopback")):
        with pytest.raises(light_probe.ProbeGateError, match=code):
            light_probe.require_doc120(url, pid)
    # run_p1 itself is gated: nothing is opened for a wrong profile
    with pytest.raises(light_probe.ProbeGateError):
        light_probe.run_p1("http://127.0.0.1:9222", "PROF-121")
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(tmp_path / "missing.csv"))
    with pytest.raises(light_probe.ProbeGateError, match="probe_doc120_unknown"):
        light_probe.require_doc120("http://127.0.0.1:9222", "PROF-120")


def test_light_document_and_dummy_handover(browser):
    result = light_probe.run_p1(browser, _gate=False)
    assert result["work_doc"]["ok"] is True, result
    assert result["work_doc"]["fulfilled"] == 1
    assert result["capture_doc"]["ok"] is True, result
    h = result["handover"]
    assert h["handover_ok"] and h["acked"] and h["same_world_survives"] and h["main_world_clean"]
    assert "DUMMY" not in repr(result)


def test_other_requests_of_the_probe_tab_are_failed(browser):
    from src.fb_api.cdp import PageTab

    tab = PageTab.open(browser, call_s=60.0)
    try:
        res = light_probe.open_light_document(tab)
        assert res["ok"] is True, res
        before = tab.light_seen["blocked"]
        got = tab.call("Runtime.evaluate", {
            "expression": "Promise.allSettled([fetch('/__acl_other.png'), fetch('https://example.com/x')])"
                          ".then(r => r.map(x => x.status))",
            "awaitPromise": True, "returnByValue": True}, timeout=60.0)
        assert got["result"]["value"] == ["rejected", "rejected"]
        assert tab.light_seen["blocked"] - before >= 2
        assert tab.light_seen["fulfilled"] == 1
    finally:
        tab.close()
