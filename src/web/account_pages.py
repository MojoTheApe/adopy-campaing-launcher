"""Campaign-local Page choices using the existing read-only profile session."""
from __future__ import annotations

from typing import Any


def pages_from_client(client: Any, act: str) -> list[dict[str, str]]:
    path = f"/{act}/promote_pages"
    rows = client.get_all(path, {"fields": "id,name", "limit": 100}, max_pages=5)
    if path in getattr(client, "truncated", []):
        raise ValueError("The Facebook Page list is incomplete. Refresh pages before choosing.")
    pages = {}
    for row in rows:
        pid = str(row.get("id") or "").strip()
        if pid.isdigit():
            pages.setdefault(pid, {"id": pid, "name": str(row.get("name") or pid)})
    return list(pages.values())


def resolve_selection(pages: list[dict[str, str]], selected: str = "") -> dict[str, str]:
    if not pages:
        raise ValueError("No Facebook Pages are available for this ad account. "
                         "Refresh pages or grant this account access to a Page.")
    if not selected:
        return pages[0]
    for page in pages:
        if page["id"] == selected:
            return page
    raise ValueError("The selected Facebook Page is not available for this account. Refresh pages and choose again.")


def _read_in_session(account: str) -> dict:
    from src.analysis_agent.account_spend_api import _session

    with _session(account) as sess:
        if sess.client.allow_writes:
            raise AssertionError("Page discovery must be read-only")
        sess.client.reset_budget(8)
        pages = pages_from_client(sess.client, sess.act)
        return {"ok": True, "state": "ready" if pages else "empty", "pages": pages,
                "act": sess.act, "account": account, "message": "" if pages else
                "No Facebook Pages are available. Grant this account access to a Page, then Refresh pages."}


def read_pages(account: str) -> dict:
    from src.analysis_agent import account_spend as accounts
    from src.analysis_agent import account_spend_api as api

    read = accounts.serialized_browser(key=accounts._profile_key, reading=True)(_read_in_session)
    try:
        return read(account)
    except Exception as exc:
        return {"ok": False, "state": "failed", "pages": [], "account": account,
                "message": "Could not load Facebook Pages. Refresh pages to retry. " + api._failure(account, exc)[0]}
