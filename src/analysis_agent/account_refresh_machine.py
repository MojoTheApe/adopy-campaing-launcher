"""Machine-scoped metadata and structure-result relay for ACL-2063.

Private maps, credentials and inventory files stay in their original directories.
This namespace intentionally shares only the allow-listed structure envelope
between local authenticated OS users of ACL. It is not a credential store or an
authorization boundary against another process running as one of those users.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import math
import re
import threading
from datetime import datetime, timezone

from config.paths import _atomic_write, machine_file_lock


class CoordinationError(RuntimeError):
    pass


_ACTIVE = threading.local()


def default_root() -> Path:
    if os.name != "nt":
        return Path("/var/tmp/adopy-acl-account-refresh-v1")
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD),
                    ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    # FOLDERID_ProgramData; never derive the machine domain from a user's env/home.
    folder = GUID(0x62AB5D82, 0xFDC1, 0x4DC3,
                  (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97))
    value = ctypes.c_wchar_p()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                          wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    result = shell.SHGetKnownFolderPath(ctypes.byref(folder), 0x10000, None, ctypes.byref(value))
    if result != 0 or not value.value:
        raise CoordinationError("The shared account-refresh folder is unavailable; no account was read.")
    try:
        return Path(value.value) / "Adopy-ACL-account-refresh-v1"
    finally:
        ole = ctypes.WinDLL("ole32")
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree(ctypes.cast(value, ctypes.c_void_p))


def _windows_object(path: Path, *, directory: bool, data: bool = False) -> None:
    """Create only our new object, verify its exact bounded principals/rights.

    Authenticated Users: Modify on the feature directory/data, read/write on
    stable lock files (no Delete). SYSTEM/Administrators retain FullControl.
    No existing descriptor, parent, deny, policy or account privilege is changed.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    mask = 0x1301BF if directory or data else 0x12019F
    inherit = "OICI" if directory else ""
    sddl = f"D:P(A;{inherit};0x{mask:x};;;AU)(A;{inherit};FA;;;SY)(A;{inherit};FA;;;BA)"
    descriptor = ctypes.c_void_p()
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())

    class SA(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]
    security = SA(ctypes.sizeof(SA), descriptor, False)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if directory:
            kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SA)]
            kernel.CreateDirectoryW.restype = wintypes.BOOL
            if not kernel.CreateDirectoryW(str(path), ctypes.byref(security)) and ctypes.get_last_error() != 183:
                raise ctypes.WinError(ctypes.get_last_error())
        elif not data:
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                ctypes.POINTER(SA), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000, 3,
                                        ctypes.byref(security), 1, 0x80, None)
            if handle == wintypes.HANDLE(-1).value:
                if ctypes.get_last_error() not in (80, 183):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel.CloseHandle(handle)
    finally:
        kernel.LocalFree(descriptor)
    if path.is_symlink() or path.lstat().st_file_attributes & 0x400:
        raise CoordinationError("The account-refresh folder contains an unsupported redirected object.")
    dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    result = adv.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
    if result:
        raise ctypes.WinError(result)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        if (not adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision))
                or bool(control.value & 0x1000) != (not data)):
            raise CoordinationError("The account-refresh object has unsupported permissions.")
        class ACL(ctypes.Structure):
            _fields_ = [("revision", ctypes.c_ubyte), ("unused", ctypes.c_ubyte), ("size", wintypes.WORD),
                        ("count", wintypes.WORD), ("unused2", wintypes.WORD)]
        count = ctypes.cast(dacl, ctypes.POINTER(ACL)).contents.count
        adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        actual = {}
        for index in range(count):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte * 4)).contents
            if header[0] != 0 or header[1] != (3 if directory else (0x10 if data else 0)):
                raise CoordinationError("The account-refresh object has unsupported permissions.")
            rights = ctypes.cast(ace.value + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
            sid = ctypes.c_wchar_p()
            if not adv.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if sid.value in actual:
                    raise CoordinationError("The account-refresh object has duplicate access entries.")
                actual[sid.value] = rights
            finally:
                kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
        if actual != {"S-1-5-11": mask, "S-1-5-18": 0x1F01FF, "S-1-5-32-544": 0x1F01FF}:
            raise CoordinationError("The account-refresh object has unsupported permissions.")
    finally:
        kernel.LocalFree(sd)


def prepare_root(root: Path) -> None:
    try:
        if os.name == "nt":
            _windows_object(root, directory=True)
        else:
            if not root.exists():
                root.mkdir(mode=0o2770, exist_ok=True)
            if root.is_symlink() or not root.is_dir():
                raise CoordinationError("The shared account-refresh folder is not a regular directory.")
    except OSError as exc:
        raise CoordinationError("The shared account-refresh folder cannot be used; no account was read.") from exc


def prepare_lock(path: Path) -> None:
    prepare_root(path.parent)
    if os.name == "nt":
        _windows_object(path, directory=False)
    elif path.is_symlink():
        raise CoordinationError("The account-refresh lock is redirected.")


def binding(account: str) -> str:
    from src.analysis_agent.account_spend import _entry_for
    entry = _entry_for(account)
    if entry is None:
        raise CoordinationError("This account was removed from the dashboard.")
    return binding_values(entry.ad_account_id, entry.profile_id or "")


def binding_values(act: str, profile: str) -> str:
    from src.analysis_agent.account_spend_api import normalize_act
    from src.fb_ads_rpa.profile_lease import physical_profile_key, ProfileLeaseError
    try:
        identity = ["structure-v1", normalize_act(act), physical_profile_key(profile)]
    except (ValueError, ProfileLeaseError) as exc:
        raise CoordinationError("The account map needs a valid ad-account ID and physical profile.") from exc
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def verify_binding(act: str, profile: str) -> None:
    """Before any external open/read, bind the actual session to its producer."""
    expected = getattr(_ACTIVE, "binding", None)
    if expected is None:
        return  # other existing API consumers retain their original contract
    if binding_values(act, profile) != expected:
        raise CoordinationError("The account map changed before its list read; no account was read.")
    from src.fb_ads_rpa.profile_lease import require_profile_lease
    require_profile_lease(profile)


def verify_session(session) -> None:
    verify_binding(session.act, getattr(session, "profile_id", ""))


_NODE_FIELDS = {
    "campaigns": {"id", "name", "status", "effective_status", "objective", "daily_budget", "lifetime_budget",
                  "bid_strategy", "bid_amount", "adlabels", "acl_created", "updated_time"},
    "adsets": {"id", "name", "status", "effective_status", "campaign_id", "daily_budget", "lifetime_budget",
               "bid_strategy", "bid_amount", "adlabels", "acl_created", "updated_time", "objective"},
    "ads": {"id", "name", "status", "effective_status", "campaign_id", "adset_id", "updated_time",
            "daily_budget", "lifetime_budget", "bid_strategy", "bid_amount", "objective", "adlabels", "acl_created"},
}


_SAFE_FAILURES = {
    "proxy_auth": "The profile proxy login is wrong or expired. Update it in Multilogin, then press Retry.",
    "logged_out": "This profile is logged out of Facebook. Sign in inside Multilogin, then press Retry.",
    "missing_profile": "This account's physical profile is missing or unavailable. Check the account map, then press Retry.",
    "transport": "The profile connection failed while reading its account list. Check the connection, then press Retry.",
    "account_map_changed": "The account map changed during its list read. Check the map, then press Retry.",
    "account_removed": "This account was removed from the dashboard. Check the account map.",
    "coordination_unavailable": "The machine account-read state is unavailable. Restore access, then press Retry.",
    "mlx_session_lost": "Multilogin lost its session. Reopen Multilogin, then press Retry.",
    "mlx_token_expired": "The Multilogin token has expired. Update it in Multilogin, then press Retry.",
    "mlx_token_workspace": "The Multilogin token belongs to another workspace. Fix it, then press Retry.",
    "call_budget": "The call budget for account reads is used up. Raise the limit, then press Retry.",
    "checkpoint": "This profile is on a Facebook checkpoint. Solve it inside Multilogin, then press Retry.",
    "rate_limited": "Meta asked to slow down. Wait for the account-read cooldown before retrying.",
    "policy_block": "Meta refused the account-list requests. Wait for the cooldown before retrying.",
    "access": "This profile cannot access its ad account. Check the account map and profile permissions, then press Retry.",
}
_GENERIC_ERROR = "The account list could not be refreshed. Press Retry."
_INTERRUPTED_ERROR = "The account read was interrupted. Press Retry."


def _safe_failure(snap) -> tuple[str, str]:
    from src.analysis_agent import account_spend
    code = snap.error_code or snap.stop
    if code == account_spend.LOGGED_OUT_CODE:
        code = "logged_out"
    if code not in _SAFE_FAILURES:
        # Inspect locally only to choose a fixed coarse category. No original
        # message, URL, proxy credential, account label or token leaves here.
        text = (snap.error + " " + " ".join(snap.problems)).lower()
        code = next((kind for words, kind in (
            (("logged out", "not logged"), "logged_out"),
            (("profile not found", "no profile", "missing profile", "no_profile"), "missing_profile"),
            (("connection", "network", "transport", "disconnected", "proxy"), "transport"),
            (("map changed",), "account_map_changed"), (("no access", "permission denied"), "access"),
        ) if any(word in text for word in words)), "")
    return code, _SAFE_FAILURES.get(code, _GENERIC_ERROR)


def envelope(snap) -> dict:
    from src.analysis_agent.campaign_inventory import read_succeeded
    out = {"state": snap.state, "finished_at": snap.finished_at, "finished_at_utc": snap.finished_at_utc,
           "currency": snap.currency, "steps": {k: v for k, v in snap.steps.items() if k in _NODE_FIELDS},
           "stop": snap.stop, "ok": read_succeeded(snap)}
    for field, allowed in _NODE_FIELDS.items():
        out[field] = [{k: v for k, v in row.items() if k in allowed} for row in getattr(snap, field)]
    # Never relay raw errors/problems, dates, Insights, credentials or arbitrary fields.
    if not out["ok"]:
        out["error_code"], out["error"] = _safe_failure(snap)
    validate_envelope(out)
    return out


def validate_envelope(payload: dict) -> None:
    allowed = {"state", "finished_at", "finished_at_utc", "currency", "steps", "stop", "ok",
               "campaigns", "adsets", "ads", "error", "error_code"}
    invalid = CoordinationError("The shared account structure is incomplete or unsupported; it was not imported.")
    if (not isinstance(payload, dict) or set(payload) - allowed
            or not (allowed - {"error", "error_code"}) <= set(payload)
            or payload["state"] not in {"ok", "partial", "empty", "failed"}
            or type(payload["ok"]) is not bool):
        raise invalid
    for field in ("finished_at", "finished_at_utc", "currency", "stop"):
        if not isinstance(payload[field], str) or len(payload[field]) > 128:
            raise invalid
    if payload["finished_at_utc"]:
        try:
            if datetime.fromisoformat(payload["finished_at_utc"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except ValueError as exc:
            raise invalid from exc
    code = payload.get("error_code", "")
    if not isinstance(code, str) or code not in {"", *_SAFE_FAILURES}:
        raise invalid
    if payload.get("error", "") not in {"", _GENERIC_ERROR, _INTERRUPTED_ERROR, _SAFE_FAILURES.get(code)}:
        raise invalid
    steps = payload["steps"]
    if (not isinstance(steps, dict) or set(steps) - set(_NODE_FIELDS)
            or any(v not in {"ok", "truncated", "skipped", "failed"} for v in steps.values())):
        raise invalid
    if payload["ok"] and (payload["state"] == "failed" or payload["stop"] or payload.get("error")
                          or set(steps) != set(_NODE_FIELDS) or any(v not in {"ok", "truncated"} for v in steps.values())):
        raise invalid
    for level, fields in _NODE_FIELDS.items():
        rows = payload[level]
        if not isinstance(rows, list):
            raise invalid
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) - fields or not isinstance(row.get("id"), str) or not row["id"]:
                raise invalid
            if row["id"] in ids:
                raise invalid
            ids.add(row["id"])
            for field, value in row.items():
                if field in {"daily_budget", "lifetime_budget", "bid_amount"}:
                    if value is not None and (type(value) not in {int, float} or not math.isfinite(value)):
                        raise invalid
                elif field == "acl_created":
                    if value is not None and type(value) is not bool:
                        raise invalid
                elif field == "adlabels":
                    if not isinstance(value, list) or any(not isinstance(v, str) or len(v) > 8192 for v in value):
                        raise invalid
                elif not isinstance(value, str) or len(value) > 8192:
                    raise invalid


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _result_record(previous: dict, key: str, payload: dict, revision: int) -> dict:
    value = {**previous, "binding": key, "schema": 1, "revision": revision,
             "payload": payload, "sha256": _payload_hash(payload)}
    if payload["state"] != "failed":
        value.update(snapshot=payload, snapshot_revision=revision, snapshot_sha256=value["sha256"])
    return value


def _validate_result(key: str, value: dict) -> None:
    try:
        if (not isinstance(value, dict) or value.get("binding") != key or value.get("schema") != 1
                or type(value.get("revision")) is not int or value["revision"] <= 0):
            raise ValueError()
        validate_envelope(value["payload"])
        if _payload_hash(value["payload"]) != value.get("sha256"):
            raise ValueError()
        if "snapshot" in value:
            validate_envelope(value["snapshot"])
            if (value["snapshot"]["state"] == "failed" or type(value.get("snapshot_revision")) is not int
                    or not 0 < value["snapshot_revision"] <= value["revision"]
                    or _payload_hash(value["snapshot"]) != value.get("snapshot_sha256")):
                raise ValueError()
    except (KeyError, ValueError, TypeError) as exc:
        raise CoordinationError("The shared account result is corrupt; automatic reads are stopped.") from exc


def _validate_state(state: dict) -> None:
    invalid = CoordinationError("The shared account-refresh state is incomplete or unsupported; automatic reads are stopped.")
    key = lambda v: isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) is not None
    if set(state) != {"version", "revision", "windows", "requests", "results", "halt", "running", "stops"}:
        raise invalid
    revision = state["revision"]
    number = lambda v: type(v) is int and 0 <= v <= revision
    if any(len(state[k]) > 10000 for k in ("windows", "requests", "results", "stops")):
        raise invalid
    for window, members in state["windows"].items():
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}/(?:08|13|18):00", window) or not isinstance(members, dict):
            raise invalid
        for identity, member in members.items():
            if (not key(identity) or not isinstance(member, dict) or set(member) != {"owners", "done"}
                    or not isinstance(member["owners"], list) or not all(key(o) for o in member["owners"])
                    or (member["done"] is not None and not number(member["done"]))):
                raise invalid
    for identity, item in state["requests"].items():
        if not key(identity) or not isinstance(item, dict) or not number(item.get("after")) or not isinstance(item.get("owners"), dict):
            raise invalid
        for owner, request in item["owners"].items():
            if (not key(owner) or set(request) != {"priority", "trigger", "force", "after", "eligible"}
                    or type(request["priority"]) is not int or request["priority"] not in range(4)
                    or not isinstance(request["trigger"], str) or len(request["trigger"]) > 100
                    or type(request["force"]) is not bool or type(request["eligible"]) is not bool
                    or not number(request["after"])):
                raise invalid
    for identity, halt in state["stops"].items():
        if (not key(identity) or not isinstance(halt, dict) or set(halt) != {"kind", "reason", "until", "key", "generation"}
                or halt["key"] != identity or not number(halt["generation"])
                or not isinstance(halt["kind"], str) or not re.fullmatch(r"[a-z_]{1,64}", halt["kind"])
                or not isinstance(halt["reason"], str) or len(halt["reason"]) > 1024
                or (halt["until"] is not None and (type(halt["until"]) not in (int, float) or not math.isfinite(halt["until"])) )):
            raise invalid
    running = state.get("running")
    if running is not None and (not isinstance(running, dict) or set(running) != {"key", "owner", "trigger", "token"}
            or not key(running["key"]) or not isinstance(running["owner"], str) or len(running["owner"]) > 64
            or not isinstance(running["trigger"], str) or len(running["trigger"]) > 100 or not number(running["token"])):
        raise invalid


