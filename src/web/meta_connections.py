"""Local preparation of isolated Meta applications (ACL-1872).

No Meta/browser calls and no transport selection. Mapping and registry writers
share one lock order: thread -> registry file -> map file -> encrypted vault.
Metadata commits disable old bindings before cleanup; App ownership is permanent.
"""
from __future__ import annotations

import csv
import contextvars
import json
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config import paths, protected_secrets as vault

_lock = threading.RLock()
_ACTIVE = {"awaiting_app", "awaiting_credential", "saved_unverified"}
_RETIRED = {"binding_changed", "disconnected"}
_PHASES = _ACTIVE | _RETIRED | {"cleanup_pending"}
_MAX_REGISTRY = 4 * 1024 * 1024
_directory = contextvars.ContextVar("meta_setup_directory", default=None)


class ConnectionError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _store() -> Path:
    return Path(_directory.get() or paths.get_local_config_dir()) / "meta-connections.json"


@contextmanager
def transaction(map_path: str | Path, *, config_directory=None):
    """Also used by all existing map writers. Never acquire in reverse order."""
    context = _directory.set(config_directory)
    try:
        with _lock:
            with paths.machine_file_lock(_store().with_suffix(".lock")):
                with paths.machine_file_lock(Path(str(map_path) + ".lock")):
                    yield
    finally:
        _directory.reset(context)


def _uid(value) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ConnectionError("A valid physical profile UUID is required.", 400) from None


def _digits(value, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,39}", value.strip()):
        raise ConnectionError(f"{label} must be a numeric ID, entered as text.", 400)
    return value.strip()


def _pair(row: dict) -> tuple[str, str] | None:
    try:
        profile = _uid(row.get("profile_id"))
        raw = str(row.get("ad_account_id") or "").strip()
        account = _digits(raw.removeprefix("act_"), "Ad account ID")
        return profile, account
    except ConnectionError:
        return None


def _rows(map_path) -> list[dict]:
    try:
        with Path(map_path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"account_name", "profile_id", "ad_account_id"}.issubset(reader.fieldnames or []):
                raise ValueError
            return list(reader)
    except Exception:
        raise ConnectionError("Account mapping unavailable; check the Accounts setup.") from None


def _empty() -> dict:
    return {"schema": 1, "connections": {}, "app_owners": {}}


