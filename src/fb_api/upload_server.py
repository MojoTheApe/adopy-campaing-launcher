"""ACL-2629: the controller side of the in-profile video upload.

A large video is not copied into the page. The page's Web Worker (see `token_capture`) asks for the bytes of one
chunk piece by piece at a same-origin address

    https://adsmanager.facebook.com/__acl_upload/<token>/<offset>/<length>

and this module answers those requests from the browser itself: the request is paused (CDP `Fetch.requestPaused`)
and fulfilled from the file on disk (`Fetch.fulfillRequest`). No request for such an address ever reaches Meta; the
bytes travel from this process to the browser process and into the worker as a binary response body - no base64
inside the page, no `atob`, nothing on the page's main thread.

Only windows registered by `register` can be served, only the exact pieces announced with their SHA-256 (the file
is read again here and compared before the piece leaves), and only while the chunk is armed. Everything else with
the prefix is failed (`BlockedByClient`). The interception is turned on for the prefix only (a light work tab
already intercepts its whole origin and is left alone). A dedicated worker has its own CDP session: it is attached
automatically (paused until the interception is on) and gets the same narrow pattern.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sys
from typing import Any, Callable

from src.fb_api import write_policy as _wp

ORIGIN = "https://adsmanager.facebook.com"
URL_PREFIX = ORIGIN + _wp.SERVE_PREFIX
PATTERN = URL_PREFIX + "*"
_PATH_RE = re.compile(r"/([a-z0-9]{16,40})/([0-9]{1,13})/([0-9]{1,13})")
#: How big a piece the worker asks for at once (it holds one piece in memory while checking it).
PIECE = _wp.UPLOAD_PIECE_MAX


class MemorySource:
    """A small in-memory source for `ServedChunk` (the probe)."""

    def __init__(self, data: bytes):
        self._data = bytes(data)

    def read_range(self, offset: int, n: int) -> bytes:
        return self._data[offset:offset + n]


class UploadServer:
    """Answers the worker's piece requests of ONE tab. Holds no bytes: reads the file when asked."""

    def __init__(self, tab: Any):
        self.tab = tab
        #: token -> (chunk, {(rel_offset, length): sha256})
        self._windows: dict[str, tuple[_wp.ServedChunk, dict[tuple[int, int], str]]] = {}
        self._session = ""          # the CDP session the interception was last armed for
        self._workers: dict[str, str] = {}    # worker session -> target id, for detaching after the upload
        self._chain: Callable[[dict], None] | None = None
        self._handler = self._on_event      # one bound method: identity checks against `tab.on_event` need it
        self.served = 0             # pieces answered
        self.served_bytes = 0
        self.blocked = 0            # requests with our prefix that were refused
        self.errors = 0
        self.refused_workers = 0    # worker sessions left paused because the interception could not be enabled
        # What the controller saw, for the probe's failure text (counts and short error names only: no URL, no token).
        self.attach_events = 0      # Target.attachedToTarget events of any kind
        self.workers_seen = 0       # ... of type worker
        self.workers_ready = 0      # workers whose interception was on and which were resumed
        self.piece_requests = 0     # requests with our prefix that reached the handler
        self.last_error = ""

    def _note(self, exc: BaseException) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {str(exc)[:80]}"

    def summary(self) -> str:
        """One short line: where the controller side of a failed probe stopped."""
        bits = [f"attach events {self.attach_events}", f"workers seen {self.workers_seen}",
                f"workers ready {self.workers_ready}", f"refused {self.refused_workers}",
                f"piece requests {self.piece_requests}", f"served {self.served}", f"errors {self.errors}"]
        text = "controller: " + ", ".join(bits)
        return text + (f" (last: {self.last_error})" if self.last_error else "")

    # -- window of one chunk
    def register(self, chunk: _wp.ServedChunk, *, piece: int = PIECE) -> tuple[str, list[dict]]:
        """Announce a chunk: returns (token, [{o, n, h}]) for the page helper. Reads the chunk once to hash it."""
        spans = chunk.pieces(piece)
        hashes = {span: hashlib.sha256(chunk.read(*span)).hexdigest() for span in spans}
        token = secrets.token_hex(12)
        self._windows[token] = (chunk, hashes)
        return token, [{"o": o, "n": n, "h": hashes[(o, n)]} for o, n in spans]

    def unregister(self, token: str) -> None:
        self._windows.pop(token, None)

    # -- the interception
    def arm(self) -> None:
        """Route the tab's events to `handle` and turn the interception on (again after a re-attach)."""
        tab = self.tab
        sid = str(getattr(tab, "session_id", "") or "")
        if tab.on_event is not self._handler:
            self._chain = tab.on_event
            tab.on_event = self._handler
        if sid == self._session and getattr(tab, "light", None) is None:
            return
        if getattr(tab, "light", None) is None:
            # An ordinary Ads Manager tab: pause only our prefix, nothing of the app's own traffic.
            tab.call("Fetch.enable", {"patterns": [{"urlPattern": PATTERN, "requestStage": "Request"}]}, timeout=10.0)
            try:
                # A service worker of the origin must not answer the inline mode's piece requests either.
                tab.call("Network.setBypassServiceWorker", {"bypass": True}, timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - best effort
                self._note(exc)
        # A light work tab intercepts its whole origin already (`light_tab`); enabling again would replace that.
        tab.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                 timeout=10.0)
        self._session = sid

    def _on_event(self, msg: dict) -> None:
        if not self.handle(msg) and self._chain is not None:
            self._chain(msg)

    def handle(self, msg: dict) -> bool:
        """True when the event was ours (a worker attach, or a request with our prefix)."""
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            self.attach_events += 1
            session = str(params.get("sessionId") or "")
            waiting = bool(params.get("waitingForDebugger"))
            if str(info.get("type") or "") in ("worker", "dedicated_worker"):
                self.workers_seen += 1
                self._prepare_worker(session, waiting, str(info.get("targetId") or ""))
            else:
                # Auto-attach is on for the whole tab: an iframe or a page's own worker that came up while it is on
                # must not stay paused. Resume it and let go of it - it is none of our business.
                self._release_other(session, waiting)
            return True
        if method != "Fetch.requestPaused":
            return False
        url = str((params.get("request") or {}).get("url") or "")
        if not url.startswith(URL_PREFIX):
            return False
        self.piece_requests += 1
        self._answer(str(msg.get("sessionId") or ""), str(params.get("requestId") or ""), url)
        return True

    def _call(self, session: str, method: str, args: dict) -> None:
        self.tab.call_session(session, method, args, timeout=30.0)

    def _release_other(self, session: str, waiting: bool) -> None:
        if not session:
            return
        if waiting:
            try:
                self._call(session, "Runtime.runIfWaitingForDebugger", {})
            except Exception:  # noqa: BLE001
                self.errors += 1
        try:
            self._call("", "Target.detachFromTarget", {"sessionId": session})
        except Exception as exc:  # noqa: BLE001
            self._note(exc)

    def _prepare_worker(self, session: str, waiting: bool, target_id: str = "") -> None:
        """Turn the interception on for a worker's session BEFORE it runs. Fail closed: when that cannot be done the
        worker is not resumed (and is closed), so none of its `__acl_upload` requests can leave the browser."""
        if not session:
            return
        try:
            self._call(session, "Fetch.enable", {"patterns": [{"urlPattern": PATTERN, "requestStage": "Request"}]})
        except Exception as exc:  # noqa: BLE001
            self._note(exc)
            self.refused_workers += 1
            if target_id:
                try:
                    self._call("", "Target.closeTarget", {"targetId": target_id})
                except Exception as exc:  # noqa: BLE001 - it stays paused: it never runs
                    self._note(exc)
            return
        self._workers[session] = target_id
        self.workers_ready += 1
        try:
            # A service worker of the origin must not answer (or pass on) the worker's piece requests.
            self._call(session, "Network.setBypassServiceWorker", {"bypass": True})
        except Exception as exc:  # noqa: BLE001 - best effort: the live check keeps watching for it
            self._note(exc)
        if waiting:
            try:
                self._call(session, "Runtime.runIfWaitingForDebugger", {})
            except Exception:  # noqa: BLE001
                self.errors += 1

    def disarm(self) -> None:
        """After a chunk (or the probe): auto-attach off, worker sessions detached, the narrow pattern removed."""
        tab = self.tab
        for session in list(self._workers):
            try:
                self._call("", "Target.detachFromTarget", {"sessionId": session})
            except Exception:  # noqa: BLE001
                self.errors += 1
        self._workers.clear()
        try:
            tab.call("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False, "flatten": True},
                     timeout=10.0)
            if getattr(tab, "light", None) is None:
                tab.call("Fetch.disable", {}, timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            self._note(exc)
        self._session = ""

    def _fail(self, session: str, rid: str, reason: str = "BlockedByClient") -> None:
        try:
            self._call(session, "Fetch.failRequest", {"requestId": rid, "errorReason": reason})
        except Exception as exc:  # noqa: BLE001 - never let a paused request hang the event pump
            self._note(exc)

    def _answer(self, session: str, rid: str, url: str) -> None:
        m = _PATH_RE.fullmatch(url[len(URL_PREFIX) - 1:].split("?", 1)[0])
        entry = self._windows.get(m.group(1)) if m else None
        if not m or entry is None:
            self.blocked += 1
            self._fail(session, rid)
            return
        chunk, hashes = entry
        span = (int(m.group(2)), int(m.group(3)))
        want = hashes.get(span)
        if want is None:
            self.blocked += 1
            self._fail(session, rid)
            return
        try:
            data = chunk.read(*span)
        except Exception as exc:  # noqa: BLE001 - the file vanished or cannot be read
            self._note(exc)
            self._fail(session, rid, "Failed")
            return
        if len(data) != span[1] or hashlib.sha256(data).hexdigest() != want:
            # The file changed after the chunk was announced: the worker must not get other bytes.
            self.blocked += 1
            self._fail(session, rid, "Failed")
            return
        try:
            self._call(session, "Fetch.fulfillRequest", {
                "requestId": rid, "responseCode": 200,
                "responseHeaders": [{"name": "Content-Type", "value": "application/octet-stream"},
                                    {"name": "Cache-Control", "value": "no-store"}],
                "body": base64.b64encode(data).decode("ascii")})
            self.served += 1
            self.served_bytes += len(data)
        except Exception:  # noqa: BLE001 - never leave the request paused: the worker would wait on it forever
            self.errors += 1
            self._fail(session, rid, "Failed")
