"""Does an account's label agree with the country its data actually comes from?

Account labels carry annotations after the name — `FacebookElf303 PA Amz` reads
as "Panama, Amazon". Those annotations are written by hand and nothing verifies
them, so they drift from reality and then quietly mislead: the tag
`rpa-v96-spend-confirmed-2026-08-24` records exactly this as a known defect —
row 15 of the account map said `PA Amz` while the account was in fact
Chile/BCI. That is the account we published the first live campaign into.

Why it matters more than a wrong caption: the label is what a person reads when
deciding which account to build a campaign in. Believing an account is Panamanian
when it is Chilean means aiming a campaign at the wrong market, in the account
whose money is being spent.

This module only compares — it does not rewrite labels. A label may legitimately
say something we cannot verify, and silently "fixing" someone's naming would be
worse than reporting the disagreement.
"""
from __future__ import annotations

from typing import Iterable

# Two-letter codes that appear as annotations in account labels, with the
# country names the stats use.
_CODES: dict[str, str] = {
    "AR": "Argentina",
    "BR": "Brazil",
    "CL": "Chile",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "EC": "Ecuador",
    "GT": "Guatemala",
    "KW": "Kuwait",
    "MX": "Mexico",
    "PA": "Panama",
    "PE": "Peru",
    "PY": "Paraguay",
    "SA": "Saudi Arabia",
    "SV": "El Salvador",
    "UY": "Uruguay",
}


def label_country_code(label: str) -> str:
    """The country code a label claims, or "" when it claims none.

    Only a standalone two-letter token counts, and only one we know: the second
    token in `FacebookMKTOBFLV37 EC Ait` is the country, `Ait` is the asset. A
    label that names no country is not a disagreement — it is a label without a
    claim, which is fine.
    """
    for token in (label or "").split()[1:]:
        code = token.strip().upper()
        if code in _CODES:
            return code
    return ""


def disagreements(rows: Iterable[dict]) -> list[dict]:
    """Accounts whose label names one country while the data says another.

    Each row needs `label` and `country_name` — the country MOST of that
    account's stats rows are attributed to. That is a strong signal and not a
    verdict: an account that ran two markets shows only the dominant one, so a
    disagreement means "go and look", not "the label is wrong".

    Rows without either field are skipped rather than reported: an account with
    no data yet has nothing to disagree with.

    Returns [{label, claims, actually}] — enough for a person to go and look.
    """
    out: list[dict] = []
    for row in rows or []:
        label = (row.get("label") or "").strip()
        actual = (row.get("country_name") or "").strip()
        if not label or not actual:
            continue
        code = label_country_code(label)
        if not code:
            continue
        claimed = _CODES[code]
        if claimed.casefold() != actual.casefold():
            out.append({"label": label, "claims": claimed, "actually": actual})
    return out