def _load() -> dict:
    source = _store()
    if not source.exists():
        return _empty()
    try:
        if source.stat().st_size > _MAX_REGISTRY:
            raise ValueError
        data = json.loads(source.read_bytes())
        if (not isinstance(data, dict) or set(data) != {"schema", "connections", "app_owners"} or data.get("schema") != 1
                or not isinstance(data.get("connections"), dict)
                or not isinstance(data.get("app_owners"), dict)):
            raise ValueError
        valid_pairs = set()
        for cid, item in data["connections"].items():
            allowed = {"connection_id", "profile_uuid", "ad_account_id", "account_name", "app_id",
                       "credential_ref", "revision", "binding_revision", "binding_valid", "credential_revision",
                       "phase", "requests", "cleanup", "created_at", "updated_at", "retired_phase"}
            if (str(uuid.UUID(cid)) != cid or not isinstance(item, dict)
                    or set(item) - allowed
                    or item["connection_id"] != cid or _uid(item["profile_uuid"]) != item["profile_uuid"]
                    or _digits(item["ad_account_id"], "Ad account ID") != item["ad_account_id"]
                    or item["phase"] not in _PHASES
                    or type(item["revision"]) is not int or item["revision"] < 1
                    or type(item["credential_revision"]) is not int or item["credential_revision"] < 0
                    or type(item["binding_revision"]) is not int or item["binding_revision"] < 1
                    or type(item["binding_valid"]) is not bool
                    or not isinstance(item["account_name"], str)
                    or not isinstance(item["app_id"], str)
                    or not isinstance(item["credential_ref"], str)
                    or not isinstance(item["requests"], dict) or not isinstance(item["cleanup"], list)):
                raise ValueError
            if "retired_phase" in item and (not isinstance(item["retired_phase"], str)
                                           or item["retired_phase"] not in _RETIRED):
                raise ValueError
            # Validate the persisted retirement before reconciliation can write
            # or cleanup can delete evidence. Active records may retain history.
            if item["phase"] == "cleanup_pending" and (
                    "retired_phase" not in item or not item["cleanup"] or item["credential_ref"]
                    or item["binding_valid"] != (item["retired_phase"] == "disconnected")):
                raise ValueError
            if item["binding_valid"]:
                pair = (item["profile_uuid"], item["ad_account_id"])
                if pair in valid_pairs:
                    raise ValueError
                valid_pairs.add(pair)
            for request_id, marker in item["requests"].items():
                if (not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id) or not isinstance(marker, dict)
                        or set(marker) != {"operation", "expected_revision", "app_id"}
                        or marker["operation"] not in {"save", "disconnect"}
                        or type(marker["expected_revision"]) is not int or marker["expected_revision"] < 1
                        or not isinstance(marker["app_id"], str)):
                    raise ValueError
                if marker["operation"] == "save":
                    _digits(marker["app_id"], "App ID")
                    if data["app_owners"].get(marker["app_id"]) != cid:
                        raise ValueError
                elif marker["app_id"]:
                    raise ValueError
            if item.get("app_id") and data["app_owners"].get(item["app_id"]) != cid:
                raise ValueError
            if item.get("credential_ref"):
                if str(uuid.UUID(item["credential_ref"])) != item["credential_ref"]:
                    raise ValueError
            for pending in item["cleanup"]:
                if (not isinstance(pending, dict) or set(pending) != {"ref", "revision"}
                        or str(uuid.UUID(pending["ref"])) != pending["ref"]
                        or type(pending["revision"]) is not int or pending["revision"] < 1
                        or pending["ref"] == item.get("credential_ref")):
                    raise ValueError
        for app, owner in data["app_owners"].items():
            if _digits(app, "App ID") != app or owner not in data["connections"]:
                raise ValueError
        return data
    except Exception:
        raise ConnectionError("Saved Meta setup is unreadable; restore its local backup before saving.", 503) from None


def _write(data: dict):
    try:
        content = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode()
        if len(content) > _MAX_REGISTRY:
            raise ValueError
        paths._atomic_write(_store(), content)
    except Exception:
        raise ConnectionError("Could not save Meta setup. Reopen saved setup to check its current state.", 503) from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retire(item: dict, phase: str):
    if phase == "binding_changed":
        item["binding_valid"] = False
    if not item["binding_valid"]:
        phase = "binding_changed"
    if item.get("credential_ref"):
        item["cleanup"].append({"ref": item["credential_ref"], "revision": item["credential_revision"]})
    item["credential_ref"] = ""
    item["retired_phase"] = phase
    item["phase"] = "cleanup_pending" if item["cleanup"] else phase
    item["revision"] += 1
    item["binding_revision"] += 1
    item["updated_at"] = _now()


def _cleanup(data: dict) -> bool:
    changed = False
    for cid, item in data["connections"].items():
        kept = []
        for pending in item["cleanup"]:
            try:
                vault.delete(pending["ref"], cid, pending["revision"], directory=_store().parent)
                changed = True
            except vault.SecretUnavailable:
                kept.append(pending)
        item["cleanup"] = kept
        if item["phase"] == "cleanup_pending" and not kept:
            item["phase"] = item.get("retired_phase", "disconnected")
            changed = True
    return changed


def _reconcile(data: dict, rows: list[dict]) -> bool:
    changed = False
    for item in data["connections"].values():
        if not item["binding_valid"] or item["phase"] not in _ACTIVE | {"disconnected", "cleanup_pending"}:
            continue
        pair = (item["profile_uuid"], item["ad_account_id"])
        matching = [r for r in rows if _pair(r) == pair]
        if len(matching) != 1:
            _retire(item, "binding_changed")
            changed = True
        else:
            label = matching[0].get("account_name", "")
            if item["account_name"] != label:
                item["account_name"] = label
                changed = True
    return changed


