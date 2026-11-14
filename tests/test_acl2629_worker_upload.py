"""ACL-2629: original videos (up to Meta's 4 GB) go up from a Web Worker inside the profile.

Everything here is fake: no browser, no Meta. What is checked:

- write policy, Python line: a served chunk (a window of a file) up to 64 MB, bytes up to the old 8 MB, 4 GB files;
- the plan estimate counts the ORIGINAL size with 64 MB chunks (compression is not planned any more);
- `UploadServer` (the CDP Fetch side): serves exactly the announced pieces, refuses everything else, attaches the
  worker session, leaves a light work tab's own interception alone;
- `InPageGraph`: probe and served write do not stage anything in the page, pump events while the job runs;
- the page helper in Node with a fake Worker: start / transfer / finish against a fake Meta, offsets from Meta,
  md5 of the reassembled bytes equals the source, piece hash and policy refusals, a blocked worker, a lost answer;
- the creator on a fake serving transport: no compression, resume without duplicates, Meta windows obeyed, fallback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import upload_server as us  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, StubTab  # noqa: E402
from tests.test_acl1891_video_chunks import (  # noqa: E402
    MB, PATH, ChunkMeta, _big_draft, _run, _sha, _video_objects)

START = {"upload_phase": "start", "file_size": "300000000"}
TRANSFER = {"upload_phase": "transfer", "upload_session_id": "12345678", "start_offset": "0"}
FINISH = {"upload_phase": "finish", "upload_session_id": "12345678", "title": "acl-x-1"}


# ───────────────────────── policy, Python line ─────────────────────────

class _Mem:
    def __init__(self, data: bytes):
        self.data = data

    def read_range(self, offset, n):
        return self.data[offset:offset + n]


def _served(length, offset=0):
    return wp.ServedChunk(_Mem(b"\x07" * 16), offset, length)


def test_a_served_chunk_may_be_up_to_64_mb_and_bytes_stay_at_8_mb():
    assert wp.UPLOAD_CHUNK_MAX == 64 * MB and wp.UPLOAD_STAGED_CHUNK_MAX == 8 * MB
    assert wp.check(ACT, "POST", PATH, TRANSFER, {"video_file_chunk": _served(64 * MB)}) == "advideos"
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, TRANSFER, {"video_file_chunk": _served(64 * MB + 1)})
    assert wp.check(ACT, "POST", PATH, TRANSFER, {"video_file_chunk": b"x" * (8 * MB)}) == "advideos"
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, TRANSFER, {"video_file_chunk": b"x" * (8 * MB + 1)})


def test_a_served_chunk_follows_every_other_upload_rule():
    for bad in ({**TRANSFER, "start_offset": "x"}, {**TRANSFER, "upload_session_id": "1"},
                {**TRANSFER, "end_offset": "9"}, {**TRANSFER, "start_offset": str(wp.UPLOAD_FILE_MAX)}):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", PATH, bad, {"video_file_chunk": _served(10)})
    for phase in (START, FINISH):
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", PATH, phase, {"video_file_chunk": _served(10)})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, TRANSFER, {"source": _served(10)})


def test_a_served_chunk_must_start_at_start_offset():
    chunk = wp.ServedChunk(_Mem(b"x" * 16), 5, 10)
    with pytest.raises(wp.WriteRefused, match="start_offset"):
        wp.check(ACT, "POST", PATH, {**TRANSFER, "start_offset": "0"}, {"video_file_chunk": chunk})
    assert wp.check(ACT, "POST", PATH, {**TRANSFER, "start_offset": "5"}, {"video_file_chunk": chunk}) == "advideos"


def test_files_up_to_4_gb_may_start_and_more_may_not():
    assert wp.UPLOAD_FILE_MAX == 4096 * MB
    assert wp.check(ACT, "POST", PATH, {**START, "file_size": str(wp.UPLOAD_FILE_MAX)}) == "advideos"
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", PATH, {**START, "file_size": str(wp.UPLOAD_FILE_MAX + 1)})


def test_a_served_chunk_is_cut_into_pieces_inside_its_window():
    chunk = wp.ServedChunk(_Mem(b"a" * 100), 10, 35)
    assert chunk.pieces(10) == [(0, 10), (10, 10), (20, 10), (30, 5)]
    assert chunk.pieces(10 ** 12)[0][1] == 35                      # capped by the chunk
    with pytest.raises(ValueError):
        chunk.read(30, 6)                                          # outside the window
    with pytest.raises(ValueError):
        wp.ServedChunk(_Mem(b""), -1, 5)
    with pytest.raises(ValueError):
        wp.ServedChunk(_Mem(b""), 0, 0)


def test_the_helper_gets_the_served_limits_from_the_same_policy():
    up = wp.policy_for_page(ACT)["upload"]
    assert up["served_chunk_max"] == 64 * MB and up["chunk_max"] == 8 * MB and up["piece_max"] == 8 * MB
    assert up["file_max"] == 4096 * MB and up["serve_prefix"] == "/__acl_upload/"


# ───────────────────────── plan estimate ─────────────────────────

def test_the_estimate_counts_the_original_in_64_mb_chunks(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    assert cr.worker_upload_enabled()
    assert cr.upload_calls(2500 * MB) == -(-2500 // 64) + cr.CALLS_UPLOAD_FIXED == 43       # a 2.5 GB file
    assert cr.upload_calls(4096 * MB) == 64 + cr.CALLS_UPLOAD_FIXED
    assert cr.upload_calls(16 * MB) == 0 and cr.upload_calls(16 * MB + 1) == 1 + cr.CALLS_UPLOAD_FIXED
    # nothing is compressed: the planned size is the original even where ffmpeg exists
    from src.fb_ads_rpa import creative_compress as cc
    monkeypatch.setattr(cc, "ffmpeg_exe", lambda: "/usr/bin/ffmpeg")
    assert cr.planned_upload_size(str(tmp_path / "x.mp4"), 2500 * MB) == 2500 * MB
    assert cr.video_limit_bytes() == 4096 * MB


def test_the_kill_switch_restores_the_compressing_path(monkeypatch):
    for off in ("0", "false", "No", "OFF"):
        monkeypatch.setenv("ACL_VIDEO_WORKER_UPLOAD", off)
        assert not cr.worker_upload_enabled()
        assert cr.upload_calls(1000 * MB) == 128 and cr.video_limit_bytes() == 1536 * MB
    monkeypatch.setenv("ACL_VIDEO_WORKER_UPLOAD", "")
    assert cr.worker_upload_enabled()


# ───────────────────────── UploadServer (CDP Fetch side) ─────────────────────────

class FakeCdpTab:
    def __init__(self, light=None):
        self.session_id = "S1"
        self.light = light
        self.on_event = None
        self.calls: list[tuple[str, str, dict]] = []      # (session, method, params)
        self.fail: set[str] = set()

    def call(self, method, params=None, *, timeout=0):
        self.calls.append((self.session_id, method, params or {}))
        return {}

    def call_session(self, session_id, method, params=None, *, timeout=0):
        if method in self.fail:
            raise RuntimeError("boom")
        self.calls.append((session_id, method, params or {}))
        return {}

    def of(self, method):
        return [(s, p) for s, m, p in self.calls if m == method]


def _paused(url, rid="r1", session=""):
    msg = {"method": "Fetch.requestPaused", "params": {"requestId": rid, "request": {"url": url}}}
    if session:
        msg["sessionId"] = session
    return msg


def _armed(data=b"0123456789" * 100, piece=300):
    tab = FakeCdpTab()
    server = us.UploadServer(tab)
    server.arm()
    chunk = wp.ServedChunk(_Mem(data), 0, len(data))
    token, pieces = server.register(chunk, piece=piece)
    return tab, server, chunk, token, pieces


def test_arm_turns_the_narrow_interception_on_and_attaches_workers():
    tab = FakeCdpTab()
    server = us.UploadServer(tab)
    server.arm()
    server.arm()                                          # again: nothing is repeated for the same session
    assert tab.of("Fetch.enable") == [("S1", {"patterns": [{"urlPattern": us.PATTERN, "requestStage": "Request"}]})]
    assert us.PATTERN == "https://adsmanager.facebook.com/__acl_upload/*"
    assert tab.of("Target.setAutoAttach") == [("S1", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                                       "flatten": True})]
    assert tab.on_event is not None


def test_a_light_work_tab_keeps_its_own_origin_wide_interception():
    tab = FakeCdpTab(light=object())
    us.UploadServer(tab).arm()
    assert tab.of("Fetch.enable") == []                   # would replace the light document's pattern
    assert len(tab.of("Target.setAutoAttach")) == 1


def test_a_worker_session_gets_the_interception_and_is_resumed():
    tab = FakeCdpTab()
    server = us.UploadServer(tab)
    server.arm()
    tab.on_event({"method": "Target.attachedToTarget", "params": {
        "sessionId": "W9", "targetInfo": {"type": "worker"}, "waitingForDebugger": True}})
    assert tab.of("Fetch.enable")[-1][0] == "W9"
    assert ("W9", {}) in tab.of("Runtime.runIfWaitingForDebugger")
    assert ("W9", {"bypass": True}) in tab.of("Network.setBypassServiceWorker")       # N5


def test_an_iframe_or_a_pages_own_worker_that_attaches_is_resumed_and_let_go():
    tab = FakeCdpTab()
    server = us.UploadServer(tab)
    server.arm()
    for kind, sid in (("iframe", "X"), ("service_worker", "Y")):
        tab.on_event({"method": "Target.attachedToTarget", "params": {
            "sessionId": sid, "targetInfo": {"type": kind}, "waitingForDebugger": True}})
        assert (sid, {}) in tab.of("Runtime.runIfWaitingForDebugger")                  # never left paused
        assert ("", {"sessionId": sid}) in tab.of("Target.detachFromTarget")
        assert not [1 for s_, p in tab.of("Fetch.enable") if s_ == sid]


def test_a_worker_whose_interception_cannot_be_enabled_is_never_resumed():
    tab = FakeCdpTab()
    server = us.UploadServer(tab)
    server.arm()
    tab.fail.add("Fetch.enable")
    tab.on_event({"method": "Target.attachedToTarget", "params": {
        "sessionId": "W9", "targetInfo": {"type": "worker", "targetId": "T9"}, "waitingForDebugger": True}})
    assert tab.of("Runtime.runIfWaitingForDebugger") == []                              # B2: fail closed
    assert ("", {"targetId": "T9"}) in tab.of("Target.closeTarget") and server.refused_workers == 1


def test_disarm_turns_auto_attach_off_and_detaches_worker_sessions():
    tab = FakeCdpTab()
    server = us.UploadServer(tab)
    server.arm()
    tab.on_event({"method": "Target.attachedToTarget", "params": {
        "sessionId": "W9", "targetInfo": {"type": "worker"}, "waitingForDebugger": True}})
    server.disarm()
    assert ("", {"sessionId": "W9"}) in tab.of("Target.detachFromTarget")             # N6
    assert tab.of("Target.setAutoAttach")[-1][1]["autoAttach"] is False
    assert tab.of("Fetch.disable")
    server.arm()                                                                         # arms again for the next chunk
    assert tab.of("Target.setAutoAttach")[-1][1]["autoAttach"] is True


def test_a_light_tab_is_not_given_fetch_disable_on_disarm():
    tab = FakeCdpTab(light=object())
    server = us.UploadServer(tab)
    server.arm()
    server.disarm()
    assert tab.of("Fetch.disable") == []


def test_exactly_the_announced_pieces_are_served_from_the_file():
    data = bytes(range(256)) * 12
    tab, server, chunk, token, pieces = _armed(data, piece=1000)
    assert [p["n"] for p in pieces] == [1000, 1000, 1000, 72] and sum(p["n"] for p in pieces) == len(data)
    for p in pieces:
        url = f"{us.URL_PREFIX}{token}/{p['o']}/{p['n']}"
        tab.on_event(_paused(url, rid=f"r{p['o']}", session="W9"))
        sess, body = tab.of("Fetch.fulfillRequest")[-1]
        assert sess == "W9"                                # answered on the session that asked (the worker's)
        assert base64.b64decode(body["body"]) == data[p["o"]:p["o"] + p["n"]]
        assert hashlib.sha256(base64.b64decode(body["body"])).hexdigest() == p["h"]
        assert {"name": "Cache-Control", "value": "no-store"} in body["responseHeaders"]
    assert server.served == 4 and server.served_bytes == len(data)


@pytest.mark.parametrize("tail", ["/0/999", "/0/1001", "/1/1000", "/3000/1000", "/0/0", "/x/y"])
def test_a_piece_that_was_not_announced_is_refused(tail):
    tab, server, chunk, token, pieces = _armed(bytes(3000), piece=1000)
    tab.on_event(_paused(f"{us.URL_PREFIX}{token}{tail}"))
    assert tab.of("Fetch.fulfillRequest") == []
    assert tab.of("Fetch.failRequest")[-1][1]["errorReason"] == "BlockedByClient"


def test_a_foreign_or_finished_token_is_refused():
    tab, server, chunk, token, pieces = _armed(bytes(3000), piece=1000)
    tab.on_event(_paused(f"{us.URL_PREFIX}{'a' * 24}/0/1000"))
    server.unregister(token)
    tab.on_event(_paused(f"{us.URL_PREFIX}{token}/0/1000"))
    assert tab.of("Fetch.fulfillRequest") == [] and len(tab.of("Fetch.failRequest")) == 2


def test_a_file_that_changed_after_the_chunk_was_announced_is_not_served():
    src = _Mem(b"A" * 3000)
    tab = FakeCdpTab()
    server = us.UploadServer(tab)
    server.arm()
    token, pieces = server.register(wp.ServedChunk(src, 0, 3000), piece=1000)
    src.data = b"A" * 1000 + b"B" + b"A" * 1999            # the second piece changed on disk
    for p in pieces:
        tab.on_event(_paused(f"{us.URL_PREFIX}{token}/{p['o']}/{p['n']}", rid=f"r{p['o']}"))
    assert len(tab.of("Fetch.fulfillRequest")) == 2
    assert tab.of("Fetch.failRequest")[-1][1]["errorReason"] == "Failed" and server.blocked == 1


def test_other_requests_go_on_to_the_previous_handler():
    tab = FakeCdpTab()
    seen = []
    tab.on_event = seen.append
    server = us.UploadServer(tab)
    server.arm()
    other = _paused("https://adsmanager.facebook.com/__acl_work", "d")
    tab.on_event(other)
    assert seen == [other] and tab.of("Fetch.fulfillRequest") == []


def test_a_failing_answer_never_raises_out_of_the_event_pump():
    tab, server, chunk, token, pieces = _armed(bytes(3000), piece=1000)
    tab.fail.add("Fetch.fulfillRequest")
    tab.on_event(_paused(f"{us.URL_PREFIX}{token}/0/1000"))      # must not raise
    assert server.errors == 1
    assert tab.of("Fetch.failRequest")[-1][1]["errorReason"] == "Failed"      # B3: the request is not left paused


# ───────────────────────── websocket masking ─────────────────────────

def test_the_fast_mask_gives_the_same_bytes_as_the_byte_loop():
    from src.fb_ads_rpa.tab_watchdog import _xor_mask
    for n in (0, 1, 3, 4, 5, 1001, 70000):
        data = os.urandom(n)
        mask = os.urandom(4)
        assert _xor_mask(data, mask) == bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        assert _xor_mask(_xor_mask(data, mask), mask) == data


# ───────────────────────── InPageGraph: probe and served write ─────────────────────────

class ServingTab(StubTab):
    """StubTab that also plays the CDP side of the worker path (no browser)."""

    def __init__(self, probe_reply=None, job_reply=None, **kw):
        super().__init__(**kw)
        self.session_id = "S1"
        self.light = None
        self.on_event = None
        self.cdp_calls: list[tuple[str, dict]] = []
        self.probe_reply = probe_reply if probe_reply is not None else {"ok": True, "bytes": 4096}
        self.job_reply = job_reply if job_reply is not None else {"status": 200, "body": {"success": True}}
        self.specs: list[dict] = []
        self.pumped: list[float] = []
        self.polls = 0

    def call(self, method, params=None, *, timeout=0):
        self.cdp_calls.append((method, params or {}))
        return {}

    def call_session(self, session_id, method, params=None, *, timeout=0):
        return {}

    def pump(self, seconds):
        self.pumped.append(seconds)
        return 0

    def evaluate_in(self, ctx, expr, timeout=60.0):
        if ".probeWorker(" in expr:
            self.exprs.append(expr)
            self.specs.append(json.loads(expr.split(".probeWorker(", 1)[1].rsplit(")", 1)[0]))
            return self.probe_reply
        if ".abort(" in expr:
            self.exprs.append(expr)
            return {"ok": True}
        if ".startCall(" in expr:
            self.exprs.append(expr)
            self.specs.append(json.loads(expr.split(".startCall(", 1)[1].rsplit(")", 1)[0]))
            return {"job": "j1"}
        if ".poll(" in expr:
            self.polls += 1
            if self.polls == 1:
                return {"state": "running"}
            return {"state": "done", "result": self.job_reply}
        return super().evaluate_in(ctx, expr, timeout)


def _graph(tab):
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    t._sleep = lambda s: None
    return t


def test_the_probe_announces_a_small_hashed_chunk_and_stages_nothing():
    tab = ServingTab()
    ok, why = _graph(tab).probe_worker()
    assert (ok, why) == (True, "")
    spec = tab.specs[0]
    assert set(spec) == {"source"} and set(spec["source"]) == {"token", "pieces"}
    assert sum(p["n"] for p in spec["source"]["pieces"]) == 4096 and len(spec["source"]["pieces"]) == 4
    assert not [e for e in tab.exprs if ".stage(" in e]
    assert any(m == "Fetch.enable" for m, _ in tab.cdp_calls)
    assert tab.cdp_calls[-1][0] in ("Fetch.disable", "Target.setAutoAttach")      # disarmed afterwards


@pytest.mark.parametrize("reply, text", [
    ({"error": "no_worker", "why": "worker blocked: SecurityError"}, "SecurityError"),
    ({"error": "no_worker", "why": "worker did not start"}, "did not start"),
    ({"ok": True, "bytes": 1}, "bad reply|ok|None"),
])
def test_a_probe_that_fails_says_why(reply, text):
    ok, why = _graph(ServingTab(probe_reply=reply)).probe_worker()
    assert ok is False and why


def test_the_probe_is_refused_for_a_read_only_page():
    t = tc.InPageGraph(ServingTab())
    assert t.probe_worker()[0] is False


def test_a_served_chunk_goes_to_the_helper_as_a_window_not_as_bytes():
    data = os.urandom(3 * MB + 5)
    tab = ServingTab()
    t = _graph(tab)
    chunk = wp.ServedChunk(_Mem(data), 0, len(data))
    got = t.write(PATH, {**TRANSFER}, files={"video_file_chunk": chunk}, filename="chunk")
    assert got == {"status": 200, "body": {"success": True}}
    spec = tab.specs[0]
    assert spec["files"] == {"video_file_chunk": "served"} and spec["expect"] == {"bytes": len(data)}
    pieces = spec["source"]["pieces"]
    assert [p["n"] for p in pieces] == [3 * MB + 5]                  # 8 MB pieces: one piece here
    assert pieces[0]["h"] == hashlib.sha256(data).hexdigest()
    assert not [e for e in tab.exprs if ".stage(" in e]              # nothing copied into the page
    assert tab.pumped, "the events of the tab are handled while the job runs"
    assert not t._server()._windows                                  # the window is closed after the call


def test_a_served_job_that_never_finishes_ends_at_its_deadline_as_unknown():
    class Stuck(ServingTab):
        def evaluate_in(self, ctx, expr, timeout=60.0):
            if ".poll(" in expr:
                return {"state": "running"}
            return super().evaluate_in(ctx, expr, timeout)

    tab = Stuck()
    t = _graph(tab)
    now = [0.0]
    t._clock = lambda: now[0]
    t._wait = lambda s_, serving: now.__setitem__(0, now[0] + 30)
    chunk = wp.ServedChunk(_Mem(b"x" * 100), 0, 100)
    got = t.write(PATH, {**TRANSFER}, files={"video_file_chunk": chunk}, filename="chunk")
    assert got == {"error": "call_unknown", "reason": "cdp_timeout"}
    assert tab.specs[0]["piece_ms"] == 60000


def test_a_served_chunk_is_checked_by_python_policy_first():
    tab = ServingTab()
    t = _graph(tab)
    big = wp.ServedChunk(_Mem(b"x"), 0, wp.UPLOAD_CHUNK_MAX + 1)
    with pytest.raises(wp.WriteRefused):
        t.write(PATH, {**TRANSFER}, files={"video_file_chunk": big}, filename="chunk")
    assert tab.specs == [] and tab.cdp_calls == []


def test_a_served_chunk_that_cannot_be_read_is_refused_before_anything_is_sent():
    class Broken:
        def read_range(self, offset, n):
            raise OSError("gone")

    tab = ServingTab()
    got = _graph(tab).write(PATH, {**TRANSFER}, files={"video_file_chunk": wp.ServedChunk(Broken(), 0, 10)},
                            filename="chunk")
    assert got["error"] == "no_file" and tab.specs == []


# ───────────────────────── the page helper (Node) with a fake Worker ─────────────────────────

_NODE = r"""
const crypto = require('crypto');
const { resolveObjectURL } = require('buffer');
const SOURCE = Buffer.from(__SOURCE_B64__, 'base64');
const SC = __SCENARIO__;
const TOKENV = __TOKENV__;
const ORIGIN = 'https://adsmanager.facebook.com';
const PREFIX = '/__acl_upload/';
const windows = {};
const log = { graph: [], pieces: 0, workers: 0 };
const meta = { size: 0, received: 0, md5: crypto.createHash('md5'), chunks: [], done: false };
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const reply = (status, obj) => ({ status: status, text: async () => JSON.stringify(obj), headers: { get: () => null } });
const range = () => ({ start_offset: String(meta.received),
                       end_offset: String(Math.min(meta.received + (SC.window || 1048576), meta.size)) });
