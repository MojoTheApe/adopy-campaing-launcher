"""Pre-flight validation for §11 upload bundles."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from config.paths import get_creatives_root
from src.campaign_bridge.link_validation import classify_link, link_error_message
from src.campaign_bridge.open_rec_mapper import parse_creative_from_ad_title
from src.upload_agent.build_upload_excel import get_account_info, lookup_creative_file


def resolve_account_sheet_name(ref: dict, account_label: str) -> str | None:
    accounts = ref.get("accounts") or {}
    name = (account_label or "").strip()
    if not name:
        return None
    if name in accounts:
        return name
    for key in accounts:
        if name.upper() in key.upper():
            return key
    return None


def _creative_file_on_disk(filename: str, creatives_root: str) -> bool:
    if not filename or not creatives_root:
        return False
    root = Path(creatives_root)
    if not root.is_dir():
        return False
    target = filename.strip()
    for path in root.rglob("*"):
        if path.is_file() and path.name == target:
            return True
    return False


def validate_plan_rows(
    ref: dict,
    plan_rows: list[dict[str, Any]],
    *,
    account_label: str,
    creatives_root: str | None = None,
) -> list[str]:
    """Return blocking error messages; empty list means OK."""
    errors: list[str] = []
    sheet_name = resolve_account_sheet_name(ref, account_label)
    if not sheet_name:
        errors.append("Account not found in Upload FB")
        return errors

    account_info = get_account_info(ref, account_label)
    link_status = classify_link(account_info.get("link"))
    link_msg = link_error_message(link_status)
    if link_msg:
        errors.append(link_msg)

    pixel = account_info.get("pixel")
    if pixel is None or not str(pixel).strip():
        errors.append("Pixel missing in Upload FB Accounts")

    if not plan_rows:
        errors.append("No ad rows selected — check at least one strategy with ad titles")
        return errors

    root = creatives_root or get_creatives_root()
    seen: set[tuple[str, str]] = set()
    for row in plan_rows:
        title = (row.get("copy") or "").strip()
        creative_type, creative_number = parse_creative_from_ad_title(title)
        key = (creative_type, creative_number)
        if key in seen:
            continue
        seen.add(key)

        if not creative_number:
            errors.append(f"Cannot parse creative number from: {title}")
            continue

        filename = lookup_creative_file(ref, creative_type, creative_number)
        if not filename:
            errors.append(f"Creative file name missing for #{creative_number}")
            continue
        if not _creative_file_on_disk(filename, root):
            errors.append(f"Creative file not found: {filename}")

    return errors
