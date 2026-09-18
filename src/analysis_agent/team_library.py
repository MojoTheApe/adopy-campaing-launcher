"""Общая библиотека таргетов и текстов — в приватном репозитории GitHub.

Раньше то же делала вкладка общей Google-таблицы: добавил один — видят все.
Таблица закрылась политикой Workspace, библиотека разъехалась по машинам
(ACL-331, ACL-335). Решение владельца 18.09 (ACL-1241): главная копия живёт в
отдельном приватном репозитории, в нём только данные, кода нет.

Как устроено:

* Два файла в корне репозитория — ``targets.json`` и ``ad_texts.json``. Формат
  тот же, что у реестров на машине, только вместо списка скрытых имён —
  ``deleted``: кто, когда и что удалил. Отсюда «Recently deleted» и Restore.
* Любая правка идёт сразу в репозиторий: прочитать свежий файл, применить
  правку, записать с sha прочитанной версии. Если за это время файл поменял
  кто-то другой, GitHub откажет, и правка повторится на новой версии — с
  повторной проверкой повторов. Нет связи — ошибка, и ничего не сохранено:
  «сохранено только у меня» не бывает.
* На машине лежит копия для чтения. Её обновляет каждое открытие раздела и
  подготовка публикации; если GitHub моргнул посреди публикации, робот
  работает по копии и говорит, от какого она времени.

Не настроено (нет ACL_LIBRARY_REPO / ACL_LIBRARY_TOKEN) — реестры работают
на машине, как до ACL-1241. Так живут тесты и машины до выдачи токена.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

API = "https://api.github.com"

#: kind → файл в репозитории и ключ записей внутри него.
FILES = {"targets": ("targets.json", "targets"), "ad_texts": ("ad_texts.json", "texts")}

#: Сколько раз повторить запись, если файл поменяли между чтением и записью.
_CONFLICT_RETRIES = 5
#: Чаще этого копию для чтения не перечитываем — публикация спрашивает
#: библиотеку на каждое объявление.
PULL_EVERY_SECONDS = 15.0


class LibraryError(RuntimeError):
    """Общую библиотеку не удалось прочитать или записать."""


class Conflict(Exception):
    """Файл изменился после чтения: sha не совпал."""


@dataclass
class Remote:
    text: str | None  # None — файла ещё нет
    sha: str | None


class Backend(Protocol):
    def get(self, path: str) -> Remote: ...
    def put(self, path: str, text: str, sha: str | None, message: str) -> str: ...


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC"


def author() -> str:
    """Кто правит. До входа через Google (ACL-1242) — выбранный на дешборде CM."""
    try:
        from src.web.buyer import get_selected_cm
        name = get_selected_cm()
    except Exception:
        name = ""
    return name or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def empty(kind: str) -> dict:
    return {"version": 1, FILES[kind][1]: {}, "deleted": {}}


# ── GitHub ────────────────────────────────────────────────────────────────

class GitHubBackend:
    """Contents API. Токен — только на репозиторий библиотеки, в логи не попадает."""

    def __init__(self, repo: str, token: str, branch: str = "main", timeout: float = 15.0):
        self.repo, self.token, self.branch, self.timeout = repo, token, branch, timeout

    def _call(self, method: str, url: str, body: dict | None = None, accept: str = "application/vnd.github+json"):
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "acl-dashboard")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LibraryError(f"GitHub is unreachable: {getattr(exc, 'reason', exc)}") from exc

    def _fail(self, status: int, raw: bytes, what: str) -> LibraryError:
        try:
            msg = json.loads(raw or b"{}").get("message", "")
        except ValueError:
            msg = ""
        if status in (401, 403):
            hint = "the library token is missing access or has expired"
        elif status == 404:
            hint = f"repository {self.repo} was not found or the token cannot see it"
        else:
            hint = msg or f"HTTP {status}"
        return LibraryError(f"Could not {what} the team library: {hint}.")

    def get(self, path: str) -> Remote:
        status, raw = self._call("GET", f"{API}/repos/{self.repo}/contents/{path}?ref={self.branch}")
        if status == 404:
            # Файла нет, или нет репозитория/доступа. Различаем по репозиторию.
            s2, raw2 = self._call("GET", f"{API}/repos/{self.repo}")
            if s2 != 200:
                raise self._fail(s2, raw2, "read")
            return Remote(None, None)
        if status != 200:
            raise self._fail(status, raw, "read")
        meta = json.loads(raw)
        if meta.get("content") and meta.get("encoding") == "base64":
            data = base64.b64decode(meta["content"])
        else:  # больше мегабайта Contents API содержимое не отдаёт — берём блоб
            s2, data = self._call("GET", f"{API}/repos/{self.repo}/git/blobs/{meta['sha']}",
                                  accept="application/vnd.github.raw+json")
            if s2 != 200:
                raise self._fail(s2, data, "read")
        return Remote(data.decode("utf-8"), meta["sha"])

    def put(self, path: str, text: str, sha: str | None, message: str) -> str:
        body = {"message": message, "branch": self.branch,
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        if sha:
            body["sha"] = sha
        status, raw = self._call("PUT", f"{API}/repos/{self.repo}/contents/{path}", body)
        if status in (409, 422) and b"sha" in (raw or b""):
            raise Conflict()
        if status == 409:
            raise Conflict()
        if status not in (200, 201):
            raise self._fail(status, raw, "save to")
        return json.loads(raw)["content"]["sha"]


# ── настройка ─────────────────────────────────────────────────────────────

_backend_override: Backend | None = None


def use_backend(backend: Backend | None) -> None:
    """Подменить хранилище (тесты) или вернуть настройку из окружения (None)."""
    global _backend_override
    _backend_override = backend
    _pulled.clear()


def backend() -> Backend | None:
    if _backend_override is not None:
        return _backend_override
    repo = (os.environ.get("ACL_LIBRARY_REPO") or "").strip()
    token = (os.environ.get("ACL_LIBRARY_TOKEN") or "").strip()
    if not repo or not token:
        return None
    return GitHubBackend(repo, token, (os.environ.get("ACL_LIBRARY_BRANCH") or "main").strip())


def configured() -> bool:
    return backend() is not None


# ── чтение и запись ───────────────────────────────────────────────────────

def _parse(kind: str, remote: Remote) -> dict:
    if remote.text is None:
        return empty(kind)
    try:
        data = json.loads(remote.text)
        key = FILES[kind][1]
        if data.get("version") != 1 or not isinstance(data.get(key), dict):
            raise ValueError("unsupported format")
        if not isinstance(data.get("deleted", {}), dict):
            raise ValueError("invalid deleted section")
        data.setdefault("deleted", {})
        return data
    except Exception as exc:
        raise LibraryError(f"The team library file {FILES[kind][0]} is damaged: {exc}. "
                           "Nothing was changed; restore it from the repository history.") from exc


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def fetch(kind: str) -> dict:
    b = backend()
    if b is None:
        raise LibraryError("The team library is not connected on this machine.")
    return _parse(kind, b.get(FILES[kind][0]))


def transact(kind: str, change: Callable[[dict], object], message: str) -> tuple[dict, object]:
    """Прочитать свежий файл, применить правку, записать; при гонке — заново.

    ``change`` правит данные на месте и возвращает результат для вызывающего;
    исключение из него (повтор, пустое поле) останавливает всё до записи.
    """
    b = backend()
    if b is None:
        raise LibraryError("The team library is not connected on this machine.")
    path = FILES[kind][0]
    for _ in range(_CONFLICT_RETRIES):
        remote = b.get(path)
        data = _parse(kind, remote)
        result = change(data)
        text = _dump(data)
        if remote.text is not None and text == remote.text:
            _remember(kind, data)
            return data, result
        try:
            b.put(path, text, remote.sha, f"{message} — {author()}")
        except Conflict:
            continue
        _remember(kind, data)
        return data, result
    raise LibraryError("The team library kept changing while saving. Try again in a moment.")


# ── копия для чтения на машине ────────────────────────────────────────────

_pulled: dict[str, float] = {}
_pull_lock = threading.Lock()
_status: dict[str, dict] = {}
_listeners: dict[str, Callable[[dict], None]] = {}


def on_update(kind: str, apply: Callable[[dict], None]) -> None:
    """Реестр подписывается: свежие данные библиотеки кладутся в его копию."""
    _listeners[kind] = apply


def _remember(kind: str, data: dict) -> None:
    apply = _listeners.get(kind)
    if apply is not None:
        apply(copy.deepcopy(data))
    _pulled[kind] = time.monotonic()
    _status[kind] = {"connected": True, "ok": True, "error": "", "synced_at": now()}


def pull(kind: str, *, force: bool = False) -> dict:
    """Обновить копию на машине. Ошибка связи не бросается — она в статусе."""
    if not configured():
        return status(kind)
    with _pull_lock:
        fresh = time.monotonic() - _pulled.get(kind, -1e9) < PULL_EVERY_SECONDS
        if force or not fresh:
            try:
                _remember(kind, fetch(kind))
            except LibraryError as exc:
                was = _status.get(kind, {})
                _status[kind] = {"connected": True, "ok": False, "error": str(exc),
                                 "synced_at": was.get("synced_at", "")}
    return status(kind)


def status(kind: str) -> dict:
    if not configured():
        return {"connected": False, "ok": True, "error": "", "synced_at": ""}
    return dict(_status.get(kind) or {"connected": True, "ok": True, "error": "", "synced_at": ""})


def recently_deleted(data: dict, limit: int = 100) -> list[dict]:
    rows = [{"name": name, **{k: v for k, v in rec.items() if k != "data"}, "item": rec.get("data", {})}
            for name, rec in data.get("deleted", {}).items()]
    rows.sort(key=lambda r: r.get("deleted_at", ""), reverse=True)
    return rows[:limit]


class MemoryBackend:
    """Хранилище в памяти: для тестов и для проверки без репозитория."""

    def __init__(self):
        self.files: dict[str, tuple[str, str]] = {}
        self.commits: list[str] = []
        self._n = 0
        self.lock = threading.Lock()

    def get(self, path: str) -> Remote:
        with self.lock:
            text, sha = self.files.get(path, (None, None))
            return Remote(text, sha)

    def put(self, path: str, text: str, sha: str | None, message: str) -> str:
        with self.lock:
            current = self.files.get(path, (None, None))[1]
            if current != sha:
                raise Conflict()
            self._n += 1
            new = f"sha{self._n}"
            self.files[path] = (text, new)
            self.commits.append(message)
            return new
