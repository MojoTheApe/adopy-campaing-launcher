"""Which country an account belongs to — and why that can be more than one.

The obvious model is wrong: an account does not have "a country". Checked
against the real sheet on 2026-08-30, five of fourteen accounts appear under two
country sections at once, and the two cases mean different things:

    FacebookMKTOBFLW06   Chile / Dead      UAE / New       ← moved
    FacebookMKTOBFLV37   Ecuador / Active  Costa Rica / Active  ← runs both

So the sheet is not a lookup table of one answer per account. It says which
countries an account is *for*, and the status says which of those are still
alive.

Sources, in order:

1. **The accounts sheet.** The operator maintains it; it is the primary statement
   of intent. Rows marked dead are dropped when the account has a live row —
   that is exactly how a move looks on paper.
2. **Operator links.** Used when the sheet does not name a country.
3. **Recent stats, by LATEST DATE.** Used to choose between several live
   countries, and as the whole answer for accounts the sheet does not mention.
4. **Never the suffix in the account label or the legacy map column.**

The label-suffix rule is the one that cost time. `FacebookElf303 PA Amz` reads as
"Panama, Amazon" and looks authoritative; it is typed by hand, is sometimes
wrong, and stays put while the account moves. Reading a country off it produced
seven "mismatches" out of twenty-four accounts — every one of them a stale
label, not a wrong account.

**Latest date, not most frequent.** An account that moved has more history in
the country it LEFT. FacebookElf303 carries 46 Chile rows against 4 Panama and
its newest day is Panama: counting rows answers "where has it been", and the
question is always "where is it now".
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from src.analysis_agent.analyze_fb_stats import normalize_account_alias
from src.analysis_agent.country_names import country_key

# Statuses that mean "not running any more". Compared case-insensitively; an
# unknown status counts as alive, because guessing an account dead hides it.
_DEAD = {"dead", "banned", "closed", "archived", "мертв", "мёртв"}


def country_from_stats(rows: Sequence[Sequence[Any]]) -> dict[str, str]:
    """{account key: country} from (alias, country, date) rows.

    Rows are read in the order given and the last one wins, so the caller sorts
    by date with a stable sort: the newest day answers, and the file's own order
    breaks ties inside a day.
    """
    out: dict[str, str] = {}
    for row in rows or []:
        if len(row) < 2:
            continue
        key = normalize_account_alias(str(row[0] or ""))
        country = str(row[1] or "").strip()
        if key and country:
            out[key] = country
    return out


def sheet_countries(sheet_rows: Iterable[dict]) -> dict[str, list[str]]:
    """{account key: live countries} from the accounts sheet.

    `country_code` holds the country NAME ("Chile", "Ecuador") — the section
    header the account sits under, not a two-letter code.

    Dead rows are kept only when the account has no live row at all: an account
    that is dead everywhere still belongs somewhere, and showing nothing would
    be worse than showing where it last was.
    """
    live: dict[str, list[str]] = {}
    dead: dict[str, list[str]] = {}
    for row in sheet_rows or []:
        key = normalize_account_alias(str(row.get("account_label") or row.get("alias") or ""))
        country = str(row.get("country_code") or "").strip()
        if not key or not country:
            continue
        status = str(row.get("account_status") or "").strip().lower()
        bucket = dead if status in _DEAD else live
        if country not in bucket.setdefault(key, []):
            bucket[key].append(country)
    for key, countries in dead.items():
        if key not in live:
            live[key] = countries
    return live


def linked_countries(links: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """{account key: countries} from what the operator connected by hand.

    Takes (account label, country) pairs — `account_links.all_pairs("country")`
    gives exactly that.
    """
    out: dict[str, list[str]] = {}
    for label, country in links or []:
        key = normalize_account_alias(str(label or ""))
        name = str(country or "").strip()
        if not key or not name:
            continue
        if name not in out.setdefault(key, []):
            out[key].append(name)
    return out


def resolve_countries(
    from_stats: dict[str, str],
    sheet_rows: Iterable[dict],
    linked: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """{account key: {"country": str, "countries": [str], "ambiguous": bool}}.

    `country` is the single best answer for a place that can show only one —
    a column, a heading. `countries` is the honest list. `ambiguous` says the
    single value was a choice among live options, so a caller that cares can
    show all of them instead.

    Choosing between several live countries is done by the stats: whichever the
    account most recently ran is the one it is running now. When the stats do
    not name any of them, no country is picked — an invented answer here aims a
    campaign at a market.

    Source precedence is sheet sections, then operator links, then recent
    stats. Lower-priority sources never add countries to a stated set. Stats
    may choose a primary country only from that set. Unknown country names
    are preserved rather than inferred from the account label or legacy map.
    """
    out: dict[str, dict] = {}
    by_sheet = sheet_countries(sheet_rows)
    by_link = dict(linked or {})

    for key in set(by_sheet) | set(from_stats) | set(by_link):
        # Preserve every option from the highest-priority available source.
        listed = []
        seen = set()
        for name in by_sheet.get(key) or by_link.get(key, []):
            identity = country_key(name)
            if identity and identity not in seen:
                seen.add(identity)
                listed.append(name)
        recent = from_stats.get(key, "")

        if not listed:
            # Not in the sheet: the stats are all we have, and that is fine.
            out[key] = {"country": recent, "countries": [recent] if recent else [], "ambiguous": False}
            continue

        if len(listed) == 1:
            out[key] = {"country": listed[0], "countries": listed, "ambiguous": False}
            continue

        chosen = next((name for name in listed if country_key(name) == country_key(recent)), "")
        out[key] = {"country": chosen, "countries": listed, "ambiguous": True}

    return out


def primary_country(resolved: dict[str, dict]) -> dict[str, str]:
    """Just the single answers — for callers that cannot show a list."""
    return {k: v["country"] for k, v in resolved.items() if v.get("country")}
