"""ACL-2522 rework: referrer of the work tab, flag-off reacquire, end-to-end 190 hand-over, stage/validate on
the work tab, no token in exceptions/logs. Fakes and Node only: no Chromium, no Meta, no Multilogin."""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.fb_api import light_tab
from src.fb_api import token_capture as tc
from src.fb_api.graph import GraphClient, GraphStop, LoggedOut
from src.fb_api.token_capture import InPageGraph

TOKEN = "EAAB" + "Zx9" * 20
ACT = "act_123456789"
CAMPAIGNS = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123456789"


class _Doc:
    marker = "m1"

    def document_intact(self, timeout=10.0):
        return ""

    def snapshot(self):
        return {"href": light_tab.LIGHT_URL, "marker": self.marker, "t": 1}


class ScriptTab:
    """A tab whose helper answers are scripted; records every `.call(`/`.validatePage(` spec."""

    def __init__(self, *, light=True, has_token=True):
        self.light = _Doc() if light else None
        self.has_token = has_token
        self.reloads = 0
        self.specs: list[dict] = []
        self.call_replies: list[dict] = []
        self.staged: dict[str, int] = {}
        self.retire_exc: BaseException | None = None
        self.n = 0

    def create_isolated_world(self, name):
        self.n += 1
        return self.n

    def reload(self):
        self.reloads += 1

    def browser_url(self, timeout=5.0):
        return light_tab.LIGHT_URL

    def evaluate_in(self, ctx, expr, timeout=10.0):
        if not expr.startswith("globalThis["):
            return None                                           # helper injection
        tail = expr.split("]", 1)[1]
        if tail.startswith(".state()"):
            return {"kind": "ads_manager", "light": self.light is not None, "has_token": self.has_token}
        if tail.startswith(".retire()"):
            if self.retire_exc is not None:
                raise self.retire_exc
            return True
        m = re.match(r"\.(call|validatePage)\((.*)\)$", tail, re.S)
        if m:
            self.specs.append(json.loads(m.group(2)))
            if m.group(1) == "validatePage":
                return {"status": 200, "body": {"success": True}, "headers": {}}
            return self.call_replies.pop(0)
        m = re.match(r'\.stage\("([^"]+)", "([^"]*)"\)$', tail)
        if m:
            self.staged[m.group(1)] = self.staged.get(m.group(1), 0) + len(base64.b64decode(m.group(2)))
            return {"ok": True, "bytes": self.staged[m.group(1)]}
        raise AssertionError("unscripted helper call: " + tail[:60])


def _graph(tab, **kw):
    ig = InPageGraph(tab, **kw)
    if tab.light is not None:
        ig.bind_act(ACT)
    return ig


# --- 1. flag off: reacquire is exactly as before the PR ---------------------------------------------------

def test_flag_off_reacquire_ends_on_a_connection_stop_and_never_reloads():
    tab = ScriptTab(light=False)
    tab.retire_exc = GraphStop("connection stopped")
    ig = InPageGraph(tab)
    assert not ig.light
    with pytest.raises(GraphStop):
        ig.reacquire(timeout=1)
    assert tab.reloads == 0                                        # no extra Ads Manager load on a stopped tab


def test_flag_off_reacquire_still_reloads_when_retire_fails_softly():
    tab = ScriptTab(light=False)
    tab.retire_exc = TimeoutError("busy")
    ig = InPageGraph(tab)
    ig.wait_ready = lambda timeout=90.0: {"kind": "ads_manager", "has_token": True}
    assert ig.reacquire(timeout=1)["has_token"] is True
    assert tab.reloads == 1


def test_light_reacquire_swallows_a_stop_of_the_work_tab_and_takes_the_token_again():
    tab = ScriptTab()
    tab.retire_exc = GraphStop("connection stopped")
    ig = _graph(tab)
    seen = []
    ig.light_recapture = lambda g, t: seen.append(1) or {"kind": "login", "has_token": False}
    assert ig.reacquire(timeout=1)["kind"] == "login" and seen == [1] and tab.reloads == 0


# --- 2. referrer on every fetch of the work tab -----------------------------------------------------------

