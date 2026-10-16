"""Пределы запросов дешборда и безопасная раздача /static (ACL-1356, аудит S04).

Дешборд слушает только loopback, но чужая вкладка браузера или локальная
программа всё равно могут прислать ему что угодно. Три дыры аудита S04:

* **Тело без предела.** Starlette читает тело целиком, а старый разборщик форм
  не ограничивал ни число полей, ни размер поля (CVE-2024-47874,
  CVE-2025-54121, CVE-2026-54283). Здесь предел проверяется ДО чтения:
  заявленный ``Content-Length`` сверх предела получает 413 сразу, а тело без
  длины (chunked) считается по мере прихода и обрывается на первом лишнем
  байте — в память целиком оно не попадает.
* **Формы без пределов.** Каждый разбор формы в ``app.py`` идёт через
  :func:`read_form` с явными ``max_fields``/``max_files``/``max_part_size``.
  Файлы дешборд по HTTP не принимает вовсе (креатив выбирается по имени из
  папки креативов на диске), поэтому ``max_files=0``.
* **UNC и абсолютные пути в /static.** На Windows путь ``\\\\host\\share``
  заставляет систему саму пойти на чужой SMB-сервер и предъявить ему NTLM-хэш
  (CVE-2026-48818). :class:`SafeStaticFiles` отбрасывает всё, кроме простых
  относительных имён, ещё до того, как путь попадёт в файловую систему.
"""
from __future__ import annotations

import ntpath
import posixpath
import re
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import FormData, Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

KiB = 1024
MiB = 1024 * KiB

#: Обычный запрос дешборда — JSON или короткая форма в несколько килобайт.
#: Самый длинный из них — список скрытых аккаунтов (до тысячи имён), это
#: десятки килобайт. Мегабайт — запас на порядок.
DEFAULT_MAX_BODY = 1 * MiB

#: Форма билдера кампаний: все слоты страны разом — кампании, объявления,
#: тексты. Большой билд (десятки кампаний по десятку объявлений с длинными
#: текстами) весит единицы мегабайт; 16 МиБ — с запасом, но не «без предела».
BUILDER_MAX_BODY = 16 * MiB

#: Маршруты, которые принимают форму билдера целиком.
BUILDER_ROUTES = frozenset({
    "/build-upload",
    "/preview-publish",
    "/publish-jobs",
    "/publish-plan",      # ACL-1760: проверка плана через Graph API
    "/api/page-forms",    # формы страницы под слоты билдера
})

#: Предел тела по маршруту; всё, чего здесь нет, живёт с DEFAULT_MAX_BODY.
ROUTE_MAX_BODY: dict[str, int] = {path: BUILDER_MAX_BODY for path in BUILDER_ROUTES}


@dataclass(frozen=True)
class FormLimits:
    max_fields: int
    max_files: int
    max_part_size: int


#: Короткие формы (аккаунт, период, профиль, текст жалобы до 4000 символов).
SMALL_FORM = FormLimits(max_fields=50, max_files=0, max_part_size=64 * KiB)

#: Форма билдера: на слот кампании ~десяток полей и ~пять на объявление.
#: Двадцать тысяч полей — это сотни кампаний; настоящий билд много меньше.
BUILDER_FORM = FormLimits(max_fields=20_000, max_files=0, max_part_size=1 * MiB)


async def read_form(request: Request, limits: FormLimits = SMALL_FORM) -> FormData:
    """Разобрать форму с явными пределами.

    Превышение любого предела Starlette превращает в 400 с понятным текстом
    («Too many fields…», «Too many files…»); тело сверх предела маршрута
    обрывает :class:`RequestBodyLimit` ещё раньше — 413.
    """
    return await request.form(
        max_fields=limits.max_fields,
        max_files=limits.max_files,
        max_part_size=limits.max_part_size,
    )


