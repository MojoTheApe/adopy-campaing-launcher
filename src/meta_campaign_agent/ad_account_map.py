from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AccountMapEntry:
    ad_account_id: str
    page_id: str | None = None
    multilogin_instance: str = "A"
    folder_id: str | None = None
    profile_id: str | None = None
    proxy_type: str | None = None
    proxy_host: str | None = None
    proxy_port: str | None = None
    proxy_user: str | None = None
    proxy_pass: str | None = None
    # Facebook Page display name — used only by the Campaign Builder's visual
    # ad preview (Feed/Reels mockup), not part of the real publish flow.
    page_name: str | None = None
    # Which country this account is worked in. Informational: the dashboard
    # groups by it and pre-fills it when an account is connected from inside a
    # country section. The CSV has carried this column all along without
    # anything reading it.
    country: str | None = None


def load_ad_account_map(path: str | Path) -> dict[str, AccountMapEntry]:
    """Load account_name -> AccountMapEntry."""
    p = Path(path)
    if not p.is_file():
        return {}

    out: dict[str, AccountMapEntry] = {}
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("account_name") or row.get("account") or "").strip()
            act = (row.get("ad_account_id") or row.get("act_id") or "").strip()
            page_id = (row.get("page_id") or row.get("facebook_page_id") or "").strip() or None
            if not name or not act:
                continue
            if not act.startswith("act_"):
                act = f"act_{act.lstrip('act_')}"
            ml_instance = (row.get("multilogin_instance") or row.get("ml_instance") or "A").strip().upper()
            out[name] = AccountMapEntry(
                ad_account_id=act,
                page_id=page_id,
                multilogin_instance=ml_instance if ml_instance in ("A", "B") else "A",
                folder_id=(row.get("folder_id") or row.get("multilogin_folder_id") or "").strip() or None,
                profile_id=(row.get("profile_id") or row.get("multilogin_profile_id") or "").strip() or None,
                proxy_type=(row.get("proxy_type") or "").strip() or None,
                proxy_host=(row.get("proxy_host") or "").strip() or None,
                proxy_port=(row.get("proxy_port") or "").strip() or None,
                proxy_user=(row.get("proxy_user") or row.get("proxy_username") or "").strip() or None,
                proxy_pass=(row.get("proxy_pass") or row.get("proxy_password") or "").strip() or None,
                page_name=(row.get("page_name") or "").strip() or None,
                country=(row.get("country") or "").strip() or None,
            )
    return out


def resolve_account_entry(map_data: dict[str, AccountMapEntry], sheet_name: str) -> AccountMapEntry | None:
    name = (sheet_name or "").strip()
    if not name:
        return None
    if name in map_data:
        return map_data[name]
    lower = name.lower()
    for key, entry in map_data.items():
        if key.lower() == lower:
            return entry
    for key, entry in map_data.items():
        if lower in key.lower() or key.lower() in lower:
            return entry
    return None
