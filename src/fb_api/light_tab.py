"""ACL-2344: the light work tab - Graph calls from a tiny page of the Ads Manager origin.

The work tab never loads the Ads Manager app. Its only document, `LIGHT_URL`, is written by the browser
itself (`Fetch.fulfillRequest`): no request for it reaches Meta. Interception is on the page ORIGIN only
(`https://adsmanager.facebook.com/*`) and stays on for the whole life of the tab, so a reload, a favicon or
any other request to that origin is answered locally (the document again) or failed - never sent.
Graph hosts (`graph.facebook.com`, `<word>-graph.facebook.com`) do not match the pattern: Graph calls of
the helper go to the network through the profile's own proxy exactly like today, and their URLs (which
carry the token) never pass through Python. `Network.*` is never enabled here.

A service worker of the origin could in theory answer the navigation before our interception; the page
then is not ours. Every document gets a random marker; `document_intact` checks it (and the document's
`timeOrigin`) before a call, so a foreign or reloaded page is noticed without any request to Meta.

The token is handed over by the helper itself (`listen`/`handOver`, see `token_capture`): Python sees
only status codes.
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import contextvars
import json
import os
import secrets
import time
import weakref
from typing import Any

from src.fb_api.cdp import CdpError, PageTab

LIGHT_ORIGIN = "https://adsmanager.facebook.com"
LIGHT_PATH = "/__acl_work"
LIGHT_URL = LIGHT_ORIGIN + LIGHT_PATH
#: The document is inert: no script, worker, frame, object, form or base; the icon is inline (no request).
#: `connect-src` is deliberately not restricted - the helper's Graph fetch must reach the network.
CSP = ("script-src 'none'; worker-src 'none'; frame-src 'none'; object-src 'none'; "
       "base-uri 'none'; form-action 'none'; img-src data:")
FLAG = "ACL_LIGHT_WORK_TAB"

#: Fallback reasons (spec 5.5); the only texts that leave this module besides booleans.
REASONS = frozenset({"handover_no_listener", "handover_timeout", "handover_rejected", "light_doc_failed",
                     "light_doc_wrong_origin", "light_doc_replaced", "work_tab_dead", "flag_off",
                     "form_photo_upload"})


def enabled(env: dict | None = None) -> bool:
    """The switch, read once per lease by the caller (spec 5.1). Default off."""
    value = (env if env is not None else os.environ).get(FLAG, "")
    return str(value).strip().lower() in ("1", "true", "yes")


#: ACL-2593: a lease that is known to need the Ads Manager page (a form-background upload) says so here, before
#: the lease starts. Not a failure of the light page: it is not counted as a light fallback.
_FORCED: "contextvars.ContextVar[str]" = contextvars.ContextVar("acl_light_forced_reason", default="")


@contextlib.contextmanager
def force_ads_manager(reason: str):
    """Leases prepared inside this block run on the Ads Manager tab (`reason` is in `REASONS`)."""
    if reason not in REASONS:
        raise ValueError("unknown light fallback reason")
    token = _FORCED.set(reason)
    try:
        yield
    finally:
        _FORCED.reset(token)


def forced_reason() -> str:
    return _FORCED.get()


def _body(marker: str) -> bytes:
    return ("<!doctype html><meta charset=utf-8><meta name=acl-light content=%s>"
            "<link rel=icon href=\"data:,\"><title>ACL</title>" % marker).encode()


def _origin_pattern() -> str:
    return LIGHT_ORIGIN + "/*"


class LightDocument:
    """Interception state of one work tab. Holds only the marker and counters - no URLs, no tokens."""

    def __init__(self, tab: Any):
        self.tab = tab
        self.marker = ""
        self.time_origin: float | None = None
        self.fulfilled = 0
        self.blocked = 0
        self.errors = 0

    # -- events of the tab (handled on every call of the tab and in `pump`)
    def on_event(self, msg: dict) -> None:
        if msg.get("method") != "Fetch.requestPaused":
            return
        params = msg.get("params") or {}
        rid = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        try:
            if url == LIGHT_URL and params.get("resourceType") == "Document":
                # every load of the light URL (also a reload) gets the document again, with a NEW marker:
                # a reloaded page has lost the token, and `document_intact` sees the marker change
                self.marker = secrets.token_hex(12)
                self.fulfilled += 1
                self.tab.call("Fetch.fulfillRequest", {
                    "requestId": rid, "responseCode": 200,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"},
                                        {"name": "Cache-Control", "value": "no-store"},
                                        {"name": "Content-Security-Policy", "value": CSP}],
                    "body": base64.b64encode(_body(self.marker)).decode()}, timeout=10.0)
            else:
                self.blocked += 1               # nothing of the page origin ever goes to Meta
                self.tab.call("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"},
                              timeout=10.0)
        except Exception:  # noqa: BLE001 - never let a paused request hang the tab's event pump
            self.errors += 1

    def intercept(self, *, timeout: float = 10.0) -> None:
        """Turn the interception on for the tab's (current) CDP session. Again after a re-attach."""
        self.tab.on_event = self.on_event
        self.tab.call("Fetch.enable", {"patterns": [{"urlPattern": _origin_pattern(), "requestStage": "Request"}]},
                      timeout=timeout)

    # -- local checks (no request to Meta)
    def _read(self, expression: str, *, timeout: float = 10.0) -> Any:
        ctx = self.tab.create_isolated_world("acl_light_check")
        return self.tab.evaluate_in(ctx, expression, timeout=timeout)

    def snapshot(self, *, timeout: float = 10.0) -> dict:
        got = self._read("(() => { const m = document.querySelector('meta[name=acl-light]');"
                         " return {href: location.href, ready: document.readyState,"
                         " marker: m ? m.content : '', t: performance.timeOrigin}; })()", timeout=timeout)
        return got if isinstance(got, dict) else {}

    def document_intact(self, *, timeout: float = 10.0) -> str:
        """'' when the tab still shows OUR document of this lease, else a reason code."""
        try:
            snap = self.snapshot(timeout=timeout)
        except (CdpError, TimeoutError):
            return "work_tab_dead"
        if snap.get("href") != LIGHT_URL:
            return "light_doc_wrong_origin"
        if not self.marker or snap.get("marker") != self.marker:
            return "light_doc_replaced"
        if self.time_origin is not None and snap.get("t") != self.time_origin:
            return "light_doc_replaced"
        return ""