def mapping_will_change(rows: list[dict]):
    """Called under transaction BEFORE the map commit: failures are fail-closed.

    Even if the CSV replace fails, an affected old binding remains retired and
    cannot spring back to life when a mapping changes away and later back.
    """
    data = _load()
    if _reconcile(data, rows):
        _write(data)  # First durable disable, then best-effort encrypted cleanup.
        if _cleanup(data):
            _write(data)


def _current(data: dict, rows: list[dict]):
    if _reconcile(data, rows):
        _write(data)
    if _cleanup(data):
        _write(data)


def _item(data: dict, cid: str) -> dict:
    try:
        if not isinstance(cid, str) or str(uuid.UUID(cid)) != cid:
            raise ValueError
        return data["connections"][cid]
    except (KeyError, ValueError, AttributeError, TypeError):
        raise ConnectionError("Meta connection not found.", 404) from None


def _dto(item: dict) -> dict:
    phase = item["phase"]
    available = False
    if item.get("credential_ref") and phase in _ACTIVE:
        try:
            vault.get(item["credential_ref"], item["connection_id"], item["credential_revision"], directory=_store().parent)
            available = True
        except vault.SecretUnavailable:
            pass
    message = {
        "awaiting_app": "Enter this connection's separate App ID and official credential.",
        "awaiting_credential": "Save a new official credential for this application.",
        "saved_unverified": "Separate app details saved. API access is not verified.",
        "binding_changed": "Profile or ad account mapping changed. Prepare a new separate connection.",
        "disconnected": "Disconnected locally. Meta access and the application are not revoked or deleted.",
        "cleanup_pending": "Disconnected locally; protected credential cleanup needs another attempt.",
    }[phase]
    if phase == "saved_unverified" and not available:
        message = "Protected credential unavailable. Save a new credential on this Windows user account."
    elif phase == "saved_unverified" and item["cleanup"]:
        message += " Previous protected credential cleanup needs another attempt."
    return {key: item[key] for key in ("connection_id", "account_name", "profile_uuid", "ad_account_id",
                                      "app_id", "revision", "binding_revision", "credential_revision", "phase")} | {
        "has_credential": available, "api_access": "unverified", "next_action": message,
        "can_save": item["binding_valid"] and (phase in _ACTIVE or phase == "disconnected"),
        "cleanup_pending": bool(item["cleanup"]),
    }


def begin(map_path, account_name: str) -> dict:
    if not isinstance(account_name, str) or not account_name.strip() or len(account_name) > 300:
        raise ConnectionError("Select a mapped account.", 400)
    with transaction(map_path):
        rows, data = _rows(map_path), _load()
        _current(data, rows)
        matches = [r for r in rows if str(r.get("account_name") or "").strip().casefold() == account_name.strip().casefold()]
        if len(matches) != 1:
            raise ConnectionError("Account mapping is missing or ambiguous; check the Accounts setup.")
        pair = _pair(matches[0])
        if not pair or sum(_pair(r) == pair for r in rows) != 1:
            raise ConnectionError("Set a unique physical profile UUID and ad account ID in Accounts first.")
        for item in data["connections"].values():
            if ((item["profile_uuid"], item["ad_account_id"]) == pair and item["binding_valid"]
                    and item["phase"] in _ACTIVE | {"disconnected", "cleanup_pending"}):
                return _dto(item)
        cid = str(uuid.uuid4())
        item = {"connection_id": cid, "profile_uuid": pair[0], "ad_account_id": pair[1],
                "account_name": matches[0]["account_name"], "app_id": "", "credential_ref": "",
                "revision": 1, "binding_revision": 1, "credential_revision": 0, "binding_valid": True,
                "phase": "awaiting_app", "requests": {}, "cleanup": [],
                "created_at": _now(), "updated_at": _now()}
        data["connections"][cid] = item
        _write(data)
        return _dto(item)


