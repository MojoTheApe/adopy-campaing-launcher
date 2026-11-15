"""ACL-2637: the original-file upload runs in a separate tab of the profile whose document has no service worker.

The Ads Manager tab is always controlled by Facebook's service worker, and the ACL-2629 path refuses a controlled
page. Now the transport opens ONE more tab of the same profile with the light document, reloads it bypassing the
service worker if needed, CHECKS `navigator.serviceWorker.controller` is null, hands the token over and serves the
pieces there. Everything here is fake: no browser, no Meta.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import light_tab as lt  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT  # noqa: E402
from tests.test_acl1891_video_chunks import MB, PATH, _big_draft, _run  # noqa: E402
from tests.test_acl2629_worker_upload import (  # noqa: E402
    TRANSFER, ServedMeta, ServingTab, _Mem, _node, worker_small)  # noqa: F401 - fixture

SW_WHY = "worker: page is controlled by a service worker; inline: page is controlled by a service worker"
CONTROLLED = {"error": "no_worker", "controlled": True, "why": SW_WHY}


class _Doc:
    def __init__(self):
        self.intact = ""

    def document_intact(self, *, timeout=10.0):
        return self.intact


class CleanTab(ServingTab):
    """The separate upload tab (a light document)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.light = _Doc()
        self.closed = False

    def close(self):
        self.closed = True


def _graph(tab):
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    t._sleep = lambda s: None
    return t


@pytest.fixture
def clean(monkeypatch):
    """The transport opens `CleanTab`s; the document and the hand-over are faked (tested separately below)."""
    made: list[CleanTab] = []
    state = {"doc": ("doc", ""), "handover": "", "probe": {"ok": True, "bytes": 4096}}

    def factory():
        tab = CleanTab(probe_reply=state["probe"])
        made.append(tab)
        return tab

    monkeypatch.setattr(lt, "open_upload_document", lambda tab, **kw: state["doc"])
    monkeypatch.setattr(tc.InPageGraph, "receive_from", lambda self, src, **kw: state["handover"])
    state["factory"], state["made"] = factory, made
    return state


def test_a_service_worker_controlled_page_uploads_from_a_clean_tab_with_no_fallback(clean):
    main = ServingTab(probe_reply=CONTROLLED)
    t = _graph(main)
    t.upload_tab_factory = clean["factory"]
    ok, why = t.probe_worker()
    assert (ok, why) == (True, "") and t.upload_context == "clean_tab" and t.upload_mode == "worker"
    up = clean["made"][0]
    data = os.urandom(MB + 7)
    got = t.write(PATH, {**TRANSFER}, files={"video_file_chunk": wp.ServedChunk(_Mem(data), 0, len(data))},
                  filename="chunk")
    assert got == {"status": 200, "body": {"success": True}}
    served = [s for s in up.specs if "files" in s]
    assert len(served) == 1 and served[0]["files"] == {"video_file_chunk": "served"}
    assert served[0]["referrer"].startswith("https://adsmanager.facebook.com/adsmanager/manage/campaigns")
    assert not [s for s in main.specs if "files" in s]                   # nothing served from the controlled page
    assert any(m == "Fetch.enable" for m, _ in up.cdp_calls) is False     # a light tab keeps its own interception
    assert up.pumped                                                      # piece requests handled on the clean tab
    assert t.close_upload_context() is True and up.closed and t.upload_context == ""


def test_an_uncontrolled_page_keeps_uploading_from_itself_and_opens_no_tab(clean):
    t = _graph(ServingTab())
    t.upload_tab_factory = clean["factory"]
    assert t.probe_worker() == (True, "") and clean["made"] == [] and t.upload_context == ""


def test_a_clean_tab_that_is_controlled_too_is_refused_with_both_reasons(clean):
    clean["doc"] = (None, "upload tab is controlled by a service worker even after a reload that bypasses it")
    t = _graph(ServingTab(probe_reply=CONTROLLED))
    t.upload_tab_factory = clean["factory"]
    ok, why = t.probe_worker()
    assert ok is False
    assert SW_WHY in why and "controller: attach events 0" in why
    assert "clean upload tab: upload tab is controlled by a service worker even after a reload" in why
    assert clean["made"][0].closed and t._upload_graph is None


def test_a_clean_tab_whose_probe_fails_is_closed_and_says_why(clean):
    clean["probe"] = {"error": "no_worker", "controlled": True, "why": "worker: page is controlled by a service worker"}
    t = _graph(ServingTab(probe_reply=CONTROLLED))
    t.upload_tab_factory = clean["factory"]
    ok, why = t.probe_worker()
    assert ok is False and "clean upload tab: worker: page is controlled by a service worker" in why
    assert clean["made"][0].closed and t._upload_graph is None


def test_a_failed_token_hand_over_closes_the_clean_tab(clean):
    clean["handover"] = "handover_timeout"
    t = _graph(ServingTab(probe_reply=CONTROLLED))
    t.upload_tab_factory = clean["factory"]
    ok, why = t.probe_worker()
    assert ok is False and "token hand-over failed (handover_timeout)" in why and clean["made"][0].closed


