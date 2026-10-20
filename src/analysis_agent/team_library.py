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
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Protocol

API = "https://api.github.com"

#: ACL-2038: репозиторий и ветка по умолчанию. Обновление несёт всё, кроме токена;
#: настройки машины (`ACL_LIBRARY_REPO` / `ACL_LIBRARY_BRANCH`) главнее.
DEFAULT_REPO = "MojoTheApe/acl-lib"
DEFAULT_BRANCH = "main"

#: kind → файл в репозитории и ключ записей внутри него.
FILES = {"targets": ("targets.json", "targets"), "ad_texts": ("ad_texts.json", "texts")}

#: Сколько раз повторить запись, если файл поменяли между чтением и записью.
_CONFLICT_RETRIES = 5
#: Чаще этого копию для чтения не перечитываем — публикация спрашивает
#: библиотеку на каждое объявление.
PULL_EVERY_SECONDS = 15.0


class LibraryError(RuntimeError):
    """Общую библиотеку не удалось прочитать или записать."""


class WriteUncertain(LibraryError):
    """The server may have committed; a retry must not be presented as safe."""


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
            if method == "PUT":
                raise WriteUncertain("The save response was lost. GitHub may have saved the change; "
                                     "refresh the library before retrying.") from exc
            raise LibraryError("GitHub is unreachable. Try again when the connection is restored.") from exc

    def _fail(self, status: int, raw: bytes, what: str) -> LibraryError:
        try:
            msg = json.loads(raw or b"{}").get("message", "")
        except (ValueError, AttributeError):
            msg = ""
        if status in (401, 403):
            hint = "the library token is missing access or has expired"
        elif status == 404:
            hint = f"repository {self.repo} was not found or the token cannot see it"
        else:
            hint = str(msg or f"HTTP {status}").replace(self.token, "[redacted]") if self.token else f"HTTP {status}"
        return LibraryError(f"Could not {what} the team library: {hint}.")

    def check(self) -> None:
        """Одно чтение репозитория библиотеки: токен рабочий и может писать (ACL-2038)."""
        status, raw = self._call("GET", f"{API}/repos/{self.repo}")
        if status != 200:
            raise self._fail(status, raw, "read")
        try:
            can_write = bool(json.loads(raw).get("permissions", {}).get("push"))
        except (ValueError, AttributeError):
            can_write = False
        if not can_write:
            raise LibraryError("The token can read the team library but cannot change it. "
                               "Ask for a token with Contents: Read and write on this repository.")

    def get(self, path: str) -> Remote:
        status, raw = self._call("GET", f"{API}/repos/{self.repo}/contents/{path}?ref={quote(self.branch, safe='')}")
        if status == 404:
            # Файла нет, или нет репозитория/доступа. Различаем по репозиторию.
            s2, raw2 = self._call("GET", f"{API}/repos/{self.repo}")
            if s2 != 200:
                raise self._fail(s2, raw2, "read")
            return Remote(None, None)
        if status != 200:
            raise self._fail(status, raw, "read")
        try:
            meta = json.loads(raw)
            sha = meta["sha"]
            if not isinstance(sha, str) or not sha or meta.get("type", "file") != "file":
                raise ValueError("expected a file")
            if meta.get("encoding") == "base64":
                data = base64.b64decode(meta.get("content", ""))
            else:  # Contents API omits content above 1 MiB.
                s2, data = self._call("GET", f"{API}/repos/{self.repo}/git/blobs/{sha}",
                                      accept="application/vnd.github.raw+json")
                if s2 != 200:
                    raise self._fail(s2, data, "read")
            return Remote(data.decode("utf-8"), sha)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise LibraryError("GitHub returned an unreadable library file response; the local copy was kept.") from exc

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
        if status >= 500:
            raise WriteUncertain("GitHub could not confirm the save. Refresh the library before retrying.")
        if status not in (200, 201):
            raise self._fail(status, raw, "save to")
        try:
            return json.loads(raw)["content"]["sha"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WriteUncertain("GitHub accepted the save but its confirmation was unreadable. "
                                 "Refresh the library before retrying.") from exc


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
    if not repo and not token:
        return None
    if not token:
        # Репозиторий без токена — «не подключено» (ACL-2038): выход — кнопка Connect,
        # а не правка .env руками. Правки остаются местными, как до подключения.
        return None
    # Без репозитория в настройках машины берётся общий (ACL-2038): токен — то,
    # что нельзя положить в сборку.
    return GitHubBackend(repo or DEFAULT_REPO, token,
                         (os.environ.get("ACL_LIBRARY_BRANCH") or DEFAULT_BRANCH).strip())


def configured() -> bool:
    # ACL-2038: подключение — это токен; репозиторий по умолчанию есть всегда.
    return _backend_override is not None or bool((os.environ.get("ACL_LIBRARY_TOKEN") or "").strip())


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
        validate(kind, data)
        return data
    except Exception as exc:
        raise LibraryError(f"The team library file {FILES[kind][0]} is damaged: {exc}. "
                           "Nothing was changed; restore it from the repository history.") from exc


def validate(kind: str, data: dict) -> None:
    """Reject the complete snapshot if any record is invalid; never drop rows."""
    if kind == "targets":
        from src.analysis_agent.targeting_registry import Target as Record
        required = ("interests",)
    else:
        from src.analysis_agent.ad_text_registry import AdText as Record
        required = ("headline", "body")
    key = FILES[kind][1]

    def record(name, raw):
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("invalid record")
        item = Record(**{k: v for k, v in raw.items() if k in Record.__dataclass_fields__})
        if any(not isinstance(getattr(item, k), str) for k in Record.__dataclass_fields__):
            raise ValueError("invalid record fields")
        if name != item.name or not name.strip() or not any(getattr(item, k).strip() for k in required):
            raise ValueError("empty or mismatched record")

    for name, raw in data[key].items():
        record(name, raw)
    for name, rec in data["deleted"].items():
        if not isinstance(rec, dict) or not all(isinstance(rec.get(k), str) for k in ("deleted_by", "deleted_at")):
            raise ValueError("invalid deleted record")
        record(name, rec.get("data"))
        if name in data[key]:
            raise ValueError("record is both active and deleted")
    renamed = data.get("renamed", {})
    if not isinstance(renamed, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                                            for k, v in renamed.items()):
        raise ValueError("invalid renamed section")


def preserve_local_copy(path: Path) -> None:
    """Keep the pre-migration library before replacing it with shared data."""
    if not path.exists():
        return
    backup = path.with_name(path.stem + ".pre-team-library.json")
    if backup.exists():
        return
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=backup.name, suffix=".tmp", delete=False) as out:
            tmp = Path(out.name)
            out.write(path.read_bytes())
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(tmp, backup)  # Atomic create without replacing an earlier backup.
        except FileExistsError:
            pass
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def fetch(kind: str) -> dict:
    b = backend()
    if b is None:
        raise LibraryError("The team library is not connected on this machine.")
    remote = b.get(FILES[kind][0])
    if remote.text is None:
        raise LibraryError(f"The team library file {FILES[kind][0]} is missing. "
                           "Seed the library before connecting dashboards; the local copy was kept.")
    return _parse(kind, remote)


