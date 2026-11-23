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


#: Сколько сессия MCP живёт без вызовов. Дольше не держим: сервер мог её закрыть.
IDLE_S = 120.0
#: Потолки на открытие (рукопожатие) и закрытие сессии; вызов ограничен TIMEOUT_S + 5.
OPEN_S = TIMEOUT_S
CLOSE_S = 5.0
#: Сколько вызывающий ждёт ответ фонового потока; после этого поток считается зависшим и заменяется.
CALL_WAIT_S = TIMEOUT_S * 2 + 10


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


def _finish(fut: concurrent.futures.Future, *, result=None, exc: BaseException | None = None) -> None:
    """Ответ вызывающему; вызывающий мог уже уйти по таймауту, тогда это не ошибка."""
    if fut.done():
        return
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)


class _Worker:
    """Фоновый поток держит одну открытую сессию MCP и обслуживает вызовы по очереди (ACL-2881).

    Сессию открывает и закрывает одна и та же задача: так требует anyio. Устаревшая сессия
    переоткрывается, вызов повторяется один раз; после IDLE_S простоя или смены адреса/токена
    следующий вызов открывает новую. Открытие, рукопожатие и закрытие ограничены по времени;
    если поток всё равно завис, вызывающий по таймауту бросает его и следующий вызов запускает
    новый поток со свежей очередью.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None

    def _ensure(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        with self._lock:
            if not (self._thread and self._thread.is_alive() and self._loop):
                ready = threading.Event()
                box: dict = {}

                def run() -> None:
                    loop = asyncio.new_event_loop()
                    queue: asyncio.Queue = asyncio.Queue()
                    box["pair"] = (loop, queue)
                    ready.set()
                    try:
                        loop.run_until_complete(self._serve(queue))
                    finally:
                        loop.close()

                self._thread = threading.Thread(target=run, name="jcloud-mcp", daemon=True)
                self._thread.start()
                ready.wait(5)
                self._loop, self._queue = box["pair"]
            return self._loop, self._queue

    @staticmethod
    async def _serve(queue: asyncio.Queue) -> None:
        pending = None  # (tool, args, future, retries, settings)
        while True:
            if pending is None:
                pending = await queue.get()
                if pending is None:
                    return
            settings = pending[4]
            opened = False
            stack = contextlib.AsyncExitStack()
            try:
                async with asyncio.timeout(OPEN_S):
                    session = await stack.enter_async_context(_open_session(*settings))
                opened = True
                while True:
                    if pending is None:
                        try:
                            pending = await asyncio.wait_for(queue.get(), IDLE_S)
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
                            _finish(fut, exc=exc)
                            pending = None
                        break
                    _finish(fut, result=data)
                    pending = None
            except Exception as exc:  # noqa: BLE001 — не открылось соединение или рукопожатие
                # Сбой уже начатой сессии не трогает вызов: он повторится или уже завершён.
                if pending is not None and not opened:
                    _finish(pending[2], exc=exc)
                    pending = None
            finally:
                try:
                    async with asyncio.timeout(CLOSE_S):
                        await stack.aclose()
                except BaseException as exc:  # noqa: BLE001 — зависшее/упавшее закрытие не должно держать поток
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise

    def call(self, tool: str, args: dict) -> dict:
        loop, queue = self._ensure()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        loop.call_soon_threadsafe(queue.put_nowait, (tool, args, fut, 0, _settings()))
        try:
            return fut.result(timeout=CALL_WAIT_S)
        except concurrent.futures.TimeoutError:
            self._abandon(loop, queue)
            raise

    def _abandon(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        """Поток завис: следующий вызов получит новый; старый уйдёт, если когда-нибудь отвиснет."""
        with self._lock:
            if self._queue is queue:
                self._thread = self._loop = self._queue = None
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(queue.put_nowait, None)

    def stop(self) -> None:
        with self._lock:
            thread, loop, queue = self._thread, self._loop, self._queue
            self._thread = self._loop = self._queue = None
        if thread and thread.is_alive() and loop and queue:
            loop.call_soon_threadsafe(queue.put_nowait, None)
            thread.join(5)


_worker = _Worker()


def _call(tool: str, args: dict) -> dict:
    return _worker.call(tool, args)


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
