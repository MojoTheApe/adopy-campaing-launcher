"""Say which Multilogin account an ad account will actually be opened with.

`Start profile failed HTTP 401` has three different causes that look identical
from the dashboard: the account points at the wrong slot, the slot holds a dead
token, or MLX Desktop is signed in as somebody else. Guessing between them cost
2026-09-03. This prints all three facts at once.

Never prints a token — only who issued it, for which workspace, and until when.

Usage:
  python scripts/acl_mlx_whoami.py                 # every account in the map
  python scripts/acl_mlx_whoami.py FacebookMKTOBFLX01
"""
from __future__ import annotations

import base64
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.paths import get_ad_account_map_path, load_env  # noqa: E402
from scripts._console import use_utf8  # noqa: E402

use_utf8()
load_env()

LABELS = {"A": "Inhouse", "B": "KLD"}


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _slot_connected(slot: str) -> bool:
    """Слот подключён: токеном автоматизации или сохранённым входом (ACL-1335)."""
    from src.meta_campaign_agent.mlx_credentials import has_saved_session

    token = (os.environ.get(f"MULTILOGIN_AUTOMATION_TOKEN_{slot}") or "").strip()
    return bool(token) or has_saved_session(slot)


def _slot_line(slot: str) -> str:
    from src.meta_campaign_agent.mlx_credentials import slot_summary

    token = (os.environ.get(f"MULTILOGIN_AUTOMATION_TOKEN_{slot}") or "").strip()
    label = LABELS.get(slot, slot)
    summary = slot_summary(slot)
    if summary.startswith("signed in"):
        # Машина, подключённая входом: токена автоматизации нет и не нужно.
        return f"  slot {slot} ({label}): {summary}"
    if not token:
        return f"  slot {slot} ({label}): EMPTY — no token and no saved sign-in"
    claims = _claims(token)
    if not claims:
        return f"  slot {slot} ({label}): a token is set, but it is not readable"
    exp = claims.get("exp")
    when = datetime.fromtimestamp(exp) if exp else None
    state = "ALIVE" if when and when > datetime.now() else "EXPIRED"
    return (
        f"  slot {slot} ({label}): {claims.get('email', '?')}"
        f"  workspace={str(claims.get('workspaceID', '?'))[:8]}…"
        f"  role={claims.get('workspaceRole', '?')}"
        f"  until {when:%Y-%m-%d %H:%M}" if when else ""
    ) + f"  — {state}"


def main() -> int:
    wanted = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()

    print("Multilogin connections on this machine")
    for slot in ("A", "B"):
        print(_slot_line(slot))

    path = Path(get_ad_account_map_path())
    print(f"\nAccount map: {path}")
    if not path.is_file():
        print("  missing — the RPA has no accounts to open")
        return 1

    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    shown = 0
    for row in rows:
        label = (row.get("account_name") or row.get("account_label") or row.get("label") or "").strip()
        if wanted and wanted not in label.lower():
            continue
        slot = (row.get("multilogin_instance") or "").strip().upper() or "A"
        note = "" if (row.get("multilogin_instance") or "").strip() else "  (blank → defaults to A)"
        mark = "ok" if _slot_connected(slot) else "SLOT NOT CONNECTED (no token, no sign-in)"
        print(
            f"  {label:<28} slot {slot} ({LABELS.get(slot, slot)}){note}"
            f"  profile_id={'set' if (row.get('profile_id') or '').strip() else 'MISSING'}"
            f"  folder_id={'set' if (row.get('folder_id') or '').strip() else 'missing'}"
            f"  → {mark}"
        )
        shown += 1

    if wanted and not shown:
        print(f"  no account matching {wanted!r} — the RPA cannot open what is not in the map")
        return 1

    print(
        "\nIf the slot above is not the Multilogin account whose profiles you use,"
        "\nopen that account in the dashboard's Multilogin panel and link the profile"
        "\nagain with Use — that writes the slot together with the profile."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