def test_light_specs_carry_the_campaigns_url_of_the_act_and_default_specs_do_not():
    tab = ScriptTab()
    tab.call_replies = [{"status": 200, "body": {"data": []}, "headers": {}}]
    ig = _graph(tab, allow_writes=True, write_act=ACT)
    ig.request("GET", "/me", {"fields": "id"})
    assert tab.specs[-1]["referrer"] == CAMPAIGNS
    ig.bind_act("act_555666777")                                    # the work tab serves the next account
    tab.call_replies = [{"status": 200, "body": {}, "headers": {}}]
    ig.request("GET", "/me", {"fields": "id"})
    assert tab.specs[-1]["referrer"].endswith("act=555666777")
    default = ScriptTab(light=False)
    default.call_replies = [{"status": 200, "body": {"data": []}, "headers": {}}]
    InPageGraph(default).request("GET", "/me", {"fields": "id"})
    assert "referrer" not in default.specs[-1]


_NODE = r"""
const RES = [__RES__];
const calls = [];
const fakeFetch = async (url, init) => {
  calls.push({ url: String(url), init: init || {} });
  return { status: 200, text: async () => '{"data":[]}', headers: { get: () => null } };
};
const loc = { href: 'https://adsmanager.facebook.com/__acl_work', hostname: 'adsmanager.facebook.com',
              pathname: '/__acl_work', origin: 'https://adsmanager.facebook.com' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [], documentElement: { innerHTML: '' } };
const perf = { getEntriesByType: (t) => t === 'resource' ? RES.map((n) => ({ name: n })) : [] };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams', HELPER)(
  G, loc, doc, perf, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
const GOOD = __GOOD__;
(async () => {
  await h.call({ method: 'GET', path: '/me', params: {}, referrer: GOOD });
  await h.call({ method: 'GET', path: '/me', params: {} });
  await h.call({ method: 'GET', path: '/me', params: {}, referrer: 'https://evil.example/adsmanager/manage/campaigns?act=1' });
  await h.call({ method: 'GET', path: '/me', params: {}, referrer: 'https://adsmanager.facebook.com/other' });
  const v = await h.validatePage({ page_id: '123', link: '', host: 'captured', credentials: 'captured', referrer: GOOD });
  process.stdout.write(JSON.stringify({ refs: calls.map((c) => c.init.referrer === undefined ? null : c.init.referrer),
    methods: calls.map((c) => c.init.method), validate: v.status }));
})();
"""