def open_light_document(tab: Any, *, timeout: float = 15.0) -> tuple[LightDocument | None, str]:
    """Load the light document into `tab` (a fresh tab of the profile). Returns (doc, '') or (None, reason).

    Spec 5.6: the final URL must be exactly `LIGHT_URL` and the document ours (marker) and `complete`
    within `timeout`; otherwise the caller closes the tab and falls back to the Ads Manager mode.
    """
    doc = LightDocument(tab)
    try:
        doc.intercept()
        tab.mark_light(doc)
        tab.call("Page.navigate", {"url": LIGHT_URL}, timeout=timeout)
        deadline = time.monotonic() + timeout
        while True:
            snap = doc.snapshot()
            href = str(snap.get("href") or "")
            if href and href != "about:blank" and href != LIGHT_URL:
                return None, "light_doc_wrong_origin"
            if href == LIGHT_URL and snap.get("ready") == "complete":
                if not doc.marker or snap.get("marker") != doc.marker:
                    return None, "light_doc_replaced"      # not our document (e.g. a service worker answered)
                doc.time_origin = snap.get("t")
                _OPEN.add(tab)
                return doc, ""
            if time.monotonic() >= deadline:
                return None, "light_doc_failed"
            tab.pump(0.2)
    except (CdpError, TimeoutError):
        return None, "light_doc_failed"


def hand_over(src: Any, dst: Any, *, wait_s: float = 8.0) -> str:
    """Token from the helper of `src` to the helper of `dst` (two `InPageGraph`s). '' or a reason code.

    The channel name, nonce and ack name are random, live only for this call and are never logged.
    `dst` accepts exactly one message with the right nonce; whatever happens, its channel is closed.
    """
    name, nonce, ack = (secrets.token_urlsafe(18) for _ in range(3))
    args = ",".join(json.dumps(v) for v in (name, nonce, ack))
    ttl_ms = int((wait_s + 5.0) * 1000)
    try:
        got = dst._eval_helper(f".listen({args},{ttl_ms})", timeout=10.0)
    except (CdpError, TimeoutError):
        return "work_tab_dead"
    if got != "listening":
        return "handover_no_listener"
    sent = ""
    try:
        sent = src._eval_helper(f".handOver({args},{int(wait_s * 1000)})", timeout=wait_s + 10.0)
    except (CdpError, TimeoutError):
        sent = ""
    try:
        status = dst._eval_helper(".listenStatus()", timeout=10.0)
        if status == "listening":
            dst._eval_helper(".stopListen()", timeout=10.0)
    except (CdpError, TimeoutError):
        return "work_tab_dead"
    # the receiver decides: it validated and stored the token. The sender's ack (`sent`) may be lost on a
    # busy machine (its evaluate timed out) although the token arrived - that is not a failure.
    if status == "received":
        return ""
    if status == "rejected":
        return "handover_rejected"
    return "handover_timeout"