def status(map_path, cid: str) -> dict:
    with transaction(map_path):
        data = _load()
        _current(data, _rows(map_path))
        return _dto(_item(data, cid))


def _request(item: dict, expected_revision, request_id, operation: str, app_id: str = "") -> bool:
    if (type(expected_revision) is not int or expected_revision < 1
            or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id)):
        raise ConnectionError("A current revision and valid request ID are required.", 400)
    marker = {"operation": operation, "expected_revision": expected_revision, "app_id": app_id}
    previous = item["requests"].get(request_id)
    if previous:
        if previous != marker:
            raise ConnectionError("Request ID already used for another change.")
        return True
    if item["revision"] != expected_revision:
        raise ConnectionError("This setup changed in another window. Reopen it before saving.")
    if len(item["requests"]) >= 2048:
        raise ConnectionError("This connection's change history is full; contact support.")
    return False


def save(map_path, cid: str, body: dict) -> dict:
    app_id = _digits(body.get("app_id"), "App ID")
    token, app_secret = body.get("access_token"), body.get("app_secret", "")
    if (not isinstance(token, str) or not token.strip() or len(token) > 16384
            or not isinstance(app_secret, str) or len(app_secret) > 4096):
        raise ConnectionError("Enter an official credential within the permitted length.", 400)
    with transaction(map_path):
        data, rows = _load(), _rows(map_path)
        _current(data, rows)
        item = _item(data, cid)
        pair = (item["profile_uuid"], item["ad_account_id"])
        if (not item["binding_valid"] or sum(_pair(r) == pair for r in rows) != 1
                or item["phase"] not in _ACTIVE | {"disconnected"}):
            raise ConnectionError("This profile/account binding changed; reopen setup for the current mapping.")
        if _request(item, body.get("expected_revision"), body.get("request_id"), "save", app_id):
            return _dto(item)
        if data["app_owners"].get(app_id, cid) != cid:
            raise ConnectionError("This App ID belongs to another local connection. Use a separate application.")
        revision = item["credential_revision"] + 1
        try:
            reference = vault.put(cid, revision, {"access_token": token.strip(), "app_secret": app_secret.strip()}, directory=_store().parent)
        except vault.SecretUnavailable:
            raise ConnectionError("Windows credential protection unavailable; nothing was saved.", 503) from None
        old_ref, old_revision = item["credential_ref"], item["credential_revision"]
        item.update(app_id=app_id, credential_ref=reference, credential_revision=revision,
                    revision=item["revision"] + 1, phase="saved_unverified", updated_at=_now())
        if old_ref:
            item["cleanup"].append({"ref": old_ref, "revision": old_revision})
        item["requests"][body["request_id"]] = {
            "operation": "save", "expected_revision": body["expected_revision"], "app_id": app_id}
        data["app_owners"][app_id] = cid
        try:
            _write(data)
        except ConnectionError:
            # Only the uncommitted new blob can be removed; old metadata is intact.
            try:
                vault.delete(reference, cid, revision, directory=_store().parent)
            except vault.SecretUnavailable:
                pass
            raise
        if _cleanup(data):
            _write(data)
        return _dto(item)


def disconnect(map_path, cid: str, body: dict) -> dict:
    with transaction(map_path):
        data = _load()
        _current(data, _rows(map_path))
        item = _item(data, cid)
        if _request(item, body.get("expected_revision"), body.get("request_id"), "disconnect"):
            return _dto(item)
        _retire(item, "disconnected")
        item["requests"][body["request_id"]] = {
            "operation": "disconnect", "expected_revision": body["expected_revision"], "app_id": ""}
        _write(data)
        if _cleanup(data):
            _write(data)
        return _dto(item)
