"""ACL-1856: durable progress of one campaign-creation operation (survives a restart and a stopped job).

One JSON file per operation under `<local config dir>/api_operations/`. The file never says what is true in
Meta: it only says what was sent, what Meta confirmed and which objects a resumed run must read back first.
Writes are atomic and taken under the same file lock as the other journals. A failed save BEFORE a send
blocks that send (`JournalError`); a failed save AFTER a send only leaves the stage `sent`, which the next
run treats as an unknown result and looks up instead of sending again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from src.fb_api.stop_log import atomic_write, file_lock

log = logging.getLogger(__name__)

SCHEMA = 1
#: A send younger than this with nothing found by lookup is "Meta may still be processing": do not send again.
UNKNOWN_HOLD_S = 600.0
#: The same for a write without a file (campaign, ad set, creative, ad): the lookups already waited about 25 s.
UNKNOWN_HOLD_WRITE_S = 60.0
#: Complete operations older than this are pruned when the journal is opened.
RETENTION_S = 30 * 24 * 3600.0

NOT_SENT, SENT, CONFIRMED, REJECTED, CANCELLED, UNKNOWN = (
    "not_sent", "sent", "confirmed", "rejected", "cancelled", "unknown")
#: Stage states that may still turn into an object in Meta.
PENDING_STATES = (SENT, UNKNOWN)

_ID_RE = re.compile(r"^op-[0-9a-f]{16}$")


class JournalError(RuntimeError):
    """Progress could not be saved: the write it guards must not be sent."""


class RequestConflict(ValueError):
    """The same request id was already used for a different plan revision."""


def default_dir() -> Path:
    from config import paths

    return Path(paths.get_local_config_dir()) / "api_operations"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def plan_fingerprint(plan: Any) -> str:
    """Immutable plan revision: everything the creator is asked to build (warnings are only notes)."""
    data = _jsonable(plan)
    if isinstance(data, dict):
        data.pop("warnings", None)
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def operation_id(act: str, tag: str, fingerprint: str) -> str:
    raw = f"{act}|{tag}|{fingerprint}".encode("utf-8")
    return "op-" + hashlib.sha256(raw).hexdigest()[:16]


class OperationJournal:
    """The durable record of one operation; every method that changes it saves it."""

    def __init__(self, path: Path, doc: dict, *, clock: Callable[[], float] = time.time):
        self.path = path
        self.doc = doc
        self.clock = clock
        self._lock = threading.RLock()
        #: Why the previous file could not be used ("" = nothing wrong).
        self.load_note = ""

    # ── open / load ─────────────────────────────────────────────
    @classmethod
    def open(cls, plan: Any, *, root: Path | str | None = None, request_id: str = "",
             clock: Callable[[], float] = time.time) -> "OperationJournal":
        """The journal of this account + plan revision; the same request returns the same operation."""
        base = Path(root) if root else default_dir()
        fp = plan_fingerprint(plan)
        op_id = operation_id(str(plan.act), str(plan.tag), fp)
        path = base / f"{op_id}.json"
        note = ""
        doc: dict | None = None
        with file_lock(path):
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if (isinstance(loaded, dict) and loaded.get("v") == SCHEMA
                            and loaded.get("operation_id") == op_id
                            and loaded.get("plan_fingerprint") == fp and isinstance(loaded.get("stages"), dict)):
                        doc = loaded
                    else:
                        note = "The saved progress of this campaign has an unknown format and was ignored."
                except (OSError, ValueError):
                    note = "The saved progress of this campaign could not be read and was ignored."
                if doc is None:
                    try:
                        os.replace(path, path.with_name(path.name + ".corrupt"))
                    except OSError:
                        pass
        now = clock()
        if doc is None:
            doc = {"v": SCHEMA, "operation_id": op_id, "account": str(plan.act), "plan_fingerprint": fp,
                   "campaign_name": str(plan.name), "label": str(plan.label), "state": "open",
                   "created_at": now, "updated_at": now, "stages": {}, "request_ids": []}
        j = cls(path, doc, clock=clock)
        j.load_note = note
        if request_id:
            j.bind_request(request_id)
        else:
            j._save()
        return j

    @staticmethod
    def request_owner(request_id: str, root: Path | str | None = None) -> dict | None:
        """The operation that already holds this request id (`{operation_id, plan_fingerprint}`), if any."""
        base = Path(root) if root else default_dir()
        if not request_id or not base.is_dir():
            return None
        for f in base.glob("op-*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and request_id in (d.get("request_ids") or []):
                return {"operation_id": d.get("operation_id"), "plan_fingerprint": d.get("plan_fingerprint")}
        return None

    @classmethod
    def prune(cls, root: Path | str | None = None, *, now: float | None = None) -> int:
        base = Path(root) if root else default_dir()
        if not base.is_dir():
            return 0
        now = time.time() if now is None else now
        removed = 0
        for f in base.glob("op-*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("state") == "complete" and now - float(d.get("updated_at") or now) > RETENTION_S:
                    f.unlink()
                    removed += 1
            except (OSError, ValueError, TypeError):
                continue
        return removed

    # ── saving ──────────────────────────────────────────────────
    def _save(self) -> None:
        with self._lock:
            self.doc["updated_at"] = self.clock()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with file_lock(self.path):
                    atomic_write(self.path, self.doc)
            except OSError as exc:
                raise JournalError(
                    "Progress could not be saved on this computer; nothing was written. "
                    f"Check the free disk space and try again ({exc.__class__.__name__}).") from exc

    def bind_request(self, request_id: str) -> None:
        with self._lock:
            ids = self.doc.setdefault("request_ids", [])
            owner = self.request_owner(request_id, self.path.parent)
            if owner and owner["operation_id"] != self.doc["operation_id"]:
                raise RequestConflict("This request ID was used for a different campaign plan.")
            if request_id not in ids:
                ids.append(request_id)
                del ids[:-20]
            self._save()

    # ── stages ──────────────────────────────────────────────────
    @property
    def operation_id(self) -> str:
        return str(self.doc["operation_id"])

    @property
    def stages(self) -> dict[str, dict]:
        return self.doc["stages"]

    def stage(self, key: str) -> dict:
        return self.stages.get(key) or {"state": NOT_SENT}

    def sent(self, key: str, kind: str, *, content_md5: str = "") -> None:
        """Record the intent BEFORE the request leaves. A failed save raises `JournalError`: do not send."""
        with self._lock:
            cur = self.stages.get(key)
            if cur and cur.get("state") == CONFIRMED:
                return       # a confirmed stage never goes back to "sent"
            self.stages[key] = {"kind": kind, "state": SENT, "object_id": (cur or {}).get("object_id", ""),
                                "content_md5": content_md5 or (cur or {}).get("content_md5", ""),
                                "sent_at": self.clock(), "confirmed_at": None, "note": ""}
            self.doc["state"] = "partial" if self.confirmed() else "open"
            self._save()

    def confirm(self, key: str, object_id: str, *, name: str = "", content_md5: str = "") -> bool:
        """Meta confirmed (answer or reconcile hit). False when the save failed: the object exists anyway."""
        return self._mark(key, CONFIRMED, object_id=str(object_id), name=name, content_md5=content_md5)

    def reject(self, key: str, note: str = "") -> bool:
        return self._mark(key, REJECTED, note=note)

    def cancel(self, key: str, note: str = "") -> bool:
        return self._mark(key, CANCELLED, note=note)

    def unknown(self, key: str, note: str = "") -> bool:
        return self._mark(key, UNKNOWN, note=note)

    def _mark(self, key: str, state: str, *, object_id: str = "", name: str = "", content_md5: str = "",
              note: str = "") -> bool:
        with self._lock:
            cur = dict(self.stages.get(key) or {"kind": "", "sent_at": self.clock()})
            if cur.get("state") == CONFIRMED and state != CONFIRMED:
                return True       # confirmed is final
            cur["state"] = state
            if object_id:
                cur["object_id"] = object_id
                cur["confirmed_at"] = self.clock()
            if name:
                cur["name"] = name
            if content_md5:
                cur["content_md5"] = content_md5
            if note:
                cur["note"] = note[:300]
            self.stages[key] = cur
            try:
                self._save()
            except JournalError:
                log.warning("operation %s: could not save stage %r as %s", self.operation_id, key, state)
                return False
            return True

    def unsent(self, key: str) -> None:
        """The send was refused locally (nothing left the machine): the stage is as if never begun."""
        with self._lock:
            cur = self.stages.get(key)
            if cur and cur.get("state") == SENT:
                if cur.get("object_id"):
                    cur["state"] = CONFIRMED
                else:
                    del self.stages[key]
                try:
                    self._save()
                except JournalError:
                    pass

    def finish(self, state: str) -> None:
        with self._lock:
            self.doc["state"] = state
            try:
                self._save()
            except JournalError:
                log.warning("operation %s: could not save final state %s", self.operation_id, state)

    # ── reading ─────────────────────────────────────────────────
    def confirmed(self) -> dict[str, dict]:
        return {k: v for k, v in self.stages.items() if v.get("state") == CONFIRMED and v.get("object_id")}

    def pending_hold(self, key: str) -> float:
        """Seconds still to wait before a `sent`/`unknown` stage may be treated as never created (0 = may)."""
        st = self.stages.get(key) or {}
        if st.get("state") not in PENDING_STATES:
            return 0.0
        limit = UNKNOWN_HOLD_S if st.get("kind") in ("image", "video") else UNKNOWN_HOLD_WRITE_S
        return max(0.0, limit - (self.clock() - float(st.get("sent_at") or 0.0)))

    def is_pending(self, key: str) -> bool:
        return (self.stages.get(key) or {}).get("state") in PENDING_STATES

    def view(self) -> dict:
        """What the manager sees: stages, partial ids and one plain next action (English)."""
        stages = [{"label": k, "state": v.get("state"), "id": v.get("object_id") or None}
                  for k, v in self.stages.items()]
        partial = [s["id"] for s in stages if s["state"] == CONFIRMED and s["id"]]
        pending = [s["label"] for s in stages if s["state"] in PENDING_STATES]
        if pending:
            hold = max((self.pending_hold(k) for k in self.stages), default=0.0)
            nxt = ("Wait about %d minutes, then publish again; the result of %s is not known yet."
                   % (max(1, round(hold / 60)), pending[0]) if hold > 0 else
                   "Publish again; the earlier attempt did not leave a visible object.")
        elif self.doc.get("state") == "complete":
            nxt = "Nothing to do."
        elif partial:
            nxt = "Publish again; what was already created is reused and nothing is created twice."
        else:
            nxt = "Publish again."
        return {"id": self.operation_id, "state": self.doc.get("state"), "stages": stages,
                "partial_ids": partial, "next_action": nxt}