def test_the_helper_sets_the_referrer_on_get_and_post_fetches_and_ignores_foreign_values():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_ref"
    res = f"https://graph.facebook.com/v22.0/act_1/campaigns?fields=name&access_token={TOKEN}"
    src = (_NODE.replace("__RES__", json.dumps(res)).replace("__GOOD__", json.dumps(CAMPAIGNS))
           .replace("NAME", json.dumps(name))
           .replace("HELPER", json.dumps(tc.helper_source(name, None, ACT))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fh:
        fh.write(src)
        script = fh.name
    try:
        done = subprocess.run([node, script], capture_output=True, encoding="utf-8", timeout=60)
    finally:
        Path(script).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert out["methods"] == ["GET", "GET", "GET", "GET", "POST"]
    assert out["refs"] == [CAMPAIGNS, None, None, None, CAMPAIGNS]
    assert out["validate"] == 200


# --- 3. end to end: 190 -> capture again -> hand over -> retried once ------------------------------------

def _e190(msg="Invalid OAuth access token."):
    return {"status": 400, "headers": {}, "body": {"error": {"code": 190, "message": msg, "type": "OAuthException"}}}


def _lease(monkeypatch, *, second_190: bool, leak: str = ""):
    tab = ScriptTab()
    ig = _graph(tab)
    rows: list[dict] = []
    monkeypatch.setattr(light_tab, "_record", rows.append)

    class Cap:                                                       # the capture tab: Ads Manager, has a token
        def __init__(self, t):
            self.tab = t

        def navigate(self, act, timeout=60.0):
            pass

        def wait_ready(self, timeout=90.0):
            return {"kind": "ads_manager", "has_token": True}

    monkeypatch.setattr(tc, "InPageGraph", Cap)
    closed = []

    class CapTab:
        light = None

        def pump(self, s):
            pass

        def close(self):
            closed.append(1)

    def hand_over(src):                                              # the hand-over: the work tab gets a token
        tab.has_token = True
        return ""

    ig.receive_from = hand_over
    ig.light_recapture = lambda g, t: light_tab.capture_into(g, CapTab, ACT, timeout=t)
    tab.has_token = True
    msg = leak or "Invalid OAuth access token."
    tab.call_replies = [_e190(msg), _e190(msg) if second_190
                        else {"status": 200, "headers": {}, "body": {"data": [{"id": "1"}]}}]
    client = GraphClient(ig, min_interval_s=0, sleep=lambda s: None)
    return tab, ig, client, rows, closed


def test_190_takes_the_token_again_hands_it_over_and_retries_once(monkeypatch):
    tab, ig, client, rows, closed = _lease(monkeypatch, second_190=False)
    got = client.get("/me/adaccounts", {"fields": "id"})
    assert got["data"] == [{"id": "1"}]
    assert len(tab.specs) == 2                                      # the call, then exactly one retry
    assert ig.light_recaptures == 1 and tab.reloads == 0           # one capture, the work tab never reloaded
    assert len(closed) == 1                                         # the capture tab was closed
    row = [r for r in rows if r.get("phase") == "light_recapture"][-1]
    assert row["handover_ok"] is True and row["capture_tab_closed"] is True and row["recapture_count"] == 1


def test_a_second_190_after_the_hand_over_stops_and_nothing_loops(monkeypatch):
    tab, ig, client, rows, closed = _lease(monkeypatch, second_190=True)
    with pytest.raises(LoggedOut):
        client.get("/me/adaccounts", {"fields": "id"})
    assert len(tab.specs) == 2 and ig.light_recaptures == 1 and tab.reloads == 0
    with pytest.raises(GraphStop):
        client.get("/me/adaccounts", {"fields": "id"})              # stopped: no third call
    assert len(tab.specs) == 2


def test_the_token_is_in_no_exception_log_or_telemetry_row(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    leak = f"Invalid OAuth access token {TOKEN} for the session"
    tab, ig, client, rows, closed = _lease(monkeypatch, second_190=True, leak=leak)
    with pytest.raises(GraphStop) as info:
        client.get("/me/adaccounts", {"fields": "id"})
    seen = [str(info.value), repr(info.value), caplog.text, json.dumps(rows, default=str),
            repr(client.__dict__), repr(ig.__dict__)]
    assert TOKEN not in "\n".join(seen)
    # a failed hand-over leaves a row with a code only
    tab2, ig2, client2, rows2, closed2 = _lease(monkeypatch, second_190=False, leak=leak)
    ig2.receive_from = lambda src: "handover_rejected"
    with pytest.raises(GraphStop) as info2:
        client2.get("/me/adaccounts", {"fields": "id"})
    assert TOKEN not in str(info2.value) + json.dumps(rows2, default=str) + caplog.text
    assert [r["fallback_reason"] for r in rows2 if r.get("phase") == "light_recapture"] == ["handover_rejected"]


# --- 4. file staging and page validation from the work tab -------------------------------------------------

def test_stage_file_and_validate_page_work_on_the_work_tab():
    tab = ScriptTab()
    ig = InPageGraph(tab, allow_writes=True, write_act=ACT)
    ig.bind_act(ACT)
    data = b"x" * (ig.STAGE_CHUNK + 10)                              # two chunks
    ctx, stage_id = ig.stage_file(data)
    assert tab.staged[stage_id] == len(data) and tab.reloads == 0
    got = ig.validate_page(ACT, "123456789012")
    assert got["status"] == 200 and tab.specs[-1]["referrer"] == CAMPAIGNS
    assert tab.reloads == 0 and ig.light_recaptures == 0            # no heavy page involved
    with pytest.raises(ValueError):
        ig.validate_page("act_999999999", "123456789012")          # still bound to the lease's account
