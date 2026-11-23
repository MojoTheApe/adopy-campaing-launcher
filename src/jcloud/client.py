"""Поиск лендингов в JCloudFront по MCP (Streamable HTTP), только чтение (ACL-2852).

Токен читается из окружения машины (`JCLOUD_MCP_TOKEN`, его кладёт `load_env`) и
уходит только в заголовок `Authorization` запроса к MCP. В ответы, сообщения об
ошибках и логи он не попадает: любое сообщение проходит через `_redact`.

Что вводит человек, определяется по виду строки:
  https://…   -> kind=url
  /путь       -> kind=path
  Variant     -> код из статистики (1_ar_fm), уходит как набрано с kind=variant:
                 JCloudFront сам проверяет точный путь, затем код внутри адреса.
Выдача всегда сгруппирована по конфигурации (ACL-1872): одна карточка на уникальную
конфигурацию, `count` — сколько лендингов в группе; выбирается представитель группы.
Полный URL сначала превращается в лендинг (kind=url), затем берутся варианты его
шаблона по `source_landing_id` на всех доменах.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

PAGE_SIZE = 20
TIMEOUT_S = 25.0
#: Поля карточки, которые отдаём в браузер. Полный ответ MCP наружу не уходит.
ITEM_FIELDS = ("id", "title", "url", "domain", "path", "template", "language", "status", "page_type")
#: Карточка группы: поля представителя + число лендингов и id конфигурации.
GROUP_FIELDS = ITEM_FIELDS + ("count", "configuration_id")

_MESSAGES = {
    "not_configured": "JCloudFront is not connected on this machine (no MCP address or token).",
    "unavailable": "JCloudFront client is not installed (package mcp).",
    "unreachable": "JCloudFront is not reachable. On the test setup, check that the SSH tunnel is open.",
    "unauthorized": "JCloudFront refused the token. Ask the administrator for a new one.",
    "timeout": "JCloudFront did not answer in time. Try again.",
    "invalid_query": "This does not look like a landing URL, a /path or a landing name.",
    "query_too_broad": "Too many landings match. Narrow the search (add the domain or a longer name).",
    "snapshot_changed": "The catalog changed while you were paging. Search again from the first page.",
    "failed": "JCloudFront returned an error. Try again.",
}


def classify_query(text: str) -> tuple[str, str]:
    """(kind, query): kind is url | path | name."""
    q = (text or "").strip()
    if re.match(r"^https?://", q, re.I):
        return "url", q
    if q.startswith("/"):
        return "path", q
    return "name", q


def _settings() -> tuple[str, str]:
    return (os.environ.get("JCLOUD_MCP_URL") or "").strip(), (os.environ.get("JCLOUD_MCP_TOKEN") or "").strip()


def configured() -> bool:
    url, token = _settings()
    return bool(url and token)


def _redact(text: str) -> str:
    token = _settings()[1]
    return text.replace(token, "***") if token and text else text


def _error(code: str, message: str | None = None, **extra: Any) -> dict:
    return {"ok": False, "state": "failed", "code": code,
            "message": _redact(message or _MESSAGES.get(code, _MESSAGES["failed"])), **extra}


def _leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        yield exc


def _classify_exception(exc: BaseException) -> str:
    import httpx

    for leaf in _leaves(exc):
        if isinstance(leaf, httpx.HTTPStatusError) and leaf.response.status_code in (401, 403):
            return "unauthorized"
        if isinstance(leaf, (httpx.TimeoutException, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(leaf, (httpx.TransportError, OSError)):
            return "unreachable"
    return "failed"


async def _call_tool(tool: str, args: dict) -> dict:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url, token = _settings()
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=TIMEOUT_S) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool(tool, args)
    data = getattr(response, "structuredContent", None)
    if getattr(response, "isError", False) or not isinstance(data, dict):
        raise RuntimeError("JCloudFront returned an invalid response")
    return data


def _call(tool: str, args: dict) -> dict:
    return asyncio.run(asyncio.wait_for(_call_tool(tool, args), TIMEOUT_S + 5))


def _group(raw: dict) -> dict:
    """Карточка группы: представитель (его и сохраняем как выбранный лендинг) + число и id конфигурации."""
    rep = raw.get("representative") if isinstance(raw.get("representative"), dict) else raw
    card = {k: rep.get(k) for k in ITEM_FIELDS}
    config = raw.get("configuration") if isinstance(raw.get("configuration"), dict) else {}
    card["count"] = raw.get("count") or 1
    card["configuration_id"] = config.get("id")
    return card


def _shape(data: dict, *, searched_as: str, page: int) -> dict:
    if not data.get("ok", True):
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = str(err.get("code") or "failed")
        known = code if code in _MESSAGES else "failed"
        extra = {}
        if err.get("retry_after_seconds"):
            extra["retry_after_seconds"] = err["retry_after_seconds"]
        return _error(known, searched_as=searched_as, **extra)
    if data.get("state") == "pending":
        return {"ok": True, "state": "pending", "searched_as": searched_as,
                "retry_after_seconds": data.get("retry_after_seconds") or 3,
                "stage": data.get("stage"), "page": page}
    items = [_group(i) for i in (data.get("items") or []) if isinstance(i, dict)]
    return {"ok": True, "state": "ready", "searched_as": searched_as, "page": data.get("page") or page,
            "items": items, "total": data.get("total", len(items)),
            "matching_landings": data.get("matching_landings", data.get("total", len(items))),
            "has_more": bool(data.get("has_more")),
            "snapshot_id": data.get("snapshot_id"), "complete": data.get("complete", True) is not False,
            "unclassified_count": data.get("unclassified_count") or 0}


def find_stocks(query: str) -> dict:
    """Подсказки ассета (сток в JCloudFront) по началу названия: [{id, name}]."""
    q = (query or "").strip()
    if len(q) < 2:
        return {"ok": True, "stocks": []}
    if not configured():
        return _error("not_configured")
    try:
        data = _call("find_stocks", {"query": q})
    except ImportError:
        return _error("unavailable")
    except Exception as exc:  # noqa: BLE001
        return _error(_classify_exception(exc))
    stocks = [{"id": s.get("id"), "name": s.get("name")} for s in (data.get("stocks") or [])
              if isinstance(s, dict) and s.get("id")]
    return {"ok": True, "stocks": stocks[:10], "matches": data.get("matches", len(stocks))}


def search(query: str, *, page: int = 1, snapshot_id: str | None = None, mode: str | None = None,
           stock_id: int | None = None) -> dict:
    """Сгруппированный поиск лендингов (одна карточка на конфигурацию).

    `mode` оставлен для старых вызовов и не нужен: способ поиска определяется видом строки.
    `stock_id` — фильтр по ассету. Без текста поиск идёт только по ассету.
    """
    kind, q = classify_query(query)
    if not q and not stock_id:
        return _error("invalid_query")
    if not configured():
        return _error("not_configured")
    page = max(1, int(page or 1))

    def call(args: dict, searched_as: str, *, grouped: bool = True) -> dict:
        args = dict(args)
        if grouped:
            args.update(group_by="configuration", page=page, page_size=PAGE_SIZE)
            if snapshot_id:
                args["snapshot_id"] = snapshot_id
            if stock_id:
                args["stock_ids"] = [int(stock_id)]
        else:
            args.update(page=1, page_size=1)
        try:
            return _shape(_call("search_landing_catalog", args), searched_as=searched_as, page=page)
        except ImportError:
            return _error("unavailable")
        except Exception as exc:  # noqa: BLE001 — любое падение транспорта сводим к понятной причине
            return _error(_classify_exception(exc))

    if not q:
        return call({}, "asset")
    if kind == "url":
        # Сначала сам лендинг, затем варианты его шаблона по всем доменам.
        found = call({"query": q, "kind": "url"}, "url", grouped=False)
        if found.get("state") != "ready" or not found.get("ok"):
            return found
        first_id = (found["items"][0] or {}).get("id") if found["items"] else None
        if not str(first_id or "").isdigit():
            # Адрес не нашёлся или пришёл без id - вариантов шаблона искать не по чему.
            return {**found, "items": [], "total": 0, "matching_landings": 0, "page": page}
        return call({"source_landing_id": int(first_id)}, "url")
    if kind == "path":
        return call({"query": q, "kind": "path"}, "path")
    return call({"query": q, "kind": "variant"}, "variant")