def capture_into(work: Any, open_tab: Any, act: str, *, timeout: float = 90.0) -> dict:
    """Take the token again for the light graph `work`: one capture tab, ONE Ads Manager load, hand-over,
    capture tab closed. Returns the state: the capture tab's (login/checkpoint/no token) or the work graph's.

    `open_tab()` opens an own tab of the same profile (the lease's tab factory). The capture tab is closed
    in every case; the work tab is never reloaded or closed here.
    """
    from src.fb_api.token_capture import InPageGraph

    began = time.monotonic()
    capture = None
    result: dict = {"kind": "unknown", "has_token": False, "light": True}
    reason = "work_tab_dead"
    try:
        capture = open_tab()
        cap = InPageGraph(capture)
        cap.navigate(act, timeout=min(60.0, timeout))
        state = cap.wait_ready(timeout=timeout)
        if state.get("kind") in ("login", "checkpoint") or not state.get("has_token"):
            reason = "capture_" + str(state.get("kind") or "no_token")
            result = {"kind": state.get("kind") or "other", "has_token": False}
            return result
        reason = work.receive_from(cap)
        if not reason:
            doc = getattr(work.tab, "light", None)
            if doc is not None:
                # the light document may have been served again (reload): this one is ours now
                snap = doc.snapshot()
                if snap.get("href") == LIGHT_URL and snap.get("marker") == doc.marker:
                    doc.time_origin = snap.get("t")
            result = work.state()
            if not result.get("has_token"):
                reason = "handover_rejected"
        return result
    except Exception as exc:  # noqa: BLE001 - a stop of the capture tab is the caller's to raise
        from src.fb_api.graph import GraphStop

        if isinstance(exc, GraphStop):
            raise
        return result
    finally:
        closed = settle_and_close(capture)
        mb = heap_used_mb(getattr(work, "tab", None))
        _record({"event": "fb_api_tab_context", "phase": "light_recapture", "mode": "light",
                 **({"work_tab_mb": mb} if mb is not None else {}),
                 "handover_ok": not reason, "fallback_reason": reason or None,
                 "capture_tab_closed": closed,
                 "capture_open_s": round(time.monotonic() - began, 1),
                 "recapture_count": getattr(work, "light_recaptures", 0)})


def heap_used_mb(tab: Any) -> float | None:
    """ACL-2523: JS heap of the work tab in MB (`Performance.getMetrics` JSHeapUsedSize), `None` when it
    cannot be read. A number only - never page content, URLs or tokens. Never raises."""
    try:
        tab.call("Performance.enable", timeout=5.0)
        metrics = (tab.call("Performance.getMetrics", timeout=5.0) or {}).get("metrics")
        for item in metrics if isinstance(metrics, list) else []:
            if isinstance(item, dict) and item.get("name") == "JSHeapUsedSize":
                value = item.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    return round(float(value) / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001 - telemetry only
        pass
    return None


def _record(row: dict) -> None:
    """Telemetry row; only codes, counts and seconds (no URLs, ids, channel names or tokens). Never raises."""
    try:
        from src.fb_api import launch as _launch
        _launch._record({"ok": True, **row})
    except Exception:  # noqa: BLE001
        pass


def open_work_tab(cdp_url: str) -> tuple[Any, LightDocument | None, str]:
    """A new own tab of the profile with the light document; on failure the tab is closed again."""
    tab = PageTab.open(cdp_url)
    doc, reason = open_light_document(tab)
    if doc is None:
        settle_and_close(tab)
        return None, None, reason
    return tab, doc, ""


#: Light tabs of THIS process. The interception lives only as long as our CDP connection: when the process
#: ends, a light tab left open would send `/__acl_work` to Meta on a reload (F5, discard/restore, session
#: restore). A normal exit closes them (`_close_all`); after a crash the restored-tab clean-up of the next
#: open closes the orphan (`cdp.is_light_work_url`). Residual risk: a reload in the gap between a crash and
#: that clean-up is one GET of the page origin with the profile's own cookies and proxy - less than the
#: restored Ads Manager tab of today.
_OPEN: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _close_all() -> None:
    for tab in list(_OPEN):
        try:
            if not getattr(tab, "closed", False):
                tab.close()
        except Exception:  # noqa: BLE001 - shutting down
            pass


atexit.register(_close_all)


def settle_and_close(tab: Any) -> bool:
    """Answer what is still paused, then close: nothing is left waiting, nothing goes out.

    True when no tab is left open (also when there was none), False when `close()` failed.
    """
    if tab is None:
        return True
    _OPEN.discard(tab)
    try:
        tab.pump(0.3)
    except Exception:  # noqa: BLE001
        pass
    try:
        tab.close()
    except Exception:  # noqa: BLE001
        return False
    return True
