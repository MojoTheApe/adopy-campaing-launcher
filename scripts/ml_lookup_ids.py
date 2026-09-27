#!/usr/bin/env python3
"""Look up Multilogin folder_id by folder name or profile name/id."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.chdir(_root)

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass

MLX_API = "https://api.multilogin.com"


def _token(instance: str) -> str:
    key = instance.strip().upper()
    if key not in ("A", "B"):
        key = "A"
    # ACL-1335: токен автоматизации или сохранённый вход — у поставщика слота.
    from src.meta_campaign_agent.mlx_credentials import MlxCredentialError, for_slot

    try:
        return for_slot(key).bearer()
    except MlxCredentialError as exc:
        raise SystemExit(str(exc))


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def list_folders(token: str) -> list[dict]:
    r = requests.get(f"{MLX_API}/workspace/folders", headers=_headers(token), timeout=60)
    r.raise_for_status()
    body = r.json()
    data = body.get("data") if isinstance(body, dict) else {}
    folders = data.get("folders") if isinstance(data, dict) else []
    return folders if isinstance(folders, list) else []


def search_profiles(token: str, *, search_text: str) -> list[dict]:
    r = requests.post(
        f"{MLX_API}/profile/search",
        headers=_headers(token),
        json={"limit": 50, "offset": 0, "search_text": search_text},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    data = body.get("data") if isinstance(body, dict) else {}
    profiles = data.get("profiles") if isinstance(data, dict) else []
    return profiles if isinstance(profiles, list) else []


def main() -> int:
    p = argparse.ArgumentParser(description="Resolve Multilogin folder_id / profile metadata")
    p.add_argument("--instance", default="A", help="Multilogin instance A or B (MKTO uses A)")
    p.add_argument("--folder-name", help="Folder display name, e.g. 'KLD FB'")
    p.add_argument("--profile-name", help="Profile name, e.g. KLD_BFL_O13")
    p.add_argument("--profile-id", help="Profile UUID")
    args = p.parse_args()

    token = _token(args.instance)
    out: dict = {"instance": args.instance.upper()}

    if args.folder_name:
        folders = list_folders(token)
        matches = [
            f
            for f in folders
            if str(f.get("name", "")).strip().lower() == args.folder_name.strip().lower()
        ]
        if not matches:
            partial = [
                f
                for f in folders
                if args.folder_name.strip().lower() in str(f.get("name", "")).strip().lower()
            ]
            matches = partial
        out["folders_matching_name"] = matches

    search = args.profile_name or args.profile_id or ""
    if search:
        profiles = search_profiles(token, search_text=search)
        if args.profile_id:
            profiles = [pr for pr in profiles if pr.get("profile_id") == args.profile_id] or profiles
        out["profiles_matching"] = profiles

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
