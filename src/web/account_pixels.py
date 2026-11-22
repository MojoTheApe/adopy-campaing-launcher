"""Пиксель кампании выбирается в билдере, как Страница (ACL-1880): список пикселей
кабинета читается той же сессией профиля только на чтение, без прямых вызовов Graph
снаружи Multilogin. Выбор уходит в черновик как `pixel_id` (источник "draft"), а
`creator.resolve_pixel` при Check plan / Publish сверяет его с кабинетом.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

#: Как у списка Страниц: Check plan всё равно сверяет выбранный пиксель в своей
#: сессии, поэтому устаревший список публикацию не испортит.
CACHE_TTL_S = 12 * 3600
#: Столько пикселей читает и `creator.read_account_facts` (одна страница ответа):
#: пиксель дальше первой страницы план не увидит, поэтому и выбирать его нельзя.
PIXEL_LIMIT = 50
_CACHE_LOCK = threading.Lock()


def _cache_path() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / "account_pixels_cache.json"


def _cache_load() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cached_pixels(account: str, *, now: float | None = None) -> dict | None:
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


def remember_pixels(account: str, result: dict, *, now: float | None = None) -> None:
    """Запомнить только непустое удачное чтение (ready); пустой ответ и ошибки не кэшируются:
    после подключения пикселя в Business Settings следующий клик перечитывает список (ACL-2835)."""
    if not (isinstance(result, dict) and result.get("ok") and result.get("state") == "ready"
            and result.get("pixels")):
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


def pixels_from_client(client: Any, act: str) -> list[dict[str, str]]:
    """Пиксели/датасеты кабинета: [{id, name}] в порядке ответа Meta."""
    rows = client.get_all(f"/{act}/adspixels", {"fields": "id,name", "limit": PIXEL_LIMIT}, max_pages=1)
    out: list[dict[str, str]] = []
    for row in rows:
        pid = str(row.get("id") or "").strip()
        if pid:
            out.append({"id": pid, "name": str(row.get("name") or "").strip() or pid})
    return out


def _message(pixels: list[dict[str, str]]) -> str:
    if not pixels:
        return ("This ad account has no pixel/dataset. Website ad sets need one: create a pixel in "
                "Events Manager, then Refresh pixels. Instant Form campaigns do not need a pixel.")
    if len(pixels) > 1:
        return f"This ad account has {len(pixels)} pixels. Choose the one for this campaign."
    return ""


def _read_in_session(sess: Any, account: str) -> dict:
    """Один запрос `/{act}/adspixels` в уже открытой сессии профиля (только чтение)."""
    if sess.client.allow_writes:
        raise AssertionError("Pixel discovery must be read-only")
    sess.client.reset_budget(2)
    pixels = pixels_from_client(sess.client, sess.act)
    return {"ok": True, "state": "ready" if pixels else "empty", "pixels": pixels,
            "act": sess.act, "account": account, "message": _message(pixels)}


def fill_from_facts(account: str, facts: Any) -> None:
    """ACL-2850: Check plan / Publish уже прочитали `/adspixels` в своей сессии - пустой или
    устаревший кэш списка берёт этот ответ. Новых запросов и сессий нет; сбой молчит."""
    try:
        ids = [str(p) for p in (getattr(facts, "pixels", None) or []) if p]
        if not ids or cached_pixels(account) is not None:
            return
        names = getattr(facts, "pixel_names", None) or {}
        pixels = [{"id": pid, "name": names.get(pid) or pid} for pid in ids]
        remember_pixels(account, {"ok": True, "state": "ready", "pixels": pixels,
                                  "act": getattr(facts, "act", ""), "account": account,
                                  "message": _message(pixels)})
    except Exception:
        pass


def cached_only(account: str) -> dict:
    """Ответ для отрисовки: список из кэша или «не загружено». Профиль не открывается."""
    hit = cached_pixels(account)
    if hit is not None:
        return hit
    return {"ok": True, "state": "not_loaded", "pixels": [], "account": account,
            "message": "Pixels are not loaded yet. Click the Pixel control or Refresh pixels to load them "
                       "(this opens the account profile)."}


def read_pixels(account: str, *, force: bool = False) -> dict:
    """Список пикселей: кэш или одна сессия профиля, в которой заодно читаются Страницы, если их кэш пуст (ACL-2850)."""
    from src.web import account_lists

    return account_lists.read_lists(account, "pixels", force=force)


def stream_pixels(account: str, *, force: bool = False):
    """Тот же `read_pixels`, но со строками ожидания по ходу: {"progress": ...} ... {"result": ...} (ACL-2850)."""
    from src.web import account_lists

    return account_lists.stream(account, "pixels", force=force)
