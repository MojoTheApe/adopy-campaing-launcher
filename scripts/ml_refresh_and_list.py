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

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass

MLX_API = "https://api.multilogin.com"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def signin(email: str, password: str) -> str:
    """Sign in and return a short-lived bearer token."""
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
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
    h = {**HEADERS, "Authorization": f"Bearer {bearer}"}
    r = requests.get(
        f"{MLX_API}/workspace/automation_token?expiration_period={period}",
        headers=h,
        timeout=30,
    )
    if r.status_code != 200:
        return ""
    data = r.json().get("data") or {}
    return data.get("token") or data.get("automation_token") or ""


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
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        print(f"  [skip] .env not found at {env_path}")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("MULTILOGIN_AUTOMATION_TOKEN_A="):
            lines[i] = f"MULTILOGIN_AUTOMATION_TOKEN_A={token}"
            updated = True
            break
    if not updated:
        lines.append(f"MULTILOGIN_AUTOMATION_TOKEN_A={token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [ok] .env updated with new automation token")


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

    print("Getting automation token (1 month)...")
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
