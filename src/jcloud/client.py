"""Поиск лендингов в JCloudFront по MCP (Streamable HTTP), только чтение (ACL-2852).

Токен читается из окружения машины (`JCLOUD_MCP_TOKEN`, его кладёт `load_env`) и
уходит только в заголовок `Authorization` запроса к MCP. В ответы, сообщения об
ошибках и логи он не попадает: любое сообщение проходит через `_redact`.

Что вводит человек, определяется по виду строки:
  https://…   -> kind=url
  /путь       -> kind=path
  Variant     -> название лендинга из статистики. На песочнице `title` лендинга
                 совпадает с путём без «/», поэтому сначала ищем как путь
                 `/<Variant>`, а если там пусто — как часть названия шаблона.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import re
import threading
from typing import Any

PAGE_SIZE = 20
TIMEOUT_S = 25.0
#: Поля карточки, которые отдаём в браузер. Полный ответ MCP наружу не уходит.
ITEM_FIELDS = ("id", "title", "url", "domain", "path", "template", "language", "status", "page_type")

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


#: Сколько сессия MCP живёт без вызовов. Дольше не держим: сервер мог её закрыть.
IDLE_S = 120.0


@contextlib.asynccontextmanager
async def _open_session(url: str, token: str):
    """Одно соединение с MCP: исходит из httpx, нужен только заголовок Authorization."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=TIMEOUT_S) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _invoke(session, tool: str, args: dict) -> dict:
    response = await session.call_tool(tool, args)
    data = getattr(response, "structuredContent", None)
    if getattr(response, "isError", False) or not isinstance(data, dict):
        raise RuntimeError("JCloudFront returned an invalid response")
    return data


class _Worker:
    """Фоновый поток держит одну открытую сессию MCP и обслуживает вызовы по очереди (ACL-2881).

    Сессию открывает и закрывает одна и та же задача: так требует anyio. Устаревшая сессия
    переоткрывается, вызов повторяется один раз; после IDLE_S простоя или смены адреса/токена
    следующий вызов открывает новую.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None

    def _ensure(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            ready = threading.Event()

            def run() -> None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                self._queue = asyncio.Queue()
                ready.set()
                try:
                    loop.run_until_complete(self._serve())
                finally:
                    loop.close()

            self._thread = threading.Thread(target=run, name="jcloud-mcp", daemon=True)
            self._thread.start()
            ready.wait(5)

    async def _serve(self) -> None:
        pending = None  # (tool, args, future, retries, settings)
        while True:
            if pending is None:
                pending = await self._queue.get()
                if pending is None:
                    return
            settings = pending[4]
            opened = False
            try:
                async with _open_session(*settings) as session:
                    opened = True
                    while True:
                        if pending is None:
                            try:
                                pending = await asyncio.wait_for(self._queue.get(), IDLE_S)
                            except asyncio.TimeoutError:
                                break  # простой: закрываем, следующий вызов откроет заново
                            if pending is None:
                                return
                        tool, args, fut, retries, wanted = pending
                        if wanted != settings:
                            break  # адрес или токен сменились: переоткрываем, вызов не теряем
                        try:
                            data = await asyncio.wait_for(_invoke(session, tool, args), TIMEOUT_S + 5)
                        except Exception as exc:  # noqa: BLE001
                            if retries < 1:
                                pending = (tool, args, fut, retries + 1, wanted)  # устаревшая сессия
                            else:
                                fut.set_exception(exc)
                                pending = None
                            break
                        fut.set_result(data)
                        pending = None
            except Exception as exc:  # noqa: BLE001 — не открылось соединение
                # Сбой при закрытии уже начатой сессии не трогает вызов: он повторится или уже завершён.
                if pending is not None and not opened:
                    pending[2].set_exception(exc)
                    pending = None

    def call(self, tool: str, args: dict) -> dict:
        self._ensure()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (tool, args, fut, 0, _settings()))
        return fut.result(timeout=TIMEOUT_S * 2 + 10)

    def stop(self) -> None:
        if self._thread and self._thread.is_alive() and self._loop:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._thread.join(5)
        self._thread = None


_worker = _Worker()


def _call(tool: str, args: dict) -> dict:
    return _worker.call(tool, args)


def _item(raw: dict) -> dict:
    return {k: raw.get(k) for k in ITEM_FIELDS}


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
    items = [_item(i) for i in (data.get("items") or []) if isinstance(i, dict)]
    return {"ok": True, "state": "ready", "searched_as": searched_as, "page": data.get("page") or page,
            "items": items, "total": data.get("total", len(items)), "has_more": bool(data.get("has_more")),
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
    """Поиск лендингов. `mode` (path|template|url) повторяет уже выбранный способ при листании и ожидании.

    `stock_id` — фильтр по ассету. Без текста поиск идёт только по ассету.
    """
    kind, q = classify_query(query)
    if not q and not stock_id:
        return _error("invalid_query")
    if not configured():
        return _error("not_configured")
    page = max(1, int(page or 1))

    def run(args: dict, searched_as: str) -> dict:
        args = {**args, "page": page, "page_size": PAGE_SIZE}
        if stock_id:
            args["stock_ids"] = [int(stock_id)]
        if snapshot_id:
            args["snapshot_id"] = snapshot_id
        try:
            return _shape(_call("search_landing_catalog", args), searched_as=searched_as, page=page)
        except ImportError:
            return _error("unavailable")
        except Exception as exc:  # noqa: BLE001 — любое падение транспорта сводим к понятной причине
            return _error(_classify_exception(exc))

    if not q:
        return run({}, "asset")
    if kind == "url":
        return run({"query": q, "kind": "url"}, "url")
    if kind == "path":
        return run({"query": q, "kind": "path"}, "path")
    # Variant: сначала как путь /<Variant>, затем как часть названия шаблона.
    if mode == "template":
        return run({"query": q, "kind": "template", "template_match": "contains"}, "template")
    result = run({"query": "/" + q, "kind": "path"}, "path")
    # `mode == "path"` — повтор после `pending`: первый ответ был «готовится», шаблон ещё не пробовали.
    if mode in (None, "path") and result.get("ok") and result.get("state") == "ready" and not result.get("total"):
        return run({"query": q, "kind": "template", "template_match": "contains"}, "template")
    return result
