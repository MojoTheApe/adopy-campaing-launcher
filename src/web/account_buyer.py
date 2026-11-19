"""Which buyer an account was connected under on this machine.

The machine-local account map is one file, but one machine can be switched to
another buyer ("Logged as"). Stats follow the selected buyer through BI; the
account map follows the same switch through a `buyer` column on each map row.

A row without a tag belongs to the machine's default buyer (`default_cm()`), so
every existing map keeps working and nothing is moved or lost. Only a view
asks "whose is this"; profiles, reads and publishing are keyed by account name
and never look at the tag, so they work whichever buyer is selected.

Older versions ignore the extra column and keep it when they rewrite the file.
"""
from __future__ import annotations

import csv
from pathlib import Path

from config.paths import get_ad_account_map_path

COLUMN = "buyer"


def _key(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def tags() -> dict[str, str]:
    """{account key: buyer tag} for every map row; the tag is "" when untagged."""
    path = Path(get_ad_account_map_path())
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                name = _key(row.get("account_name") or row.get("account") or "")
                tag = " ".join(str(row.get(COLUMN) or "").split())
                if name:
                    out[name] = tag
    except (OSError, csv.Error, UnicodeDecodeError):
        return {}
    return out


def owner_of(label: str, tag_map: dict[str, str] | None = None) -> str | None:
    """The buyer an account belongs to; the default buyer when untagged.

    None for an account with no map row at all: nobody connected it here, so it
    has no owner on this machine (the stats feeds already filter it by buyer).
    """
    from src.web.buyer import default_cm

    found = (tags() if tag_map is None else tag_map).get(_key(label))
    if found is None:
        return None
    # No default buyer configured and no tag: there is nothing to separate by.
    return found or default_cm() or None


def tag_for_new_row(selected: str | None = None) -> str:
    """Tag to store on a newly connected account.

    Blank for the default buyer: untagged already means "default", and leaving
    it blank keeps the file identical to what older versions wrote.
    """
    from src.web.buyer import default_cm, get_selected_cm

    chosen = (get_selected_cm() if selected is None else selected).strip()
    return "" if _key(chosen) == _key(default_cm()) else chosen


def is_visible(label: str, selected: str | None = None, tag_map: dict[str, str] | None = None) -> bool:
    """Whether the account belongs on the selected buyer's screens."""
    from src.web.buyer import get_selected_cm

    chosen = get_selected_cm() if selected is None else selected
    owner = owner_of(label, tag_map)
    return owner is None or _key(owner) == _key(chosen)