class MachineStore:
    def __init__(self, root: Path | None = None):
        if root is None:
            from src.analysis_agent.account_refresh_state import machine_root
            root = machine_root()
        self.root = Path(root)
        self.path = self.root / "state.json"
        self.marker = self.root / "initialized.json"

    def lease(self, name: str):
        from src.analysis_agent.account_refresh_state import lease
        path = self.root / name
        prepare_lock(path)
        return lease(path)

    def owner_alive(self, owner: str) -> bool:
        path = self.root / f"worker-{owner}.lock"
        prepare_lock(path)
        try:
            with machine_file_lock(path, timeout=0):
                return False
        except TimeoutError:
            return True

    @contextmanager
    def transaction(self):
        prepare_lock(self.root / "state.lock")
        with machine_file_lock(self.root / "state.lock", timeout=10):
            existed = self.path.exists()
            if self.marker.exists():
                try:
                    if self.marker.is_symlink() or self.marker.stat().st_size > 512:
                        raise ValueError()
                    if json.loads(self.marker.read_text()) != {"schema": 1, "kind": "account_refresh_machine"}:
                        raise ValueError()
                    if not existed:
                        raise ValueError()
                except (OSError, ValueError) as exc:
                    raise CoordinationError("The shared account-refresh state is missing or unsupported; automatic reads are stopped.") from exc
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 32 * 1024 * 1024:
                    raise CoordinationError("The shared account-refresh state is unsupported.")
                if os.name == "nt":
                    _windows_object(self.path, directory=False, data=True)
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = {"version": 1, "revision": 0, "windows": {}, "requests": {}, "results": {}, "halt": None,
                         "running": None, "stops": {}}
            except (OSError, ValueError) as exc:
                raise CoordinationError("The shared account-refresh state could not be read; automatic reads are stopped.") from exc
            if (not isinstance(state, dict) or state.get("version") != 1
                    or any(not isinstance(state.get(k), dict) for k in ("windows", "requests", "results", "stops"))
                    or type(state.get("revision")) is not int or state["revision"] < 0):
                raise CoordinationError("This account-refresh state is not supported by this ACL version.")
            for key, value in state["results"].items():
                if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
                    raise CoordinationError("The shared account binding is unsupported.")
                _validate_result(key, value)
                if value["revision"] > state["revision"]:
                    raise CoordinationError("The shared account revision is unsupported.")
            _validate_state(state)
            before = json.dumps(state, sort_keys=True)
            yield state
            _validate_state(state)
            data = json.dumps(state, ensure_ascii=False, sort_keys=True)
            if data != before or not existed:
                if len(data.encode()) > 32 * 1024 * 1024:
                    raise CoordinationError("The shared account-refresh state reached its safe size limit.")
                _atomic_write(self.path, (data + "\n").encode())
                if os.name == "nt":
                    _windows_object(self.path, directory=False, data=True)
            if not self.marker.exists():
                _atomic_write(self.marker, b'{"schema":1,"kind":"account_refresh_machine"}\n')
            if os.name == "nt":
                _windows_object(self.marker, directory=False, data=True)

    def migrate(self, owner: str, window: str | None, keys: list[str], *, seen: bool, halt: dict | None,
                bindings: dict[str, str], now: float) -> None:
        """Conserve legacy admission/stop without inventing a successful list.

        A previous version did not record physical bindings. Its already admitted
        current window stays consumed, with an explicit unknown-result receipt;
        only an explicit Retry can replace that uncertainty. No old private cache
        or historical window is promoted to shared successful evidence.
        """
        with self.transaction() as state:
            if seen and window:
                members = state["windows"].setdefault(window, {})
                state["revision"] += 1
                for key in keys:
                    if key not in members:
                        stamp = datetime.fromtimestamp(now, timezone.utc)
                        payload = {"state": "failed", "finished_at": stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                                   "finished_at_utc": stamp.isoformat(), "currency": "", "steps": {}, "stop": "", "ok": False,
                                   "campaigns": [], "adsets": [], "ads": [],
                                   "error": "The account read was interrupted. Press Retry."}
                        if key not in state["results"]:
                            state["results"][key] = _result_record({}, key, payload, state["revision"])
                        members[key] = {"owners": [owner], "done": state["revision"]}
            if halt and not halt.get("machine_generation"):
                key = bindings.get(halt.get("account", ""))
                if key and key not in state["stops"] and (halt.get("until") is None or halt["until"] > now):
                    state["revision"] += 1
                    state["stops"][key] = {"key": key, "kind": halt.get("kind", "global"),
                        "reason": "An earlier account-read stop needs recovery. Check this account's map and press Retry.",
                        "until": halt.get("until"), "generation": state["revision"]}
            state["halt"] = self._selected_stop(state)

    def admit(self, window: str, keys: list[str], owner: str) -> bool:
        with self.transaction() as state:
            started = window not in state["windows"]
            # Window metadata is bounded; only the current logical window can
            # launch reads. Old completed records need no unbounded retention.
            state["windows"] = {w: m for w, m in state["windows"].items() if w[:10] >= window[:10]}
            members = state["windows"].setdefault(window, {})
            for key in keys:
                member = members.setdefault(key, {"owners": [], "done": None})
                if owner not in member["owners"]:
                    member["owners"].append(owner)
            return started

    def satisfied(self, key: str, window: str) -> bool:
        with self.transaction() as state:
            return bool(state["windows"].get(window, {}).get(key, {}).get("done"))

    @contextmanager
    def executing(self, token: int, key: str | None = None):
        previous = getattr(_ACTIVE, "execution", None)
        previous_binding = getattr(_ACTIVE, "binding", None)
        _ACTIVE.execution = (str(self.root.resolve()), token)
        _ACTIVE.binding = key
        try:
            yield
        finally:
            _ACTIVE.execution = previous
            _ACTIVE.binding = previous_binding

    def recover_running(self, *, now: float | None = None) -> str | None:
        """The caller holds the actual read lease; never recover its own live scope."""
        with self.transaction() as state:
            old = state.get("running")
            if old and getattr(_ACTIVE, "execution", None) != (str(self.root.resolve()), old["token"]):
                # The OS read lease is now ours, so this persisted execution has
                # no surviving owner. Do not turn it into another automatic read.
                state["revision"] += 1
                stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
                unknown = {"state": "failed", "finished_at": stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                           "finished_at_utc": stamp.isoformat(),
                           "currency": "", "steps": {}, "stop": "", "ok": False,
                           "campaigns": [], "adsets": [], "ads": [],
                           "error": "The account read was interrupted. Press Retry."}
                previous = state["results"].get(old["key"], {})
                state["results"][old["key"]] = _result_record(previous, old["key"], unknown, state["revision"])
                for members in state["windows"].values():
                    if old["key"] in members:
                        members[old["key"]]["done"] = state["revision"]
                state["running"] = None
                return old["key"]
            return None

    def begin(self, key: str, owner: str, trigger: str) -> int:
        """Called only while holding the actual OS read fence, before any open."""
        with self.transaction() as state:
            if state.get("running"):
                raise CoordinationError("Another account read still owns its machine reservation.")
            state["revision"] += 1
            token = state["revision"]
            state["running"] = {"key": key, "owner": owner, "trigger": trigger, "token": token}
            return token

    def request(self, key: str, owner: str, priority: int, trigger: str, force: bool) -> None:
        with self.transaction() as state:
            item = state["requests"].setdefault(key, {"owners": {}, "after": state["revision"]})
            old = item["owners"].get(owner)
            if old is None or priority < old["priority"]:
                item["owners"][owner] = {"priority": priority, "trigger": trigger, "force": force,
                                        "after": state["revision"], "eligible": True}
            elif force:
                old["force"] = True

    def pending(self, key: str, owner: str) -> bool:
        with self.transaction() as state:
            return owner in state["requests"].get(key, {}).get("owners", {})

    def may_dispatch(self, key: str) -> bool:
        # Never wait on an OS ownership lease while holding the state transaction.
        with self.transaction() as state:
            pending = json.loads(json.dumps(state["requests"]))
        own = pending.get(key)
        if not own:
            return True
        active = []
        for candidate, item in pending.items():
            priorities = [v["priority"] for o, v in item["owners"].items()
                          if v.get("eligible", True) and self.owner_alive(o)]
            if priorities:
                active.append((min(priorities), candidate))
        own_priority = min((p for p, k in active if k == key), default=None)
        return own_priority is None or own_priority == min(p for p, _ in active)

    def availability(self, key: str, owner: str, eligible: bool) -> None:
        with self.transaction() as state:
            value = state["requests"].get(key, {}).get("owners", {}).get(owner)
            if value is not None:
                value["eligible"] = eligible

    def withdraw(self, key: str, owner: str) -> None:
        with self.transaction() as state:
            item = state["requests"].get(key)
            if item:
                item["owners"].pop(owner, None)
                if not item["owners"]:
                    state["requests"].pop(key)

    def finish(self, key: str, revision: int | None) -> None:
        if revision is None:
            return
        with self.transaction() as state:
            item = state["requests"].get(key)
            if item:
                item["owners"] = {o: v for o, v in item["owners"].items() if v.get("after", item["after"]) >= revision}
                if not item["owners"]:
                    state["requests"].pop(key)

    def halt(self, *, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self.transaction() as state:
            state["stops"] = {k: v for k, v in state["stops"].items()
                              if v["until"] is None or now < v["until"]}
            halt = self._selected_stop(state)
            state["halt"] = halt
            return dict(halt) if halt else None

    @staticmethod
    def _selected_stop(state: dict) -> dict | None:
        return min(state["stops"].values(), default=None,
                   key=lambda h: (0 if h["kind"] == "checkpoint" else (1 if h["until"] is None else 2),
                                  h["generation"]))

    def stop_generation(self, key: str) -> int | None:
        with self.transaction() as state:
            stop = state["stops"].get(key)
            return stop["generation"] if stop else None

    def result(self, key: str) -> dict | None:
        with self.transaction() as state:
            value = state["results"].get(key)
            return json.loads(json.dumps(value)) if value else None

    def results(self, keys: list[str]) -> dict:
        with self.transaction() as state:
            return {key: json.loads(json.dumps(state["results"][key])) for key in keys if key in state["results"]}

    def publish(self, key: str, snap, *, now: float | None = None, force: bool = False,
                recovery: int | None = None, own_recovery: int | None = None, token: int | None = None) -> int:
        from src.analysis_agent.account_refresh import _global_stop, _halt_kind, classify
        now = time.time() if now is None else now
        payload = envelope(snap)
        with self.transaction() as state:
            if token is not None:
                running = state.get("running")
                if not running or running["token"] != token or running["key"] != key:
                    raise CoordinationError("This account-read result no longer owns its machine reservation.")
                state["running"] = None
            state["revision"] += 1
            revision = state["revision"]
            previous = state["results"].get(key, {})
            state["results"][key] = _result_record(previous, key, payload, revision)
            if classify(snap) == "global":
                reason, wait = _global_stop(snap)
                old = state["stops"].get(key)
                if not old or old["kind"] != "checkpoint" or _halt_kind(snap) == "checkpoint":
                    state["stops"][key] = {"kind": _halt_kind(snap), "reason": reason,
                                          "until": now + wait if wait else None,
                                          "key": key, "generation": revision}
            elif payload["ok"] and force:
                halt = self._selected_stop(state)
                if (halt and halt["generation"] == recovery
                        and (halt["kind"] != "checkpoint" or halt["key"] == key)):
                    state["stops"].pop(halt["key"], None)
                own = state["stops"].get(key)
                if own and own["generation"] == own_recovery:
                    state["stops"].pop(key, None)
            request = state["requests"].get(key)
            if request:
                request["owners"] = {owner: value for owner, value in request["owners"].items()
                                     if value["after"] >= revision}
                if not request["owners"]:
                    state["requests"].pop(key)
            state["halt"] = self._selected_stop(state)
            for members in state["windows"].values():
                if key in members:
                    members[key]["done"] = revision
            return revision