def test_a_clean_tab_that_cannot_be_opened_is_a_reason_not_a_stop(clean):
    def boom():
        raise tc.CdpError("the browser did not open a tab")

    t = _graph(ServingTab(probe_reply=CONTROLLED))
    t.upload_tab_factory = boom
    ok, why = t.probe_worker()
    assert ok is False and "clean upload tab: could not be opened (CdpError" in why


def test_a_lost_clean_tab_is_no_file_and_is_closed(clean):
    t = _graph(ServingTab(probe_reply=CONTROLLED))
    t.upload_tab_factory = clean["factory"]
    assert t.probe_worker()[0] is True
    up = clean["made"][0]
    up.light.intact = "light_doc_replaced"                                # the document is not ours any more
    data = os.urandom(1000)
    got = t.write(PATH, {**TRANSFER}, files={"video_file_chunk": wp.ServedChunk(_Mem(data), 0, len(data))},
                  filename="chunk")
    assert got["error"] == "no_file" and "clean upload tab lost" in got["why"]
    assert up.closed and not [s for s in up.specs if "files" in s]


def test_a_new_probe_closes_the_previous_clean_tab(clean):
    t = _graph(ServingTab(probe_reply=CONTROLLED))
    t.upload_tab_factory = clean["factory"]
    t.probe_worker()
    t.probe_worker()
    assert clean["made"][0].closed and not clean["made"][1].closed
    t.close_upload_context()
    assert clean["made"][1].closed


def _light_with_clean_tab(clean):
    light = _graph(CleanTab(probe_reply=CONTROLLED))                    # a light work tab, controlled by the SW
    light.upload_tab_factory = clean["factory"]
    assert light.probe_worker()[0] is True and light.light
    return light


def test_a_light_lease_switched_to_ads_manager_keeps_uploading_from_the_clean_tab(clean):
    light = _light_with_clean_tab(clean)
    up = clean["made"][0]
    heavy = _graph(ServingTab(probe_reply=CONTROLLED))                   # the Ads Manager tab: controlled too
    light.leave_light = lambda: heavy
    client = g.GraphClient(light, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=10)
    client._leave_light_tab()
    assert client.transport is heavy and heavy.upload_context == "clean_tab" and light._upload_graph is None
    assert not up.closed
    data = os.urandom(1000)
    got = heavy.write(PATH, {**TRANSFER}, files={"video_file_chunk": wp.ServedChunk(_Mem(data), 0, len(data))},
                      filename="chunk")
    assert got == {"status": 200, "body": {"success": True}}
    assert [s for s in up.specs if "files" in s] and not [s for s in heavy.tab.specs if "files" in s]
    heavy.close_upload_context()
    assert up.closed


def test_a_new_transport_that_cannot_take_the_clean_tab_gets_it_closed(clean):
    light = _light_with_clean_tab(clean)
    heavy = SimpleNamespace(light=False, pin_page=lambda p: None)
    light.leave_light = lambda: heavy
    client = g.GraphClient(light, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=10)
    client._leave_light_tab()
    assert client.transport is heavy and clean["made"][0].closed and light._upload_graph is None


# ───────────────────────── open_upload_document: the controller is checked, not assumed ─────────────────────────

class DocTab:
    """A fake tab for `open_upload_document`: the light document is served, `controlled` answers per load."""

    def __init__(self, controlled_per_load, reload_serves=True):
        self.loads = 0
        self.controlled = list(controlled_per_load)
        self.calls: list[tuple[str, dict]] = []
        self.reload_serves = reload_serves
        self.light = None
        self.on_event = None
        self.closed = False
        self.doc = None

    def mark_light(self, doc):
        self.light = doc
        self.doc = doc

    def call(self, method, params=None, *, timeout=0):
        self.calls.append((method, params or {}))
        if method in ("Page.navigate",) or (method == "Page.reload" and self.reload_serves):
            self.loads += 1
            self.on_event({"method": "Fetch.requestPaused", "params": {
                "requestId": f"r{self.loads}", "resourceType": "Document", "request": {"url": lt.LIGHT_URL}}})
        return {}

    def create_isolated_world(self, name):
        return 1

    def evaluate_in(self, ctx, expr, *, timeout=10.0):
        if "serviceWorker" in expr:
            return self.controlled[min(self.loads, len(self.controlled)) - 1]
        return {"href": lt.LIGHT_URL, "ready": "complete", "marker": self.doc.marker, "t": float(self.loads)}

    def pump(self, seconds):
        return 0

    def close(self):
        self.closed = True


def test_an_uncontrolled_light_document_is_used_without_a_reload():
    tab = DocTab([False])
    doc, why = lt.open_upload_document(tab, timeout=1.0)
    assert doc is not None and why == ""
    assert not [c for c in tab.calls if c[0] == "Page.reload"]
    lt.settle_and_close(tab)