def transact(kind: str, change: Callable[[dict], object], message: str, *,
             allow_create: bool = False) -> tuple[dict, object]:
    with _sync_transaction(kind):
        return _transact(kind, change, message, allow_create=allow_create)


def _transact(kind: str, change: Callable[[dict], object], message: str, *,
              allow_create: bool) -> tuple[dict, object]:
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
        if remote.text is None and not allow_create:
            raise LibraryError(f"The team library file {path} is missing. Seed it before editing.")
        data = _parse(kind, remote)
        result = change(data)
        validate(kind, data)
        text = _dump(data)
        if remote.text is not None and text == remote.text:
            _remember(kind, data)
            return data, result
        try:
            b.put(path, text, remote.sha, f"{message} — {author()}")
        except Conflict:
            continue
        except WriteUncertain:
            # One read is safe; repeating the mutation after an uncertain PUT
            # could duplicate work or overwrite a later manager's correction.
            try:
                confirmed = b.get(path)
                matches = confirmed.text is not None and _dump(_parse(kind, confirmed)) == text
            except Exception:
                matches = False
            if not matches:
                raise WriteUncertain("Could not confirm whether GitHub saved the change. "
                                     "Refresh the library before retrying.") from None
        try:
            _remember(kind, data)
        except Exception:
            # The remote commit is already confirmed. Never claim 'nothing
            # saved' merely because the local read copy could not be updated.
            _pulled.pop(kind, None)
            _status[kind] = {"connected": True, "ok": False,
                             "error": "Saved to the team library, but the local copy could not be updated. Refresh it.",
                             "synced_at": _status.get(kind, {}).get("synced_at", "")}
        return data, result
    raise LibraryError("The team library kept changing while saving. Try again in a moment.")


