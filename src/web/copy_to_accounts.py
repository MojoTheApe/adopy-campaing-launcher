"""ACL-2456: what the builder tells the buyer before a campaign is copied to other ad accounts.

The copy itself is made in the builder page (the same DOM copy as Duplicate / Move to...); the
account-bound fields never travel with it: the tracking link is the target form's own, the Page goes
back to Automatic, existing Instant Form ids are cleared. This module answers the questions the page
cannot answer alone, from the account map and the cached account readings, with no call to Meta:

* is the target a real, different ad account from the map;
* which pixel the account map holds for it (and whether the plan will have to guess);
* whether the budget can be kept (both currencies known and equal) or the buyer must enter it again.

A copy is a normal draft in the target account's form: it still goes through Preview and Check plan,
and is created PAUSED only through the API backend.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from src.meta_campaign_agent.ad_account_map import AccountMapEntry, resolve_account_entry

API_ONLY = "API backend only"

BUDGET_KEEP = "keep"
BUDGET_CONFIRM = "confirm"


class CopyRefused(ValueError):
    """The whole request is refused (wrong backend, no source account)."""


def _currency(facts: Mapping[str, Any] | None) -> str:
    return str((facts or {}).get("currency") or "").strip().upper()


def budget_rule(source_currency: str, target_currency: str) -> dict:
    """Keep the amount only when both currencies are known and equal; otherwise the buyer enters it.

    No exchange rate is applied: a converted number would be a guess. An unknown currency (the account
    was never read) counts as a mismatch, so the source amount is never carried over silently.
    """
    src, dst = (source_currency or "").strip().upper(), (target_currency or "").strip().upper()
    if src and dst and src == dst:
        return {"mode": BUDGET_KEEP, "reason": "", "source_currency": src, "target_currency": dst}
    if src and dst:
        reason = (f"The source budget is in {src}, this account is in {dst}. "
                  f"Enter the budget in {dst}; the source amount is not copied.")
    else:
        which = "target account" if not dst else "source account"
        reason = (f"The currency of the {which} is not known yet (no read of the account). "
                  "Enter the budget for this account; the source amount is not copied.")
    return {"mode": BUDGET_CONFIRM, "reason": reason, "source_currency": src, "target_currency": dst}


def _target(label: str, source_act: str, source_currency: str, entry: AccountMapEntry | None,
            facts: Mapping[str, Any] | None) -> dict:
    out: dict[str, Any] = {"label": label, "ok": True, "blockers": [], "notes": [], "pixel": "", "page": ""}
    if entry is None:
        out["ok"] = False
        out["blockers"].append("This account is not in the account map.")
        return out
    act = (entry.ad_account_id or "").strip()
    if not act:
        out["ok"] = False
        out["blockers"].append("The account map has no ad account id for it.")
    elif act == source_act:
        out["ok"] = False
        out["blockers"].append("Same ad account as the source.")
    out["ad_account_id"] = act
    out["budget"] = budget_rule(source_currency, _currency(facts))
    pixel = (entry.pixel_id or "").strip()
    out["pixel"] = pixel
    if pixel:
        out["notes"].append(f"Pixel {pixel} from the account map.")
    else:
        out["notes"].append("No pixel in the account map: Check plan uses the account's only pixel and "
                            "stops if it has several. Set the pixel in the account map to avoid that.")
    # The builder keeps the Page choice on the campaign: a copy always starts as Automatic.
    out["notes"].append("Page: Automatic (the source Page is not copied).")
    out["notes"].append("Tracking link: the target account's own. Instant Form ids are cleared.")
    return out


def describe_targets(source: str, targets: list[str], *, account_map: Mapping[str, AccountMapEntry],
                     facts_for: Callable[[str], Mapping[str, Any] | None],
                     api_backend: bool) -> dict:
    """The answer for the copy dialog. Raises `CopyRefused` for a request that cannot be served at all."""
    if not api_backend:
        raise CopyRefused(API_ONLY)
    source = (source or "").strip()
    src_entry = resolve_account_entry(dict(account_map), source) if source else None
    if src_entry is None:
        raise CopyRefused("The source account is not in the account map.")
    source_act = (src_entry.ad_account_id or "").strip()
    source_currency = _currency(facts_for(source))
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in targets:
        label = str(raw or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        rows.append(_target(label, source_act, source_currency, resolve_account_entry(dict(account_map), label),
                            facts_for(label)))
    return {"ok": True, "source": source, "source_currency": source_currency, "targets": rows}
