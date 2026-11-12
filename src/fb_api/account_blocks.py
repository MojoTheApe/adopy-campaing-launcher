"""ACL-2091 part A: one owner of "why is this profile paused".

A *hold* is a stop that needs a person: a Facebook checkpoint, a login page, a Meta policy block
(368 and the 1404078/2859015/1404163 family) and a frozen Ads Manager tab whose one replacement is
already spent. A hold has no timer, survives a dashboard restart (``account_holds.json`` next to
``warm_sessions.json``) and ends only through :func:`clear`. Timed stops (rate limit, token-only
logged out, the first frozen tab) stay in ``account_spend_api._COOLDOWN``; this module only keeps
their action/label beside them so the refusal can say what happened.

The block belongs to the physical profile (``physical_profile_key``), so every label that shares the
profile shows it. Nothing here talks to Meta, opens a window or touches a tab.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

#: Kinds that become a hold (no timer). `logged_out` only with page evidence.
HOLD_KINDS = ("checkpoint", "logged_out", "policy_block", "page_unresponsive")
#: Codes of a Meta block that mean "stop and let a person look" (368 is POLICY_CODES in graph.py).
POLICY_CODES = (368, 1404078, 2859015, 1404163)
ACCOUNT_BLOCKED_CODE = "account_blocked"
#: ACL-2595: Graph code of "Please authenticate your account" (31/3858385) kept on a checkpoint-kind hold.
AUTH_REQUIRED_CODE = 31
HOLDS_FILE = "account_holds.json"

_LOCK = threading.RLock()
#: Test seam: a path that replaces the file in the local config dir.
_PATH_OVERRIDE: Path | None = None
#: key -> (action, label) of the timed stop kept in `_COOLDOWN` (not persisted).
_TIMED_META: dict[str, tuple[str, str, float]] = {}
#: A remembered action/label older than this is not quoted (the longest timed pause is far shorter).
_TIMED_META_TTL_S = 3600.0

# ───────────────────────── action of the stop ─────────────────────────

_ACTION: contextvars.ContextVar[str] = contextvars.ContextVar("acl_problem_action", default="")
#: True when an entry point named the action itself (not a `only_if_unset` default). The var lives in the
#: thread that runs the operation: `read_session`/the queued dispatcher run it in the caller's thread.
_EXPLICIT: contextvars.ContextVar[bool] = contextvars.ContextVar("acl_problem_action_explicit", default=False)
#: Actions a person starts on purpose; only these may probe a held profile (and lift the hold).
BUYER_ACTIONS = ("retry", "read")

ACTION_TEXT = {
    "schedule": "a scheduled read",
    "attention": "a background read",
    "action": "a refresh after your action",
    "retry": "Retry",
    "read": "Read",
    "check_plan": "Check plan",
    "publish": "Publish",
    "command": "a campaign change",
}
_FALLBACK_ACTION = "a read"


@contextlib.contextmanager
def problem_action(action: str, *, only_if_unset: bool = False) -> Iterator[None]:
    """Name the action running in this context. The innermost caller wins, except with `only_if_unset`
    (a default for code that may run under a caller that already named the real action)."""
    keep = (only_if_unset and _ACTION.get()) or not action
    token = _ACTION.set(_ACTION.get() if keep else str(action))
    etoken = _EXPLICIT.set(_EXPLICIT.get() or (not keep and not only_if_unset))
    try:
        yield
    finally:
        _EXPLICIT.reset(etoken)
        _ACTION.reset(token)


def buyer_started() -> bool:
    """Did a person start the running action (Retry, or an explicit Read)? Schedules, background reads,
    publish and a defaulted `a read` are not."""
    return bool(_EXPLICIT.get()) and _ACTION.get() in BUYER_ACTIONS


def current_action() -> str:
    """Human text of the running action (`a read` when no entry point named it)."""
    raw = _ACTION.get()
    return ACTION_TEXT.get(raw, raw) or _FALLBACK_ACTION


def action_for_trigger(trigger: str) -> str:
    """Map a read trigger (`schedule`, `attention`, `action`, `retry`, `button`...) to an action name."""
    trig = str(trigger or "").strip().lower()
    if trig in ("schedule", "attention", "action", "retry"):
        return trig
    if trig == "open":      # a page open reads in the background
        return "attention"
    return "read"


# ───────────────────────── persisted holds ─────────────────────────

def _path() -> Path:
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / HOLDS_FILE


def _load() -> dict[str, dict[str, Any]]:
    """Read the file on every call (a second dashboard on the same config dir sees it). Fails open."""
    try:
        raw = _path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("account holds unreadable (%s); using the timers only", type(exc).__name__)
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("account holds file is corrupt; using the timers only")
        return {}
    if not isinstance(data, dict):
        log.warning("account holds file has an unexpected shape; using the timers only")
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict) and v.get("kind") in HOLD_KINDS}


def _save(holds: dict[str, dict[str, Any]]) -> bool:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(holds, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("account holds not saved (%s)", type(exc).__name__)
        return False


def _event(name: str, **fields: Any) -> None:
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        _rt.record(name, None, diag={"source": "account_blocks", **fields})
    except Exception:  # noqa: BLE001 — telemetry never breaks a stop
        pass


# ───────────────────────── classification ─────────────────────────

def _code_of(stop: Any) -> int | None:
    code, sub = getattr(stop, "code", None), getattr(stop, "subcode", None)
    for value in (sub, code):
        if value in POLICY_CODES:
            return int(value)
    return int(code) if isinstance(code, int) else None


def is_hold_stop(stop: Any, *, replacement_spent: bool = True) -> bool:
    """Does this stop become a hold? Token-only `LoggedOut` (no page evidence) never does."""
    kind = getattr(stop, "kind", "")
    if kind in ("checkpoint", "policy_block"):
        return True
    if kind == "logged_out":
        return bool(getattr(stop, "page_seen", False))
    if kind == "page_unresponsive":
        return bool(replacement_spent)
    return False


def note_stop(key: str, label: str, stop: Any, action: str = "", *, replacement_spent: bool = True) -> bool:
    """Record a stop. Returns True when a persisted hold was set (False: timed or ignored).

    `action` defaults to the one named by the entry point (`problem_action`). Timed kinds only remember
    their action/label here; the caller keeps the timer.
    """
    kind = getattr(stop, "kind", "")
    action = action or current_action()
    if not key:
        return False
    if not is_hold_stop(stop, replacement_spent=replacement_spent):
        if kind in ("rate_limited", "logged_out", "page_unresponsive"):
            with _LOCK:
                _TIMED_META[key] = (action, str(label or ""), time.monotonic())
            _event("profile_block_set", kind=kind, code=getattr(stop, "code", None), action=action, timed=True,
                   page_seen=bool(getattr(stop, "page_seen", False)))
        return False
    with _LOCK:
        holds = _load()
        holds[key] = {"kind": kind, "code": _code_of(stop), "label": str(label or ""), "action": action,
                      "at": time.time(), "target_id": str(getattr(stop, "target_id", "") or "")}
        if not _save(holds):
            return False        # fail open: the caller keeps its timer
    _event("profile_block_set", kind=kind, code=_code_of(stop), action=action, timed=False,
           page_seen=bool(getattr(stop, "page_seen", False)))
    return True


def is_held(key: str) -> bool:
    """Is this physical profile waiting for a person? Builder prep and background reads must ask first."""
    return bool(key) and key in _load()


def hold_of(key: str) -> dict[str, Any] | None:
    held = _load().get(key) if key else None
    return dict(held) if held else None


def clear(key: str, by: str = "retry", *, kind: str = "") -> bool:
    """Lift the hold of a profile (`kind` limits it to one kind). Returns True when one was removed."""
    if not key:
        return False
    with _LOCK:
        holds = _load()
        held = holds.get(key)
        if not held or (kind and held.get("kind") != kind):
            return False
        del holds[key]
        _save(holds)
        _TIMED_META.pop(key, None)
    _event("profile_block_cleared", kind=held.get("kind"), by=by,
           held_s=max(0, int(time.time() - float(held.get("at") or time.time()))))
    return True


def forget_timed(key: str) -> None:
    """Drop the action/label kept beside a timer (called when the timer ends or is replaced)."""
    with _LOCK:
        _TIMED_META.pop(key, None)


# ───────────────────────── texts ─────────────────────────

def _reason(kind: str, label: str, action: str, code: int | None, same_profile: bool) -> str:
    who = f"{label} (same profile)" if same_profile else (label or "the profile")
    if kind == "checkpoint" and code == AUTH_REQUIRED_CODE:
        return "Meta asks to confirm this account (security check)."
    if kind == "checkpoint":
        return f"Facebook security check on {who}, found during {action}."
    if kind == "logged_out":
        return f"{who} is logged out of Facebook, found during {action}."
    if kind == "policy_block":
        return f"Meta blocked {action} on {who} as suspicious (error {code or 368})."
    if kind == "page_unresponsive":
        return f"The Ads Manager tab of {who} stopped answering during {action}."
    if kind == "token_expired":
        return f"The Ads Manager session of {who} expired during {action}."
    if kind == "rate_limited":
        return f"Meta rate limit on {who}, reached during {action}."
    return f"{who} was stopped during {action}."


def _next_step(kind: str, code: int | None, *, mins: int = 0, replaceable: bool = False) -> str:
    if kind == "checkpoint" and code == AUTH_REQUIRED_CODE:
        return ("Open Ads Manager in this profile and complete the check, then publish again. "
                "Existing ads keep running.")
    if kind == "checkpoint":
        return "Solve the checkpoint in the open profile window, then press Retry."
    if kind == "logged_out":
        return "Log in in the open profile window, then press Retry."
    if kind == "policy_block":
        if code == 1404163:
            try:
                from src.fb_api.error_catalog import explain

                step = explain(1404163).action_en
            except Exception:  # noqa: BLE001
                step = ""
            if step:
                return step
        return "Check the account and its Business Manager. Retrying soon risks a checkpoint or a ban."
    if kind == "page_unresponsive":
        return "The next action opens a fresh tab." if replaceable else "Press Retry to open a fresh tab."
    return f"Waiting about {max(1, mins)} min."


def _suffix(verb: str) -> str:
    return {"publish": " Nothing was created.", "read": " Nothing was called."}.get(verb, "")


def describe(kind: str, label: str, action: str, code: int | None = None, *, same_profile: bool = False,
             left_s: float = 0.0, replaceable: bool = False) -> tuple[str, str]:
    """`(reason line, next step)` for one stop. `kind` may be `token_expired` for a token-only logout."""
    mins = int(left_s // 60) + 1 if left_s and left_s != float("inf") else 0
    step_kind = "rate_limited" if kind == "token_expired" else kind
    return (_reason(kind, label, action, code, same_profile),
            _next_step(step_kind, code, mins=mins, replaceable=replaceable))


def refusal(key: str, label: str, verb: str = "", *, timed: tuple[float, str] | None = None,
            replaceable: bool = False) -> tuple[str, str] | None:
    """`(message, "account_blocked")` when the profile must not be touched, else None.

    A persisted hold wins. `timed=(left_s, kind)` describes the in-memory cooldown of the caller; it is
    explained with the same texts (the old "waiting about 10 min" lines are gone).
    """
    held = _load().get(key) if key else None
    if held:
        reason, step = describe(held["kind"], held.get("label") or label, held.get("action") or _FALLBACK_ACTION,
                                held.get("code"), same_profile=bool(label and held.get("label") not in ("", label)))
        _event("profile_block_refused", kind=held["kind"], verb=verb, action_of_block=held.get("action"))
        return f"{reason} {step}{_suffix(verb)}", ACCOUNT_BLOCKED_CODE
    if timed is not None and timed[0] > 0:
        left, kind = timed
        with _LOCK:
            action, owner, at = _TIMED_META.get(key, (_FALLBACK_ACTION, "", 0.0))
            if at and time.monotonic() - at > _TIMED_META_TTL_S:
                _TIMED_META.pop(key, None)
                action, owner = _FALLBACK_ACTION, ""
        if kind == "logged_out":
            kind = "token_expired"
        reason, step = describe(kind, owner or label, action, None, same_profile=bool(owner and label and owner != label),
                                left_s=left, replaceable=replaceable)
        _event("profile_block_refused", kind=kind, verb=verb, action_of_block=action)
        return f"{reason} {step}{_suffix(verb)}", ACCOUNT_BLOCKED_CODE
    return None


def view(labels: dict[str, str] | list[str]) -> dict[str, dict[str, Any]]:
    """Block of every label that has one. `labels` is `{label: key}` or a list of labels (keys resolved here)."""
    if not isinstance(labels, dict):
        labels = {lab: _key_of(lab) for lab in labels}
    holds = _load()
    out: dict[str, dict[str, Any]] = {}
    for label, key in labels.items():
        held = holds.get(key) if key else None
        if not held:
            continue
        reason, step = describe(held["kind"], held.get("label") or label, held.get("action") or _FALLBACK_ACTION,
                                held.get("code"), same_profile=held.get("label") not in ("", label))
        out[label] = {"kind": held["kind"], "code": held.get("code"), "reason": reason, "next": step,
                      "action": held.get("action") or "", "at": held.get("at"), "left_s": None,
                      "window_open": None, "can_retry": True, "needs_confirm": held["kind"] == "policy_block"}
    return out


def _key_of(label: str) -> str:
    try:
        from src.analysis_agent import account_spend as _as

        return _as._profile_key(label)
    except Exception:  # noqa: BLE001 — unknown account: no block to show
        return ""