# ── копия для чтения на машине ────────────────────────────────────────────

_pulled: dict[str, float] = {}
_pull_lock = threading.RLock()
_status: dict[str, dict] = {}
_listeners: dict[str, Callable[[dict], None]] = {}
_sync_readers: dict[str, Callable[[], str]] = {}


@contextmanager
def _sync_transaction(kind: str):
    # Serialize GET -> local apply with writes, including a second dashboard
    # process. Registry locks are distinct and acquired only inside this lock.
    # ACL-1512: the shared machine lock (ACL-1495) — bounded wait with EDEADLK
    # retried instead of LK_LOCK's 10 x 1 s, and no first-write race on the file.
    from config import paths
    with _pull_lock:
        path = Path(paths.get_local_config_dir()) / f"team_library_{kind}.lock"
        with paths.machine_file_lock(path):
            yield


def on_update(kind: str, apply: Callable[[dict], None], read_sync: Callable[[], str]) -> None:
    """Реестр подписывается: свежие данные библиотеки кладутся в его копию."""
    _listeners[kind] = apply
    _sync_readers[kind] = read_sync


def _remember(kind: str, data: dict) -> None:
    stamp = now()
    apply = _listeners.get(kind)
    if apply is not None:
        local = copy.deepcopy(data)
        local["library_synced_at"] = stamp
        apply(local)
    _pulled[kind] = time.monotonic()
    _status[kind] = {"connected": True, "ok": True, "error": "", "synced_at": stamp}


def pull(kind: str, *, force: bool = False) -> dict:
    """Обновить копию на машине. Ошибка связи не бросается — она в статусе."""
    if not configured():
        return status(kind)
    with _sync_transaction(kind):
        fresh = time.monotonic() - _pulled.get(kind, -1e9) < PULL_EVERY_SECONDS
        if force or not fresh:
            try:
                _remember(kind, fetch(kind))
            except (LibraryError, OSError) as exc:
                # A failed refresh is also an attempt. Otherwise each ad waits
                # for another network timeout instead of using the read copy.
                _pulled[kind] = time.monotonic()
                was = status(kind)
                _status[kind] = {"connected": True, "ok": False, "error": str(exc),
                                 "synced_at": was.get("synced_at", "")}
    return status(kind)


def status(kind: str) -> dict:
    if not configured():
        return {"connected": False, "ok": True, "error": "", "synced_at": ""}
    if kind in _status:
        return dict(_status[kind])
    try:
        stamp = _sync_readers[kind]()
    except Exception:
        stamp = ""
    return {"connected": True, "ok": False, "error": "The team library has not been refreshed in this session.",
            "synced_at": stamp if isinstance(stamp, str) else ""}


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
