#!/usr/bin/env python3
"""List all Multilogin profiles with folder_id and profile_id.

Run: python scripts/ml_list_profiles.py
Outputs a table ready to copy into config/ad_account_map.csv
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.chdir(_root)

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env")
except ImportError:
    pass

import requests

LAUNCHER_BASE = (os.environ.get("MULTILOGIN_LAUNCHER_BASE_A") or "https://launcher.mlx.yt:45001/api/v2").rstrip("/")
TOKEN = (os.environ.get("MULTILOGIN_AUTOMATION_TOKEN_A") or "").strip()

if not TOKEN:
    print("ERROR: MULTILOGIN_AUTOMATION_TOKEN_A not set in .env")
    sys.exit(1)

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}


def get_folders() -> list[dict]:
    url = f"{LAUNCHER_BASE}/profile"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
    except requests.ConnectionError as e:
        print(f"ERROR: Cannot connect to Multilogin Launcher ({LAUNCHER_BASE})")
        print(f"  Make sure Multilogin is running on this machine.")
        print(f"  Detail: {e}")
        sys.exit(1)

    if r.status_code == 401:
        print("ERROR: Automation token rejected (401). Check MULTILOGIN_AUTOMATION_TOKEN_A in .env")
        sys.exit(1)
    if r.status_code >= 400:
        print(f"ERROR: API returned HTTP {r.status_code}: {r.text[:500]}")
        sys.exit(1)

    body = r.json()
    # MLX returns {"data": [...]} or a list directly
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        data = body.get("data") or body.get("profiles") or []
        if isinstance(data, list):
            return data
    return []


def main():
    print(f"Connecting to Multilogin: {LAUNCHER_BASE}")
    profiles = get_folders()

    if not profiles:
        print("No profiles found. Make sure Multilogin is running and profiles exist.")
        sys.exit(0)

    print(f"\nFound {len(profiles)} profile(s):\n")

    # Print table header
    col_w = [40, 38, 38]
    header = f"{'Profile name':<{col_w[0]}} {'folder_id':<{col_w[1]}} {'profile_id':<{col_w[2]}}"
    print(header)
    print("-" * sum(col_w))

    rows = []
    for p in profiles:
        name    = str(p.get("name") or p.get("profile_name") or "—")
        fid     = str(p.get("folder_id") or p.get("folderId") or "—")
        pid     = str(p.get("profile_id") or p.get("uuid") or p.get("id") or "—")
        print(f"{name:<{col_w[0]}} {fid:<{col_w[1]}} {pid:<{col_w[2]}}")
        rows.append((name, fid, pid))

    print()
    print("─" * 40)
    print("Next step: fill config/ad_account_map.csv")
    print("Template (copy the example):")
    print()
    print("account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,proxy_type,proxy_host,proxy_port,proxy_user,proxy_pass")
    for name, fid, pid in rows[:3]:
        print(f"<FB_account_name>,act_<ad_account_id>,<page_id>,A,{fid},{pid},http,<proxy_host>,<port>,<proxy_user>,<proxy_pass>")


if __name__ == "__main__":
    main()