def test_a_controlled_document_is_reloaded_bypassing_the_service_worker_and_then_checked():
    tab = DocTab([True, False])
    doc, why = lt.open_upload_document(tab, timeout=1.0)
    assert doc is not None and why == ""
    assert ("Page.reload", {"ignoreCache": True}) in tab.calls
    assert doc.fulfilled == 2 and doc.time_origin == 2.0                 # the reload was answered by our interception
    lt.settle_and_close(tab)


def test_a_document_still_controlled_after_the_reload_is_refused():
    tab = DocTab([True, True])
    doc, why = lt.open_upload_document(tab, timeout=1.0)
    assert doc is None and why == "upload tab is controlled by a service worker even after a reload that bypasses it"


def test_an_unreadable_controller_state_is_refused():
    tab = DocTab([None])
    doc, why = lt.open_upload_document(tab, timeout=1.0)
    assert doc is None and "could not be read" in why


# ───────────────────────── the page helper: a service worker taking control mid-upload ─────────────────────────

def test_a_service_worker_claiming_the_page_mid_upload_stops_the_worker_before_any_post():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 200_000, "claim_at": 2})
    assert out["results"][0] == {"error": "no_file",
                                 "why": "a service worker took control of the page during the upload"}
    assert [x["phase"] for x in out["log"]["graph"]] == ["start", "finish"]       # no transfer was posted


def test_a_service_worker_claiming_the_page_mid_upload_stops_inline_before_the_next_piece():
    out = _node({"name": "upload", "probe_first": True, "worker_blocked": True, "window": 1_000_000,
                 "piece": 200_000, "claim_at": 6})                       # 4 probe pieces, then the 2nd upload piece
    assert out["probe"]["mode"] == "inline"
    assert out["results"][0] == {"error": "no_file",
                                 "why": "a service worker took control of the page during the upload"}
    assert out["log"]["pieces"] == 6
    assert [x["phase"] for x in out["log"]["graph"]] == ["start", "finish"]


def test_a_service_worker_taking_control_during_the_post_never_cancels_it():
    out = _node({"name": "upload", "window": 1_000_000, "piece": 200_000, "claim_on_post": True})
    assert out["results"][0]["ok"] is True                              # the started POST went through
    assert out["meta"]["chunks"] == [[0, 1_000_000]]
    assert out["results"][1] == {"error": "no_file", "why": "page is controlled by a service worker"}


# ───────────────────────── the creator: the clean tab is closed, the fallback says one clear thing ─────────────────

class ClosingMeta(ServedMeta):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.closed = 0
        self.upload_context = "clean_tab"
        self.upload_mode = "worker"

    def close_upload_context(self):
        self.closed += 1
        return True


def _no_compress(path):
    raise AssertionError("must not compress")


def test_the_run_closes_the_clean_tab_when_it_succeeds(worker_small, tmp_path):
    d, _ = _big_draft(tmp_path)
    meta = ClosingMeta(meta_chunk=MB)
    out, _ = _run(meta, d, compress_video=_no_compress)
    assert out.error == "" and meta.closed == 1
    assert any("separate tab without a service worker" in s for s in out.steps)


def test_the_run_closes_the_clean_tab_when_it_stops(worker_small, tmp_path):
    d, _ = _big_draft(tmp_path)
    meta = ClosingMeta(meta_chunk=MB)
    meta.chunk_faults = ["bogus_ahead"]
    out, _ = _run(meta, d, compress_video=_no_compress)
    assert out.error and meta.closed == 1


def test_a_file_the_fallback_cannot_take_gets_one_clear_line_and_no_upload(worker_small, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "VIDEO_SINGLE_MAX_BYTES", MB)
    monkeypatch.setattr(cr, "VIDEO_MAX_BYTES_STAGED", 2 * MB)            # production: 1536 MB
    d, _ = _big_draft(tmp_path)                                           # 3.5 MB
    meta = ServedMeta(meta_chunk=MB // 2, probe=(False, SW_WHY + " | controller: attach events 0"))

    def compress(path):
        return SimpleNamespace(compressed=False, path=path, original_mb=3.5, sent_mb=3.5, reason="ffmpeg not found")

    monkeypatch.setattr(cr, "video_compress_limit_mb", lambda: 1.0)
    out, _ = _run(meta, d, compress_video=compress)
    assert out.stop_kind == "plan" and not meta.sessions                  # nothing was uploaded
    assert out.error == ("ad 1: the video is 4 MB; the in-profile upload is not available (" + SW_WHY + ") and "
                         "the fallback upload accepts at most 2 MB (not compressed: ffmpeg not found). "
                         "Nothing more was created.")
    assert not any("uploading the original" in w or "compressed and uploaded the slower way" in w
                   for w in out.warnings)
    assert any("controller: attach events 0" in s for s in out.steps)    # the full probe text stays in the steps