const fakeFetch = async (url, init) => {
  url = String(url);
  if (url.startsWith(ORIGIN + PREFIX)) {
    const [token, o, n] = url.slice((ORIGIN + PREFIX).length).split('/');
    log.pieces++;
    const w = windows[token];
    if (SC.hang_pieces) return new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(new Error('abort'))));
    if (!w || SC.block_pieces) throw new TypeError('blocked');
    let bytes = Buffer.from(SOURCE.subarray(w.offset + Number(o), w.offset + Number(o) + Number(n)));
    if (SC.corrupt_piece === log.pieces) bytes[0] ^= 1;
    if (SC.short_piece === log.pieces) bytes = bytes.subarray(0, bytes.length - 1);
    return { status: 200, blob: async () => new Blob([bytes]) };
  }
  const body = init.body;
  const phase = body.get('upload_phase');
  log.graph.push({ phase: phase, referrer: init.referrer, cred: init.credentials, redirect: init.redirect, token_in_body: body.get('access_token') === TOKENV,
                   token_in_url: url.indexOf('access_token') >= 0, url: url.split('?')[0] });
  if (phase === 'start') {
    meta.size = Number(body.get('file_size'));
    return reply(200, Object.assign({ upload_session_id: '900001', video_id: '777' }, range()));
  }
  if (phase === 'transfer') {
    const blob = body.get('video_file_chunk');
    const buf = Buffer.from(await blob.arrayBuffer());
    const off = Number(body.get('start_offset'));
    if (off !== meta.received) return reply(400, { error: { code: 100, message: 'wrong start offset', error_data: range() } });
    meta.received += buf.length;
    meta.md5.update(buf);
    meta.chunks.push([off, buf.length]);
    if (SC.lose_transfer === meta.chunks.length) throw new TypeError('network');
    return reply(200, range());
  }
  meta.done = meta.received === meta.size;
  return reply(200, { success: meta.done });
};
const FakeWorker = function (url) {
  if (SC.worker_blocked) { const e = new Error('csp'); e.name = 'SecurityError'; throw e; }
  log.workers++;
  const w = this;
  const inner = { onmessage: null, close: () => {}, postMessage: (d) => setTimeout(() => w.onmessage && w.onmessage({ data: d }), 0) };
  w.postMessage = (m) => setTimeout(() => inner.onmessage && inner.onmessage({ data: m }), 0);
  w.terminate = () => {};
  resolveObjectURL(url).text().then((src) => {
    if (SC.worker_silent) return;
    new Function('self', 'fetch', 'crypto', 'FormData', 'Blob', 'AbortController', 'return ' + src)(
      inner, fakeFetch, crypto.webcrypto, FormData, Blob, AbortController);
  });
};
const loc = { href: ORIGIN + '/adsmanager/manage/campaigns', origin: ORIGIN, hostname: 'adsmanager.facebook.com',
              pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const G = {};
new Function('globalThis', 'location', 'document', 'performance', 'fetch', 'URL', 'URLSearchParams', 'crypto', 'Worker',
             __HELPER__)(G, loc, doc, { getEntriesByType: () => [] }, fakeFetch, URL, URLSearchParams, crypto.webcrypto,
                         FakeWorker);
const h = G[__NAME__];
const PATHV = __PATH__;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let tokenSeq = 0;
function announce(off, len, pieceSize, mutate) {
  const token = 'tok' + String(++tokenSeq).padStart(20, '0');
  windows[token] = { offset: off, length: len };
  const pieces = [];
  for (let o = 0; o < len; o += pieceSize) {
    const n = Math.min(pieceSize, len - o);
    pieces.push({ o: o, n: n, h: sha(SOURCE.subarray(off + o, off + o + n)) });
  }
  const spec = { method: 'POST', path: PATHV, filename: 'chunk', files: { video_file_chunk: 'served' },
                 source: { token: token, pieces: pieces }, expect: { bytes: len },
                 params: { upload_phase: 'transfer', upload_session_id: '900001', start_offset: String(off),
                           waterfall_id: 'a'.repeat(32) } };
  if (SC.piece_ms) spec.piece_ms = SC.piece_ms;
  spec.referrer = ORIGIN + '/adsmanager/manage/campaigns?act=1';
  if (mutate) mutate(spec);
  return spec;
}
async function job(spec) {
  const s = h.startCall(spec);
  if (s.error) return s;
  let p;
  while ((p = h.poll(s.job)).state === 'running') await sleep(1);
  return p.result;
}
const slim = (r) => (r && r.error) ? { error: r.error, why: r.why } : { ok: true, status: r.status, body: r.body };
(async () => {
  const out = { scenario: SC.name };
  const phase = (params) => h.call({ method: 'POST', path: PATHV, params: Object.assign({ waterfall_id: 'a'.repeat(32) }, params), files: {} });
  if (SC.name === 'upload') {
    const started = await phase({ upload_phase: 'start', file_size: String(SOURCE.length) });
    out.start = slim(started);
    let guard = 0;
    out.results = [];
    while (meta.received < meta.size && guard++ < 50) {
      const off = meta.received;
      const end = Math.min(off + (SC.window || 1048576), meta.size);
      const r = await job(announce(off, Math.min(end - off, SC.cap || 1 << 30), SC.piece || 400000));
      out.results.push(slim(r));
      if (r.error) break;
    }
    out.finish = slim(await phase({ upload_phase: 'finish', upload_session_id: '900001', title: 'acl-x' }));
  } else if (SC.name === 'refusals') {
    await phase({ upload_phase: 'start', file_size: String(SOURCE.length) });
    const before = { graph: log.graph.length, pieces: log.pieces };
    const bigPieces = [];
    for (let i = 0; i < 9; i++) bigPieces.push({ o: i * 8388608, n: 8388608, h: '0'.repeat(64) });
    const muts = {
      over_cap: (s) => { s.source.pieces = bigPieces; s.expect.bytes = 9 * 8388608; },
      gap: (s) => { s.source.pieces[1].o += 1; },
      bad_token: (s) => { s.source.token = 'AB'; },
      extra_key: (s) => { s.source.extra = 1; },
      bad_hash: (s) => { s.source.pieces[0].h = 'zz'; },
      piece_over: (s) => { s.source.pieces[0].n = 8388609; s.expect.bytes = 8388609; },
      too_many: (s) => { s.source.pieces = Array.from({ length: 513 }, (_, i) => ({ o: i, n: 1, h: '0'.repeat(64) })); s.expect.bytes = 513; },
      size_mismatch: (s) => { s.expect.bytes += 1; },
      empty: (s) => { s.source.pieces = []; },
      wrong_field: (s) => { s.files = { source: 'served' }; },
      two_sources: (s) => { s.files = { video_file_chunk: 'served', source: 'served' }; },
      finish_phase: (s) => { s.params = { upload_phase: 'finish', upload_session_id: '900001', title: 'x' }; s.files = {}; },
      start_phase: (s) => { s.params = { upload_phase: 'start', file_size: '5' }; s.files = {}; },
      other_edge: (s) => { s.path = '/act_999999/advideos'; },
      forms: (s) => { s.path = '/123456789/leadgen_forms'; s.params = {}; },
      adimages: (s) => { s.path = '/' + PATHV.split('/')[1] + '/adimages'; s.params = {}; s.files = { filename: 'served' }; },
    };
    out.refused = {};
    for (const k of Object.keys(muts)) out.refused[k] = slim(await job(announce(0, 800000, 400000, muts[k])));
    // not through a job: a served chunk may only be sent by a job
    const direct = announce(0, 800000, 400000);
    out.refused.no_job = slim(await h.call(direct));
    out.untouched = { graph: log.graph.length === before.graph, pieces: log.pieces === before.pieces };
  } else if (SC.name === 'probe') {
    const token = 'probe' + '0'.repeat(20);
    windows[token] = { offset: 0, length: 4096 };
    const pieces = [0, 1, 2, 3].map((i) => ({ o: i * 1024, n: 1024, h: sha(SOURCE.subarray(i * 1024, (i + 1) * 1024)) }));
    out.probe = await h.probeWorker({ source: { token: token, pieces: pieces } });
    out.bad = await h.probeWorker({ source: { token: 'AB', pieces: pieces } });
    out.graph_calls = log.graph.length;
  } else if (SC.name === 'abort') {
    await phase({ upload_phase: 'start', file_size: String(SOURCE.length) });
    const spec = announce(0, 800000, 100000);
    const s = h.startCall(spec);
    await sleep(2);
    out.abort = h.abort(s.job);
    let p;
    while ((p = h.poll(s.job)).state === 'running') await sleep(1);
    out.result = slim(p.result);
  }
  out.log = { graph: log.graph, pieces: log.pieces, workers: log.workers };
  out.meta = { received: meta.received, size: meta.size, md5: meta.md5.digest('hex'), chunks: meta.chunks, done: meta.done };
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
"""

_SOURCE = b"".join(hashlib.sha256(b"acl-2629-%d" % i).digest() for i in range(80000))[:2_500_000]
POLICY = wp.policy_for_page(ACT)


def _node(scenario, policy=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_worker_upload"
    script = (_NODE.replace("__SOURCE_B64__", json.dumps(base64.b64encode(_SOURCE).decode()))
              .replace("__SCENARIO__", json.dumps(scenario))
              .replace("__TOKENV__", json.dumps("EAAB" + "Zx9" * 20))
              .replace("__PATH__", json.dumps(PATH)).replace("__NAME__", json.dumps(name))
              .replace("__HELPER__", json.dumps(tc.helper_source(name, policy or POLICY))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=120)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_the_worker_uploads_the_whole_file_and_meta_gets_the_same_bytes():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 400_000})
    assert out["start"]["ok"] and out["finish"]["ok"] and out["finish"]["body"] == {"success": True}
    assert all(r["ok"] for r in out["results"])
    m = out["meta"]
    assert m["received"] == m["size"] == len(_SOURCE) and m["done"] is True
    assert m["md5"] == hashlib.md5(_SOURCE).hexdigest()                    # reassembled bytes == the source
    # the chunks follow Meta's windows exactly: contiguous, never overlapping, never repeated
    assert m["chunks"] == [[0, 1_000_000], [1_000_000, 1_000_000], [2_000_000, 500_000]]
    phases = [x["phase"] for x in out["log"]["graph"]]
    assert phases == ["start", "transfer", "transfer", "transfer", "finish"]
    assert out["log"]["workers"] == 1 and out["log"]["pieces"] == 3 + 3 + 2   # 400 kB pieces of three chunks


def test_every_graph_post_of_the_worker_keeps_the_token_in_the_body_and_follows_no_redirect():
    out = _node({"name": "upload", "window": 2_500_000, "piece": 700_000})
    for post in out["log"]["graph"]:
        assert post["token_in_body"] is True and post["token_in_url"] is False
        assert post["cred"] == "include" and post["redirect"] == "error"
        assert post["url"].endswith("/" + PATH.split("/")[1] + "/advideos")
    assert out["meta"]["md5"] == hashlib.md5(_SOURCE).hexdigest()
    assert "EAAB" not in json.dumps(out["results"]) and "EAAB" not in json.dumps(out["finish"])


def test_a_chunk_larger_than_8_mb_is_sent_whole_when_it_is_served():
    big = (b"".join(hashlib.sha256(b"big-%d" % i).digest() for i in range(400_000)))[:12_000_000]
    assert len(big) > wp.UPLOAD_STAGED_CHUNK_MAX
    global _SOURCE
    old = _SOURCE
    _SOURCE = big
    try:
        out = _node({"name": "upload", "window": 12_000_000, "piece": 8_388_608})
    finally:
        _SOURCE = old
    assert out["meta"]["chunks"] == [[0, 12_000_000]] and out["meta"]["md5"] == hashlib.md5(big).hexdigest()
    assert out["log"]["pieces"] == 2


def test_a_corrupted_piece_is_refused_in_the_worker_and_nothing_is_posted():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 400_000, "corrupt_piece": 2})
    assert out["results"][0] == {"error": "no_file", "why": "worker piece does not match the file"}
    assert [x["phase"] for x in out["log"]["graph"]] == ["start", "finish"]    # the transfer was never sent
    assert out["meta"]["received"] == 0


def test_a_short_piece_is_refused_in_the_worker():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 400_000, "short_piece": 1})
    assert out["results"][0]["error"] == "no_file" and "wrong size" in out["results"][0]["why"]
    assert out["meta"]["received"] == 0


def test_pieces_that_the_controller_does_not_answer_are_refused_before_any_post():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 400_000, "block_pieces": True})
    assert out["results"][0]["error"] == "no_file" and "piece request failed" in out["results"][0]["why"]
    assert [x["phase"] for x in out["log"]["graph"]] == ["start", "finish"]


def test_a_piece_the_controller_never_answers_times_out_as_no_file_and_nothing_is_posted():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 400_000, "hang_pieces": True, "piece_ms": 1000})
    assert out["results"][0] == {"error": "no_file", "why": "worker piece request timed out"}   # B3
    assert [x["phase"] for x in out["log"]["graph"]] == ["start", "finish"]


def test_the_worker_post_carries_the_same_referrer_as_other_calls():
    out = _node({"name": "upload", "window": 2_500_000, "piece": 700_000})
    posts = [x for x in out["log"]["graph"] if x["phase"] == "transfer"]
    assert posts and all(x["referrer"] == "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
                         for x in posts)                                                    # N3


def test_a_blocked_worker_is_reported_and_nothing_is_sent():
    out = _node({"name": "upload", "worker_blocked": True})
    assert out["results"][0]["error"] == "no_file" and "worker blocked" in out["results"][0]["why"]
    assert "SecurityError" in out["results"][0]["why"]
    assert [x["phase"] for x in out["log"]["graph"]] == ["start", "finish"]


def test_a_worker_that_never_starts_is_reported():
    # (the helper waits 8 s for the ready message)
    out = _node({"name": "probe", "worker_silent": True})
    assert out["probe"] == {"error": "no_worker", "why": "worker did not start"}
    assert out["graph_calls"] == 0


def test_the_probe_runs_the_whole_data_path_without_a_request_to_meta():
    out = _node({"name": "probe"})
    assert out["probe"] == {"ok": True, "bytes": 4096}
    assert out["bad"]["error"] == "no_worker" and out["bad"]["why"] == "source"
    assert out["graph_calls"] == 0 and out["log"]["pieces"] == 4 and out["log"]["workers"] == 1


def test_the_probe_reports_a_blocked_worker():
    out = _node({"name": "probe", "worker_blocked": True})
    assert out["probe"]["error"] == "no_worker" and "SecurityError" in out["probe"]["why"]


def test_a_lost_answer_is_unknown_and_the_chunk_is_not_sent_again_by_the_helper():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 400_000, "lose_transfer": 1})
    assert out["results"][0]["error"] == "network"                  # Python reconciles; the helper never retries
    transfers = [x for x in out["log"]["graph"] if x["phase"] == "transfer"]
    assert len(transfers) == 1


def test_every_malformed_served_chunk_is_refused_by_the_page_helper_before_anything_moves():
    out = _node({"name": "refusals"})
    why = {k: v.get("why") for k, v in out["refused"].items()}
    # (a spec without a file is turned away by `startCall` itself as bad_option; everything else by the policy)
    assert all(v.get("error") in ("write_refused", "bad_option") for v in out["refused"].values()), out["refused"]
    assert why["over_cap"] == "chunk_size" and why["size_mismatch"] == "file_size"
    for k in ("gap", "bad_token", "extra_key", "bad_hash", "piece_over", "too_many", "empty", "no_job"):
        assert why[k] == "source", (k, why[k])
    assert out["refused"]["start_phase"] == out["refused"]["finish_phase"] == {"error": "bad_option"}
    assert why["forms"] == "source" and why["adimages"] == "source"                    # N1
    assert why["wrong_field"] == "file" and why["two_sources"] == "file" and why["other_edge"] == "path"
    assert out["untouched"] == {"graph": True, "pieces": True}      # no Graph request, no piece request
    assert out["log"]["workers"] == 0                               # not even a worker was started


def test_stop_aborts_the_worker_request_and_the_result_is_not_a_success():
    out = _node({"name": "abort", "window": 1_000_000})
    assert out["abort"] == {"ok": True}
    assert out["result"].get("error") in ("aborted", "network", "no_file")


def test_the_helper_with_an_old_policy_refuses_served_chunks():
    policy = wp.policy_for_page(ACT)
    for k in ("served_chunk_max", "piece_max"):
        policy["upload"].pop(k)
    out = _node({"name": "upload", "window": 1_000_000, "piece": 400_000}, policy)
    assert out["results"][0]["error"] == "write_refused" and out["results"][0]["why"] == "source"


def test_mutating_the_served_cap_in_the_policy_is_caught_by_the_refusals():
    policy = wp.policy_for_page(ACT)
    policy["upload"]["served_chunk_max"] = 1 << 40
    out = _node({"name": "refusals"}, policy)
    assert out["refused"]["over_cap"].get("error") != "write_refused" or out["refused"]["over_cap"]["why"] != "chunk_size"


# ───────────────────────── the creator on a fake serving transport ─────────────────────────

class ServedMeta(ChunkMeta):
    """ChunkMeta whose transport can serve a file to the worker: it gets a `ServedChunk` and reads the window."""

    serves_chunks = True

    def __init__(self, *, probe=(True, ""), **kw):
        super().__init__(**kw)
        self.probe = probe
        self.probes = 0
        self.served_sizes: list[int] = []

    def probe_worker(self):
        self.probes += 1
        return self.probe

    def write(self, path, params, files=None, filename="", **kw):
        if files and isinstance(files.get("video_file_chunk"), wp.ServedChunk):
            chunk = files["video_file_chunk"]
            wp.check(ACT, "POST", path, params, files)                    # the Python line sees the ServedChunk
            self.served_sizes.append(len(chunk))
            files = {"video_file_chunk": chunk.read(0, len(chunk))}      # what the worker would have rebuilt
            real = wp.UPLOAD_STAGED_CHUNK_MAX
            wp.UPLOAD_STAGED_CHUNK_MAX = max(real, len(chunk))           # (the fake Meta re-checks the plain bytes)
            try:
                return super().write(path, params, files, filename, **kw)
            finally:
                wp.UPLOAD_STAGED_CHUNK_MAX = real
        return super().write(path, params, files, filename, **kw)


@pytest.fixture
def worker_small(monkeypatch):
    """The worker path with small numbers: single-request limit 1 MB, chunk cap 1 MB (production 16 MB and 64 MB)."""
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES_WORKER", MB)
    monkeypatch.setattr(cr, "VIDEO_CHUNK_MAX", MB)
    monkeypatch.setattr(wp, "UPLOAD_CHUNK_MAX", MB)
    monkeypatch.setattr(cr, "VIDEO_CHUNK_STAGED_MAX", MB // 2)
    monkeypatch.setattr(wp, "UPLOAD_STAGED_CHUNK_MAX", MB // 2)


def _no_compress(calls):
    def fn(path):
        calls.append(path)
        raise AssertionError("the worker path must not compress")
    return fn


def test_the_original_goes_up_in_chunks_of_the_meta_window_without_compression(worker_small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=MB)
    compressed: list[str] = []
    out, _ = _run(meta, d, compress_video=_no_compress(compressed))
    assert out.error == "" and compressed == [] and meta.probes == 1
    assert out.readback["ok"] is True
    sess = next(iter(meta.sessions.values()))
    assert sess["done"] and sess["sha"].hexdigest() == _sha(f)          # the same bytes as the file on disk
    assert sess["received"] == sess["size"] == f.stat().st_size         # the original size, not a compressed one
    assert [o for p, o in meta.phases if p == "transfer"] == [0, MB, 2 * MB, 3 * MB]
    assert meta.served_sizes == [MB, MB, MB, 500_000] and max(meta.served_sizes) <= MB
    assert len(_video_objects(meta)) == 1


def test_a_chunk_never_exceeds_the_cap_even_when_meta_offers_more(worker_small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=3 * MB)
    out, _ = _run(meta, d)
    assert out.error == ""
    assert max(meta.served_sizes) == MB                               # cap, not Meta's 3 MB window
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)


def test_a_smaller_meta_window_is_obeyed(worker_small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=600_000)
    out, _ = _run(meta, d, max_calls=300)
    assert out.error == "" and max(meta.served_sizes) == 600_000
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)


def test_windows_much_smaller_than_planned_stop_cleanly_before_more_is_sent(worker_small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=50_000)                 # the plan assumed MB-sized windows
    out, _ = _run(meta, d, max_calls=400)
    assert out.stop_kind == "video_error" and "upload windows" in out.error and "more calls" in out.error
    assert "ACL_VIDEO_WORKER_UPLOAD=0" in out.error
    assert meta.served_sizes == [] and not any(p == "finish" for p, _ in meta.phases)       # nothing sent, no finish


@pytest.mark.parametrize("fault", ["lost", "unsent", "refused_stored"])
def test_an_interrupted_chunk_resumes_without_duplicates(worker_small, tmp_path, fault):
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=MB)
    meta.chunk_faults = ["", fault]                                    # the second chunk is the interrupted one
    out, _ = _run(meta, d)
    assert out.error == "", out.error
    sess = next(iter(meta.sessions.values()))
    # Meta ends up with exactly the file: nothing missing, nothing twice
    assert sess["received"] == sess["size"] and sess["sha"].hexdigest() == _sha(f) and sess["done"]
    assert len(_video_objects(meta)) == 1


def test_an_impossible_offset_from_meta_stops_before_finish_on_the_worker_path(worker_small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=MB)
    meta.chunk_faults = ["bogus_ahead"]
    out, _ = _run(meta, d)
    assert out.error and "offset" in out.error and not any(p == "finish" for p, _ in meta.phases)


def test_a_chunk_over_the_policy_cap_is_refused_and_nothing_is_sent(worker_small, tmp_path, monkeypatch):
    d, f = _big_draft(tmp_path)
    monkeypatch.setattr(wp, "UPLOAD_CHUNK_MAX", MB // 4)               # policy tighter than the creator's cap
    meta = ServedMeta(meta_chunk=MB)
    out, _ = _run(meta, d)
    assert out.error and meta.served_sizes == []
    assert not any(p == "transfer" for p, _ in meta.phases)


def test_the_file_changing_during_the_upload_stops_it(worker_small, tmp_path):
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=MB)
    real = meta.write

    def write(path, params, files=None, filename="", **kw):
        got = real(path, params, files, filename, **kw)
        if params.get("upload_phase") == "transfer" and params["start_offset"] == "0":
            with open(f, "ab") as fh:
                fh.write(b"tail")
        return got

    meta.write = write
    out, _ = _run(meta, d, size_of=lambda p: Path(p).stat().st_size)
    assert out.error and "changed" in out.error


def test_when_the_probe_fails_the_old_path_compresses_and_says_so(worker_small, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=MB // 2, probe=(False, "worker blocked: SecurityError"))
    calls: list[str] = []

    def compress(path):
        calls.append(path)
        from types import SimpleNamespace
        return SimpleNamespace(compressed=False, path=path, original_mb=3.5, sent_mb=3.5, reason="")

    out, _ = _run(meta, d, compress_video=compress)
    assert out.error == "" and calls and meta.served_sizes == []       # bytes went the old way (8 MB -> 0.5 MB here)
    assert any("upload worker is not available" in w and "SecurityError" in w for w in out.warnings)
    assert next(iter(meta.sessions.values()))["sha"].hexdigest() == _sha(f)


def test_the_kill_switch_keeps_the_old_path_without_probing(worker_small, tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_VIDEO_WORKER_UPLOAD", "0")
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)
    d, f = _big_draft(tmp_path)
    meta = ServedMeta(meta_chunk=MB // 2)
    out, _ = _run(meta, d, compress_video=lambda p: __import__("types").SimpleNamespace(
        compressed=False, path=p, original_mb=3.5, sent_mb=3.5, reason=""))
    assert out.error == "" and meta.probes == 0 and meta.served_sizes == []


def test_a_transport_without_the_worker_path_keeps_the_old_path_silently(worker_small, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)
    d, f = _big_draft(tmp_path)
    meta = ChunkMeta(meta_chunk=MB // 2)                               # no probe_worker at all
    out, _ = _run(meta, d, compress_video=lambda p: __import__("types").SimpleNamespace(
        compressed=False, path=p, original_mb=3.5, sent_mb=3.5, reason=""))
    assert out.error == "" and not any("upload worker" in w for w in out.warnings)


def test_small_videos_do_not_probe_the_worker(worker_small, tmp_path):
    d, f = _big_draft(tmp_path, size=MB // 2)
    meta = ServedMeta()
    out, _ = _run(meta, d)
    assert meta.probes == 0


def test_check_plan_counts_the_original_and_the_64_mb_chunks(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_VIDEO_WORKER_UPLOAD", raising=False)
    import tests.test_acl2370_chunk_budget as T
    plan = T._plan(tmp_path, [2500], monkeypatch)
    budget, refusal = T.publish_api.create_call_budget(plan)
    assert refusal == ""                                              # a 2.5 GB file fits one run
    chunks = cr.estimate_upload_calls(plan, size_of=lambda p: 2500 * MB)
    assert 43 <= chunks <= 43 + 3
    assert budget >= chunks + cr.estimate_create_calls(plan)
