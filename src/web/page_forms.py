"""Список форм Страницы для выбора в билдере (ACL-1775).

Только чтение и только по явному нажатию «Refresh forms»: ни опроса, ни
таймеров. Путь тот же, что у «Check plan» и чтений ACL-1760: замок чтения
профиля, сессия Ads Manager в своей вкладке, клиент БЕЗ права записи.

Условия Lead Ads не принимаются никогда; форм здесь не создают (ACL-1776).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from src.fb_api import forms as _forms

#: Вызовов Graph на одно нажатие: страницы кабинета + список форм + условия.
READ_MAX_CALLS = 8


def page_for(entry_page_id: str, client: Any, act: str) -> str:
    """Страница аккаунта: из карты; пусто - единственная из `promote_pages` кабинета."""
    page_id = (entry_page_id or "").strip()
    if page_id:
        return page_id
    pages = client.get_all(f"/{act}/promote_pages", {"fields": "id,name", "limit": 100}, max_pages=1)
    ids = [str(p.get("id")) for p in pages if p.get("id")]
    return ids[0] if ids else ""


def _read_in_session(account_label: str, selected_page: str | None = None) -> dict[str, Any]:
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    entry = _as._entry_for(account_label)
    entry_page = str(getattr(entry, "page_id", "") or "") if entry is not None else ""
    with _api._session(account_label) as sess:
        client = sess.client
        if client.allow_writes:
            raise AssertionError("the forms read must not be able to write")
        client.reset_budget(READ_MAX_CALLS)
        if selected_page is None:
            page_id = page_for(entry_page, client, sess.act)
        else:
            from src.web.account_pages import pages_from_client, resolve_selection
            page_id = resolve_selection(pages_from_client(client, sess.act), selected_page)["id"]
        if not page_id:
            read = _forms.forms_capability("", [])
        else:
            read = _forms.read_page_forms(client, page_id)
        if client.writes:
            raise AssertionError("the forms read must not write")
    return read.as_dict()


def read_forms(account_label: str, selected_page: str | None = None) -> dict[str, Any]:
    """Формы Страницы аккаунта. Всегда словарь: `ok`, `forms`, `message`, `state`."""
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    started = time.monotonic()
    run = _as.serialized_browser(key=_as._profile_key, reading=True)(
        lambda account: _read_in_session(account, selected_page))
    try:
        out = run(account_label)
        error = ""
    except Exception as exc:  # noqa: BLE001 - отказ профиля/страницы: текстом человеку
        error = _api._failure(account_label, exc)[0]
        out = {"state": "failed", "ok": False, "page_id": "", "forms": [], "message": error}
    out["account"] = account_label
    out["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    # Журнал не должен ломать чтение (`_record` сам глотает сбои).
    _as._record("api_forms_read", not error and out.get("ok") is True, time.monotonic() - started,
                {"account": account_label, "source": "api", "state": out.get("state"),
                 "forms": len(out.get("forms") or [])})
    return out
