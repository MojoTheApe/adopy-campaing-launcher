"""Which Multilogin workspace each ACL slot is really connected to.

The two slots ("Inhouse" = A, "KLD" = B) differ only by the token saved under
`MULTILOGIN_AUTOMATION_TOKEN_A` / `_B`. Nothing used to check *whose* token it
was: a buyer who typed the KLD login while the Inhouse slot was selected ended
up with both slots pointing at KLD, and the page kept saying "Connected
(Inhouse)" because it only knew the slot's name.

The truth is in the token itself: an automation token is a JWT that carries the
workspace id it was issued for. This module reads it offline (no network, the
token never leaves the machine) and answers three questions:

* what workspace is this slot connected to;
* do the two slots point at the same one (a state that can only be a mistake);
* how to disconnect a slot cleanly.
"""
from __future__ import annotations

import os
from typing import Mapping

SLOTS = ("A", "B")


def _key(kind: str, slot: str) -> str:
    return {
        "token": f"MULTILOGIN_AUTOMATION_TOKEN_{slot}",
        "name": f"MULTILOGIN_WORKSPACE_{slot}",
        "id": f"MULTILOGIN_WORKSPACE_ID_{slot}",
    }[kind]


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def workspace_id(slot: str, env: Mapping[str, str] | None = None) -> str:
    """Workspace id of the slot's token; "" when it cannot be told.

    The token wins over the remembered id: the token is what the RPA actually
    uses, the remembered id is only what the last sign-in wrote down.
    """
    from src.fb_api import mlx_session

    env = _env(env)
    claims = mlx_session._claims((env.get(_key("token", slot)) or "").strip()) or {}
    from_token = str(claims.get("workspaceID") or claims.get("workspace_id") or "").strip().lower()
    return from_token or (env.get(_key("id", slot)) or "").strip().lower()


def workspace_name(slot: str, env: Mapping[str, str] | None = None) -> str:
    return (_env(env).get(_key("name", slot)) or "").strip()


def has_token(slot: str, env: Mapping[str, str] | None = None) -> bool:
    return bool((_env(env).get(_key("token", slot)) or "").strip())


def duplicate(env: Mapping[str, str] | None = None) -> dict | None:
    """Both slots hold a token for the same workspace — always a mistake.

    `{workspace_id, name}` or None. Slots whose workspace cannot be read are
    never reported: a false alarm here would disconnect a healthy account.
    """
    env = _env(env)
    if not all(has_token(s, env) for s in SLOTS):
        return None
    a, b = workspace_id("A", env), workspace_id("B", env)
    if not a or a != b:
        return None
    return {"workspace_id": a, "name": workspace_name("A", env) or workspace_name("B", env)}


def other_slot_with(workspace: str, slot: str, env: Mapping[str, str] | None = None) -> str:
    """The *other* slot already connected to `workspace`, or ""."""
    env = _env(env)
    other = "B" if slot == "A" else "A"
    want = (workspace or "").strip().lower()
    if want and has_token(other, env) and workspace_id(other, env) == want:
        return other
    return ""


def disconnect(slot: str) -> None:
    """Forget the slot's token and workspace on this machine.

    An empty value instead of a deleted line: `update_machine_env` rewrites the
    machine `.env` atomically under its lock and drops duplicate lines for the
    key, and an empty token reads as "not connected" everywhere. Profile links
    in the account map are left alone — they belong to the workspace, and the
    next sign-in to the right one makes them valid again.
    """
    from config import paths

    slot = slot.strip().upper()
    if slot not in SLOTS:
        raise ValueError(f"unknown slot: {slot!r}")
    keys = [_key("token", slot), _key("name", slot), _key("id", slot)]
    paths.update_machine_env({k: "" for k in keys})
    for k in keys:
        os.environ.pop(k, None)


def repair(labels: Mapping[str, str], env: Mapping[str, str] | None = None) -> dict:
    """Undo "both slots are the same workspace", once, without asking.

    Which slot is the wrong one is decided by the workspace's own name when it
    names exactly one slot ("KLD workspace" belongs to the KLD slot). When the
    name settles nothing, both slots are disconnected: signing in again is a
    minute, while keeping the wrong one silently publishes to the wrong account.

    Returns `{repaired, cleared, kept, message}`; `repaired` is False and
    nothing is touched when there is no conflict.
    """
    env = _env(env)
    dup = duplicate(env)
    if not dup:
        return {"repaired": False, "cleared": [], "kept": "", "message": ""}

    name = dup["name"].lower()
    named = [s for s in SLOTS if (labels.get(s) or "").strip().lower() and (labels[s].strip().lower() in name)]
    if len(named) == 1:
        kept = named[0]
        cleared = [s for s in SLOTS if s != kept]
    else:
        kept = ""
        cleared = list(SLOTS)

    for s in cleared:
        disconnect(s)

    shown = ", ".join(labels.get(s) or s for s in cleared)
    ws = f" ({dup['name']})" if dup["name"] else ""
    if kept:
        message = (f"{labels.get(cleared[0]) or cleared[0]} and {labels.get(kept) or kept} were both connected to the "
                   f"same Multilogin workspace{ws}. {shown} was disconnected — sign in to it with its own account.")
    else:
        message = (f"Both Multilogin slots were connected to the same workspace{ws}, so it is unclear which one is "
                   f"right. {shown} were disconnected — sign in to each with its own account.")
    return {"repaired": True, "cleared": cleared, "kept": kept, "message": message}
