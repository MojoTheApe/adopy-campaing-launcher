"""Campaign-local Page choices using the existing read-only profile session."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

#: Список Страниц аккаунта живёт на машине столько; потом читается заново.
#: Check plan сам сверяет выбранную Страницу в своей сессии, так что
#: устаревший список публикацию не испортит, а лишний запуск профиля
#: (очередь по памяти и замку) стоит минуты.
CACHE_TTL_S = 12 * 3600
_CACHE_LOCK = threading.Lock()


def _cache_path() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / "account_pages_cache.json"


def _cache_load() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cached_pages(account: str, *, now: float | None = None) -> dict | None:
    """Свежий ответ из кэша или None (нет, устарел, файл не читается)."""
    with _CACHE_LOCK:
        entry = _cache_load().get(account)
    if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
        return None
    at = entry.get("at")
    now = time.time() if now is None else now
    if not isinstance(at, (int, float)) or not (0 <= now - at < CACHE_TTL_S):
        return None
    return {**entry["result"], "cached_at": at}


def remember_pages(account: str, result: dict, *, now: float | None = None) -> None:
    """Запомнить только удачное чтение (ready/empty); ошибки не кэшируются."""
    if not (isinstance(result, dict) and result.get("ok") and result.get("state") in ("ready", "empty")):
        return
    from config.paths import _atomic_write

    with _CACHE_LOCK:
        data = _cache_load()
        data[account] = {"at": time.time() if now is None else now,
                         "result": {k: v for k, v in result.items() if k != "cached_at"}}
        try:
            _atomic_write(_cache_path(), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
        except OSError:
            pass


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
                "No Page is available for this account. With Automatic, the Graph API backend creates only "
                "the website campaign and ad sets PAUSED (add a Page and ads in Ads Manager), and the RPA "
                "backend leaves the Identity as Ads Manager sets it. A specific Page or an Instant Form "
                "requires a Page; Refresh pages after restoring access."}


def cached_only(account: str) -> dict:
    """Render-time answer: the cached list or "not loaded". Never opens a profile."""
    hit = cached_pages(account)
    if hit is not None:
        return hit
    return {"ok": True, "state": "not_loaded", "pages": [], "account": account,
            "message": "Pages are not loaded yet. Click the Page control or Refresh pages to load them "
                       "(this opens the account profile)."}


def read_pages(account: str, *, force: bool = False) -> dict:
    from src.analysis_agent import account_spend as accounts
    from src.analysis_agent import account_spend_api as api

    if not force:
        hit = cached_pages(account)
        if hit is not None:
            return hit
    read = accounts.serialized_browser(key=accounts._profile_key, reading=True)(_read_in_session)
    try:
        result = read(account)
        remember_pages(account, result)
        return result
    except Exception as exc:
        return {"ok": False, "state": "failed", "pages": [], "account": account,
                "message": "Could not load Facebook Pages. Refresh pages to retry. " + api._failure(account, exc)[0]}