class RequestBodyTooLarge(HTTPException):
    """Тело запроса оказалось больше предела маршрута."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body is too large.")


def _refusal(status_code: int, message: str) -> JSONResponse:
    # Connection: close — остаток непрочитанного тела не должен быть
    # принят за следующий запрос на том же соединении.
    return JSONResponse({"error": message}, status_code=status_code, headers={"Connection": "close"})


class RequestBodyLimit:
    """ASGI-прослойка: предел тела запроса, проверяемый до буферизации.

    1. Заявленный ``Content-Length`` больше предела → 413, приложение вообще
       не вызывается и ни один байт тела не читается.
    2. Некорректный ``Content-Length`` → 400.
    3. Тело без длины (или клиент прислал больше, чем заявил) считается по
       кускам; первый кусок сверх предела обрывает чтение исключением, а
       ответ приложения, каким бы он ни был (маршрут мог поймать исключение
       и ответить 400), заменяется на 413.
    """

    def __init__(
        self,
        app: ASGIApp,
        default_max_body: int = DEFAULT_MAX_BODY,
        route_max_body: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.default_max_body = default_max_body
        self.route_max_body = dict(route_max_body or {})

    def limit_for(self, path: str) -> int:
        key = path.rstrip("/") or "/"
        return self.route_max_body.get(key, self.default_max_body)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Путь без root_path: за префиксом маршруты билдера должны совпасть.
        limit = self.limit_for(_route_path(scope))
        declared_raw = Headers(scope=scope).get("content-length")
        if declared_raw is not None:
            try:
                declared = int(declared_raw.strip())
                if declared < 0:
                    raise ValueError(declared_raw)
            except ValueError:
                await _refusal(400, "Invalid Content-Length header.")(scope, receive, send)
                return
            if declared > limit:
                await _refusal(413, "Request body is too large.")(scope, receive, send)
                return

        state: dict[str, Any] = {"received": 0, "exceeded": False, "started": False, "replaced": False}

        async def limited_receive() -> Message:
            if state["exceeded"]:
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > limit:
                    state["exceeded"] = True
                    raise RequestBodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            if state["replaced"]:
                # Ответ приложения уже подменён на 413 — остальное глотаем.
                return
            if message["type"] == "http.response.start":
                if state["exceeded"]:
                    state["replaced"] = True
                    state["started"] = True
                    await _refusal(413, "Request body is too large.")(scope, receive, send)
                    return
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except RequestBodyTooLarge:
            if state["started"]:
                raise
            state["replaced"] = True
            await _refusal(413, "Request body is too large.")(scope, receive, send)


# Имя файла в /static: буквы, цифры, точка, дефис, подчёркивание; не с точки
# (значит, ни «.», ни «..», ни скрытых файлов).
_SAFE_STATIC_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\Z")

# Имена устройств Windows (CON, NUL.css, COM1…) — не файлы: realpath/stat на них
# идут к устройству. Хвостовая точка Windows молча отрезает — тоже отказ.
_WINDOWS_DEVICE = re.compile(r"(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(\..*)?\Z", re.I)


def _safe_static_segment(segment: str) -> bool:
    return bool(_SAFE_STATIC_SEGMENT.match(segment)) and not segment.endswith(".") and not _WINDOWS_DEVICE.match(segment)


def _route_path(scope: Scope) -> str:
    """Путь внутри смонтированного приложения (как ``get_route_path`` Starlette)."""
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path) and path[len(root_path) : len(root_path) + 1] in ("", "/"):
        return path[len(root_path) :]
    return path


def is_safe_static_path(path: str) -> bool:
    """Простой относительный путь из безопасных имён, и ничего больше.

    Отказ, если в пути есть обратная косая черта, двоеточие (буква диска,
    альтернативные потоки NTFS), пустой сегмент (``//host`` — UNC), ``..``
    или путь абсолютный по правилам хоть Windows, хоть POSIX.
    """
    rel = path[1:] if path.startswith("/") else path
    if not rel or "\\" in rel or ":" in rel or "\x00" in rel:
        return False
    if ntpath.isabs(rel) or posixpath.isabs(rel) or ntpath.splitdrive(rel)[0]:
        return False
    return all(_safe_static_segment(segment) for segment in rel.split("/"))


class SafeStaticFiles(StaticFiles):
    """StaticFiles, до которого не доходят UNC, буквы дисков и абсолютные пути.

    Проверка стоит в двух местах: по сырому пути запроса — до нормализации,
    которая могла бы превратить ``C:/x`` в «относительный» ``C:x``, — и ещё
    раз в ``lookup_path`` перед обращением к файловой системе.
    ``follow_symlink=True`` запрещён: ссылка из папки static могла бы вести
    куда угодно, в том числе на сетевой ресурс.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("follow_symlink"):
            raise ValueError("SafeStaticFiles never follows symlinks.")
        super().__init__(*args, **kwargs)

    async def get_response(self, path: str, scope: Scope):  # type: ignore[override]
        if not is_safe_static_path(_route_path(scope)):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)

    def lookup_path(self, path: str) -> tuple[str, Any]:
        if not is_safe_static_path(path.replace(ntpath.sep, "/")) or ":" in path or path.startswith(("\\", "/")):
            return "", None
        return super().lookup_path(path)
