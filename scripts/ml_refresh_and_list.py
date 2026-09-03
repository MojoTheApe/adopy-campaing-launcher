#!/usr/bin/env python3
"""Sign in to Multilogin X, print all folders + profiles, and optionally update .env token.

Usage:
    python scripts/ml_refresh_and_list.py
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.paths import get_machine_env_path, load_env, update_machine_env

load_env()

MLX_API = "https://api.multilogin.com"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def signin(email: str, password: str) -> str:
    """Sign in and return a short-lived bearer token.

    MD5, not SHA-256: that is what the Multilogin X API asks for. The dashboard
    was corrected on 2026-07-13 (commit 3126015) and this script was not, so it
    has been failing sign-in ever since — which is exactly the kind of thing you
    find out on the day you need it as a second opinion.
    """
    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
    r = requests.post(
        f"{MLX_API}/user/signin",
        headers=HEADERS,
        json={"email": email, "password": password_hash},
        timeout=30,
    )
    if r.status_code != 200:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        msg = (body.get("status") or {}).get("message") or r.text[:200]
        sys.exit(f"Sign-in failed ({r.status_code}): {msg}")
    data = r.json().get("data") or {}
    token = data.get("token") or ""
    if not token:
        sys.exit(f"Sign-in: no token in response: {r.text[:300]}")
    return token


def get_automation_token(bearer: str, period: str = "1mo") -> str:
    """Mint an automation token, and SAY what happened when it fails.

    This used to return "" for every non-200, so "Multilogin refuses this
    account" and "we asked for a period it no longer accepts" looked identical
    from the outside. That silence is why 2026-09-03 was spent guessing at
    subscriptions and permissions instead of reading one line of JSON.

    The periods are tried in order because an unaccepted `expiration_period` is
    a 400 on an endpoint that is otherwise working perfectly.
    """
    h = {**HEADERS, "Authorization": f"Bearer {bearer}"}
    tried = [period] + [p for p in ("1m", "30d", "1w", "1d") if p != period]
    for attempt in tried:
        url = f"{MLX_API}/workspace/automation_token?expiration_period={attempt}"
        r = requests.get(url, headers=h, timeout=30)
        if r.status_code == 200:
            data = r.json().get("data") or {}
            token = data.get("token") or data.get("automation_token") or ""
            if token:
                print(f"  [ok] expiration_period={attempt}")
                return token
            print(f"  [warn] expiration_period={attempt}: HTTP 200 but no token in {r.text[:200]}")
            continue
        print(f"  [warn] expiration_period={attempt}: HTTP {r.status_code} {r.text[:200]}")
    return ""


def describe_bearer(bearer: str) -> None:
    """Print what the sign-in token says about itself.

    A Multilogin account can belong to more than one workspace, and sign-in puts
    you in exactly one of them. If that one is not the workspace holding the
    profiles, everything downstream is technically correct and useless: folders
    list fine, profiles come back empty, and minting an automation token is
    refused because you are not the owner *of this workspace*.
    """
    import base64

    parts = bearer.split(".")
    if len(parts) != 3:
        print("  [warn] the sign-in token is not a JWT — cannot read it")
        return
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
    except Exception as exc:
        print(f"  [warn] could not decode the token: {exc}")
        return
    interesting = ("workspace_id", "workspaceID", "workspace", "role", "roles",
                   "user_id", "userID", "email", "plan", "tier", "exp")
    for key in interesting:
        if key in claims:
            print(f"  {key}: {claims[key]}")
    unknown = [k for k in claims if k not in interesting]
    if unknown:
        print(f"  (other fields: {', '.join(sorted(unknown))})")


def list_workspaces(bearer: str) -> list[dict]:
    """All workspaces this account can see, so a second one cannot hide."""
    h = {**HEADERS, "Authorization": f"Bearer {bearer}"}
    r = requests.get(f"{MLX_API}/user/workspaces", headers=h, timeout=30)
    if r.status_code != 200:
        print(f"  [warn] workspaces endpoint: {r.status_code} {r.text[:300]}")
        return []
    body = r.json()
    data = body.get("data") or {}
    items = data.get("workspaces") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        print(f"  no workspaces in the answer: {r.text[:300]}")
        return []
    out: list[dict] = []
    print(f"  {len(items)} workspace(s):")
    for i, w in enumerate(items, 1):
        if not isinstance(w, dict):
            print(f"    {i}) {w}")
            continue
        name = w.get("name") or w.get("workspace_name") or "—"
        wid = w.get("workspace_id") or w.get("workspaceID") or w.get("id") or "—"
        role = w.get("role") or w.get("role_name") or w.get("permissions") or "—"
        print(f"    {i}) {name:<30} role={role:<18} id={wid}")
        out.append({"name": name, "id": wid, "role": role})
    return out


def switch_workspace(bearer: str, email: str, workspace_id: str) -> str:
    """Re-issue the sign-in token against a specific workspace.

    Sign-in puts you in exactly one workspace — the account's own. An account
    that works in someone else's workspace therefore authenticates perfectly and
    then sees an empty world: no profiles, and no right to mint an automation
    token, because the token would be for the wrong workspace. Everything after
    that looks like a permissions or subscription problem and is neither.
    """
    h = {**HEADERS, "Authorization": f"Bearer {bearer}"}
    bodies = [
        {"email": email, "workspace_id": workspace_id},
        {"workspace_id": workspace_id},
        {"workspaceID": workspace_id},
        {"email": email, "workspaceID": workspace_id},
    ]
    for body in bodies:
        r = requests.post(f"{MLX_API}/user/refresh_token", headers=h, json=body, timeout=30)
        if r.status_code == 200:
            data = r.json().get("data") or {}
            token = data.get("token") or ""
            if token:
                print(f"  [ok] switched with {sorted(body)}")
                return token
            print(f"  [warn] {sorted(body)}: HTTP 200 but no token — {r.text[:200]}")
            continue
        print(f"  [warn] {sorted(body)}: HTTP {r.status_code} {r.text[:200]}")
    return ""


def list_folders(bearer: str) -> list[dict]:
    h = {**HEADERS, "Authorization": f"Bearer {bearer}"}
    r = requests.get(f"{MLX_API}/workspace/folders", headers=h, timeout=30)
    if r.status_code != 200:
        print(f"  [warn] folders endpoint: {r.status_code} {r.text[:200]}")
        return []
    body = r.json()
    data = (body.get("data") or {})
    if isinstance(data, dict):
        folders = data.get("folders") or []
    else:
        folders = data if isinstance(data, list) else []
    return folders


def search_profiles(bearer: str, search: str = "") -> list[dict]:
    h = {**HEADERS, "Authorization": f"Bearer {bearer}"}
    r = requests.post(
        f"{MLX_API}/profile/search",
        headers=h,
        json={"limit": 100, "offset": 0, "search_text": search},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  [warn] search endpoint: {r.status_code} {r.text[:200]}")
        return []
    body = r.json()
    data = body.get("data") or {}
    if isinstance(data, dict):
        return data.get("profiles") or []
    return data if isinstance(data, list) else []


def update_env_token(token: str) -> None:
    """Write the token where THIS machine actually reads it from.

    There are two possible locations and they differ per machine: a migrated
    install keeps settings in %LOCALAPPDATA%\\ACL\\.env, an older one still has
    them inside the checkout. Writing to the wrong one produces a token that is
    saved, correct, and completely ignored.
    """
    import re

    key = "MULTILOGIN_AUTOMATION_TOKEN_A"
    pattern = re.compile(rf"^\s*(?:export\s+)?{key}\s*=")
    for path in (Path(get_machine_env_path()), _ROOT / ".env"):
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f'{key}={json.dumps(token, ensure_ascii=False)}'
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"  [ok] token updated in {path}")
                return
    # Nowhere to update — put it in the machine-local file, which is where a
    # current install expects it.
    try:
        print(f"  [ok] token saved to {update_machine_env({key: token})}")
    except Exception as exc:
        print(f"  [fail] could not save the token: {exc}")


def main() -> None:
    print("=== Multilogin X — sign in & list profiles ===\n")

    email = input("Multilogin email: ").strip()
    if not email:
        sys.exit("Email is required.")

    password = getpass.getpass("Multilogin password: ").strip()
    if not password:
        sys.exit("Password is required.")

    print("\nSigning in...")
    bearer = signin(email, password)
    print("  [ok] signed in\n")

    print("Who this sign-in belongs to...")
    describe_bearer(bearer)

    print("\nWorkspaces visible to this account...")
    spaces = list_workspaces(bearer)

    if len(spaces) > 1:
        pick = input(
            "\n  Work in which workspace? [number, Enter = keep the current one]: "
        ).strip()
        if pick.isdigit() and 1 <= int(pick) <= len(spaces):
            chosen = spaces[int(pick) - 1]
            print(f"  Switching to {chosen['name']} ({chosen['id']})...")
            switched = switch_workspace(bearer, email, chosen["id"])
            if switched:
                bearer = switched
                print("  Now signed in against that workspace:")
                describe_bearer(bearer)
            else:
                print("  [warn] could not switch — continuing in the current workspace")

    print("\nGetting automation token (1 month)...")
    auto_token = get_automation_token(bearer, "1mo")
    if auto_token:
        print(f"  [ok] automation token obtained")
        save = input("  Save to fb_agents/.env? [Y/n]: ").strip().lower()
        if save in ("", "y", "yes"):
            update_env_token(auto_token)
    else:
        print("  [warn] could not get automation token, using short-lived token")
        auto_token = bearer

    print("\nFetching folders...")
    folders = list_folders(auto_token)
    folder_map: dict[str, str] = {}
    if folders:
        print(f"  Found {len(folders)} folder(s):\n")
        print(f"  {'Folder name':<35} folder_id")
        print("  " + "-" * 75)
        for f in folders:
            name = f.get("name") or f.get("folder_name") or "—"
            fid = f.get("folderID") or f.get("folder_id") or f.get("id") or "—"
            print(f"  {name:<35} {fid}")
            folder_map[fid] = name
    else:
        print("  No folders returned (or endpoint not available)")

    print("\nFetching profiles (search all)...")
    profiles = search_profiles(auto_token, "")
    if profiles:
        print(f"  Found {len(profiles)} profile(s):\n")
        print(f"  {'Profile name':<35} {'folder_id':<38} profile_id")
        print("  " + "-" * 115)
        for p in profiles:
            name = p.get("name") or p.get("profile_name") or "—"
            pid  = p.get("profile_id") or p.get("uuid") or p.get("id") or "—"
            fid  = p.get("folder_id") or p.get("folderID") or "—"
            print(f"  {name:<35} {fid:<38} {pid}")
    else:
        print("  No profiles returned — try searching by name below")
        term = input("\n  Search term (e.g. 'UL869'): ").strip()
        if term:
            profiles = search_profiles(auto_token, term)
            for p in profiles:
                name = p.get("name") or "—"
                pid  = p.get("profile_id") or p.get("uuid") or "—"
                fid  = p.get("folder_id") or "—"
                print(f"  {name:<35} {fid:<38} {pid}")

    print("\nDone. Copy folder_id + profile_id into the dashboard → Campaign Builder → Save config.")


if __name__ == "__main__":
    main()
