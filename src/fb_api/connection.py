"""Machine connection policy shared by reads, Check and publication.

Only profile preparation occurs here. No Graph writes or replay of actions.
"""
from __future__ import annotations
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from config.paths import get_local_config_dir, machine_file_lock, _atomic_write

_CONFIRMED = ContextVar("acl_confirmed_profile_reopen", default=False)
DEFAULTS = {"managed_sessions": True, "auto_reopen_manual": False}


def settings():
    path = Path(get_local_config_dir()) / "connection_settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or any(k in data and type(data[k]) is not bool for k in DEFAULTS):
        raise ValueError("Connection settings are unreadable; profiles were not changed")
    return {k: data.get(k,v) for k,v in DEFAULTS.items()}


def save_settings(changes):
    if not isinstance(changes,dict) or any(k not in DEFAULTS or type(v) is not bool for k,v in changes.items()):
        raise ValueError("Connection settings require boolean values")
    path = Path(get_local_config_dir()) / "connection_settings.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    with machine_file_lock(path.with_suffix(".lock")):
        data = settings()
        data.update(changes)
        _atomic_write(path,json.dumps(data,sort_keys=True).encode())
    return data


def may_reopen_manual():
    raw = os.environ.get("ACL_AUTO_REOPEN_MANUAL", "").strip().lower()
    return _CONFIRMED.get() or (raw in {"1","true","yes"} if raw else settings()["auto_reopen_manual"])


@contextmanager
def confirmed_reopen(confirmed):
    token = _CONFIRMED.set(confirmed is True)
    try:
        yield
    finally:
        _CONFIRMED.reset(token)


def prepare_existing(registry, entry, account):
    """Adopt a running verified profile, or open a closed one (ACL-2262) through the warm path."""
    managed = settings()["managed_sessions"]
    if registry.is_warm(entry.profile_id):
        sess = registry.session(entry.profile_id)
        if not managed or sess is None or sess.owner:
            return True
        # ACL-2262: a registered profile may have been closed or restarted since (overnight, server
        # restart). One local process look, no network: only a changed profile goes on to recovery below.
        kind, port = registry._scan([entry.profile_id]).get(entry.profile_id,("unknown",None))
        if kind not in ("none",) and not (kind == "port" and int(port or 0) != sess.port):
            return True
    if not managed:
        return False
    pid = (getattr(entry,"profile_id","") or "").strip()
    if not pid:
        return False
    from src.fb_ads_rpa.operations import profile_lock
    from src.fb_api import warm, stop_log
    if stop_log.default().restart_wait_s(pid) > 0:
        return False
    with profile_lock(pid):
        kind, port = registry._scan([pid]).get(pid,("unknown",None))
        sess = registry.session(pid)
        if sess is not None and kind == "port" and int(port or 0) == sess.port:
            return True
        if sess is not None and kind == "port":
            if sess.owner:
                raise warm.WarmError("This profile is in use; it was not restarted.","busy")
            registry._forget(pid,stopped=False)
        if kind == "none" and sess is not None:
            # ACL-2262: the registry still lists a profile whose process is gone (closed by hand, crashed,
            # killed with the machine). Nothing may depend on the person pressing a button: forget the
            # dead record and open the profile again through the same warm path below.
            if sess.owner:
                raise warm.WarmError("This profile is in use; it was not restarted.","busy")
            registry._forget(pid,stopped=True)
        # ACL-2262: an account the owner works with gets its profile opened here, closed or not.
        if kind in ("port","none") or (kind == "no_port" and may_reopen_manual()):
            if not registry.has_closer():
                from src.fb_ads_rpa.browser import stop_profile_on_request
                registry.set_closer(stop_profile_on_request)
            result = registry.open_one(account,entry)
            if not result.get("ok"):
                raise warm.WarmError(result.get("message") or "Connection preparation failed",result.get("code") or "connection")
            return True
        return registry.is_warm(pid)
