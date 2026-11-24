"""ACL-2899: аккаунты байера приходят с хаба, а не живут на машине.

Вход: токен Google (с правами на email) -> POST /v1/session -> ключ юзера.
Ключ лежит в хранилище секретов машины (token_vault), не в репозитории, не в
логах, не в телеметрии. Дальше GET /v1/my-accounts с ETag и слияние строк в
локальную карту аккаунтов.

Синхронизация только по событиям (открыли дашборд, вкладка снова видна):
ни таймеров, ни фонового цикла. Любая ошибка (нет сети, хаб лежит, нет ключа)
оставляет локальную карту как есть и отдаёт тихий статус, страница не падает.

С сервера читаем и только читаем: proxy_* и tracking_link никогда не уходят на хаб.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

#: Поля строки, которые назначает сервер. Остальные колонки карты (proxy_*, tracking_link, buyer…) локальные.
SERVER_FIELDS = ("ad_account_id", "page_id", "page_name", "country", "multilogin_instance", "folder_id", "profile_id")

_KEY_FILE = "user_session.json"
_STATE_FILE = "server_accounts.json"
_lock = threading.Lock()

#: http(method, url, headers, body_bytes|None) -> (status, headers_lowercase, parsed_json|None)
Http = Callable[[str, str, dict, "bytes | None"], tuple]


# -- адрес хаба и транспорт --------------------------------------------------

def hub_url() -> str:
    """ACL_HUB_URL, иначе тот же хаб, что у отправки журнала событий."""
    explicit = (os.environ.get("ACL_HUB_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    from src.acl_event_log.shipper import DEFAULT_URL

    return DEFAULT_URL.rstrip("/")


def _urllib_http(method: str, url: str, headers: dict, body: "bytes | None") -> tuple:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - адрес хаба из настроек
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
        status = exc.code
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        data = None
    return status, hdrs, data


# -- файлы машины ------------------------------------------------------------

def _local(name: str) -> Path:
    from config import paths

    return Path(paths.get_local_config_dir()) / name


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    from config.paths import _atomic_write

    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def load_key() -> str:
    """Ключ юзера из хранилища секретов; пусто, если не входили."""
    try:
        from config import token_vault

        path = _local(_KEY_FILE)
        if not path.is_file():
            return ""
        return str(json.loads(token_vault.read_secret_text(path)).get("key") or "")
    except Exception:  # noqa: BLE001 - битый файл = нет ключа
        return ""


def _save_key(key: str) -> None:
    from config import token_vault

    path = _local(_KEY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    token_vault.write_secret_text(path, json.dumps({"key": key}))


def _forget_key() -> None:
    try:
        _local(_KEY_FILE).unlink()
    except OSError:
        pass


def load_state() -> dict:
    """{'names': [...], 'etag': '...'}: какие аккаунты пришли с сервера и последний ETag."""
    state = _load_json(_local(_STATE_FILE))
    names = state.get("names")
    hidden = state.get("sync_hidden")
    seeded = state.get("seeded_for")
    return {"names": [n for n in names if isinstance(n, str)] if isinstance(names, list) else [],
            "etag": str(state.get("etag") or ""),
            # Скрытые самой синхронизацией: вернуть можно только их, ручные скрытия юзера не трогаем
            "sync_hidden": [n for n in hidden if isinstance(n, str)] if isinstance(hidden, list) else [],
            # Предложение войти заново уже показано на этой машине (один раз, не на каждую вкладку)
            "signin_prompted": bool(state.get("signin_prompted")),
            # ACL-2901: для каких юзеров карта машины уже перенесена на хаб (один раз на юзера)
            "seeded_for": [n for n in seeded if isinstance(n, str)] if isinstance(seeded, list) else []}


def _save_state(names: list[str], etag: str, sync_hidden: list[str] | None = None,
                signin_prompted: bool | None = None, seeded_for: list[str] | None = None) -> None:
    old = load_state()
    _write_json(_local(_STATE_FILE), {
        "names": sorted(set(names), key=str.casefold), "etag": etag,
        "sync_hidden": sorted(set(old["sync_hidden"] if sync_hidden is None else sync_hidden), key=str.casefold),
        "signin_prompted": old["signin_prompted"] if signin_prompted is None else signin_prompted,
        "seeded_for": sorted(set(old["seeded_for"] if seeded_for is None else seeded_for)),
    })


# -- токен Google ------------------------------------------------------------

def token_status() -> str:
    """'ok' | 'old_scopes' | 'none': есть ли у сохранённого токена права на email."""
    try:
        from config import paths, token_vault
        from src.analysis_agent import gsheet_accounts as ga

        info = json.loads(token_vault.read_secret_text(paths.get_google_oauth_token_path()))
    except Exception:  # noqa: BLE001
        return "none"
    if not info.get("refresh_token"):
        return "none"
    return "ok" if ga.token_has_identity_scopes(info) else "old_scopes"


def _google_access_token() -> str:
    """Свежий access-токен из сохранённого входа; пусто, если не получилось. В лог не пишется."""
    try:
        import google.auth.transport.requests
        from google.oauth2.credentials import Credentials

        from config import paths, token_vault
        from src.analysis_agent import gsheet_accounts as ga

        info = json.loads(token_vault.read_secret_text(paths.get_google_oauth_token_path()))
        creds = Credentials.from_authorized_user_info(info, ga.saved_token_scopes(info))
        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())
        return creds.token or ""
    except Exception:  # noqa: BLE001
        return ""


# -- сессия ------------------------------------------------------------------

def open_session(http: Http, token_provider: Callable[[], str]) -> str:
    """POST /v1/session: токен Google -> ключ юзера (сохраняется). Пусто при любой неудаче."""
    google_token = token_provider()
    if not google_token:
        return ""
    body = json.dumps({"google_access_token": google_token}).encode("utf-8")
    try:
        status, _h, data = http("POST", hub_url() + "/v1/session",
                                {"Content-Type": "application/json", "User-Agent": "acl-dashboard"}, body)
    except Exception:  # noqa: BLE001 - нет сети
        log.info("User session: hub is unreachable.")
        return ""
    if status != 200 or not isinstance(data, dict) or not data.get("key"):
        log.info("User session was not opened (hub status %s).", status)
        return ""
    key = str(data["key"])
    try:
        _save_key(key)
    except Exception:  # noqa: BLE001
        log.info("User session key could not be saved.")
        return ""
    return key


# -- слияние -----------------------------------------------------------------

def _name_key(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def merge_into_map(accounts: list[dict], state_names: list[str], *, map_path: Path | None = None,
                   sync_hidden: list[str] | None = None) -> dict:
    """Слить строки сервера в локальную карту и скрыть/вернуть снятые.

    Сервер выигрывает по непустым полям; локальные (proxy_*, tracking_link…) не трогаем.
    Аккаунт, которого нет в списке серверных, а на машине добавлен вручную, не меняется,
    кроме случая, когда сервер сам назначил такое же имя. Возвращает {changed, names, hidden, shown}.
    """
    from config.paths import get_ad_account_map_path, write_csv_atomic
    from src.web import hidden_accounts

    path = map_path or Path(get_ad_account_map_path())
    rows: list[dict] = []
    columns: list[str] = []
    if path.is_file():
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            columns = list(reader.fieldnames or [])
    for col in ("account_name", *SERVER_FIELDS):
        if col not in columns:
            columns.append(col)

    by_name = {}
    for row in rows:
        by_name.setdefault(_name_key(row.get("account_name")), row)
    known = {_name_key(n) for n in state_names}
    names = {_name_key(n): n for n in state_names}
    changed = False
    to_hide: list[str] = []
    to_show: list[str] = []

    for acc in accounts:
        name = str(acc.get("account_name") or "").strip()
        key = _name_key(name)
        if not key:
            continue
        removed = bool(acc.get("removed_at"))
        row = by_name.get(key)
        if removed:
            # Скрываем только то, что пришло с сервера: ручной аккаунт с тем же именем не наш
            if row is not None and key in known:
                to_hide.append(row.get("account_name") or name)
            continue
        if row is None:
            row = {c: "" for c in columns}
            row["account_name"] = name
            rows.append(row)
            by_name[key] = row
            changed = True
        for field in SERVER_FIELDS:
            val = str(acc.get(field) or "").strip()
            # Пустое серверное поле (в т.ч. profile_id) локальное значение не стирает
            if val and (row.get(field) or "") != val:
                row[field] = val
                changed = True
        names[key] = row.get("account_name") or name
        to_show.append(names[key])

    if changed:
        write_csv_atomic(path, columns, rows)
    hidden_now = hidden_accounts.load()
    # Ревью ACL-2899: возвращаем только то, что скрыла сама синхронизация. Аккаунт, который
    # юзер скрыл руками, остаётся скрытым, даже если он назначен на сервере.
    ours = {_name_key(n): n for n in (sync_hidden or [])}
    hidden_done = []
    for label in to_hide:
        if hidden_accounts._key(label) not in hidden_now:
            hidden_accounts.set_hidden(label, True)
            hidden_done.append(label)
            ours[_name_key(label)] = label
    shown_done = []
    for label in to_show:
        if _name_key(label) in ours:
            if hidden_accounts._key(label) in hidden_now:
                hidden_accounts.set_hidden(label, False)
                shown_done.append(label)
            ours.pop(_name_key(label), None)
    for label in to_hide:
        names.setdefault(_name_key(label), label)
    return {"changed": changed or bool(hidden_done) or bool(shown_done), "names": list(names.values()),
            "hidden": hidden_done, "shown": shown_done, "sync_hidden": list(ours.values())}


# -- главный вход ------------------------------------------------------------

def sync(http: Http | None = None, token_provider: Callable[[], str] | None = None) -> dict:
    """Одна синхронизация. Не бросает: всегда словарь со status.

    status: 'updated' | 'not_modified' | 'signin_required' | 'not_signed_in' | 'offline' | 'no_session'
    """
    http = http or _urllib_http
    token_provider = token_provider or _google_access_token
    if not _lock.acquire(blocking=False):
        return {"ok": True, "status": "busy", "changed": False}
    try:
        return _sync_locked(http, token_provider)
    except Exception:  # noqa: BLE001 - в страницу ничего не пробрасываем
        log.info("User accounts sync failed.")
        return {"ok": True, "status": "offline", "changed": False}
    finally:
        _lock.release()


def _sync_locked(http: Http, token_provider: Callable[[], str]) -> dict:
    scope = token_status()
    if scope == "old_scopes":
        # Ревью ACL-2899: предложение войти заново — ровно один раз на машину, флаг в server_accounts.json
        state = load_state()
        prompt = not state["signin_prompted"]
        if prompt:
            _save_state(state["names"], state["etag"], signin_prompted=True)
        return {"ok": True, "status": "signin_required", "changed": False, "prompt": prompt,
                "message": "Sign in with Google again to get your accounts."}
    if scope == "none":
        return {"ok": True, "status": "not_signed_in", "changed": False}

    key = load_key() or open_session(http, token_provider)
    if not key:
        return {"ok": True, "status": "no_session", "changed": False}

    state = load_state()
    for attempt in (0, 1):
        try:
            status, hdrs, data = _get_my_accounts(http, key, state["etag"])
        except Exception:  # noqa: BLE001
            log.info("User accounts: hub is unreachable.")
            return {"ok": True, "status": "offline", "changed": False}
        if status == 401 and attempt == 0:
            _forget_key()  # ключ просрочен или юзер отключён: один новый вход и повтор
            key = open_session(http, token_provider)
            if not key:
                return {"ok": True, "status": "no_session", "changed": False}
            continue
        break
    if status == 304:
        return {"ok": True, "status": "not_modified", "changed": False}
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        log.info("User accounts: hub answered %s.", status)
        return {"ok": True, "status": "offline" if status >= 500 or status == 0 else "no_session", "changed": False}

    seeded = 0
    email = _email_of(key)
    if not data["accounts"] and email and email not in state["seeded_for"]:
        # ACL-2901: у юзера на хабе пусто — один раз переносим текущую карту машины.
        outcome = _seed(http, key)
        if outcome is None:
            hdrs = {}  # перенос не удался: без ETag следующая синхронизация не упрётся в 304 и повторит его
        else:
            seeded = outcome
            _save_state(state["names"], state["etag"], seeded_for=[*state["seeded_for"], email])
            try:
                status, hdrs, fresh = _get_my_accounts(http, key, "")
            except Exception:  # noqa: BLE001
                status = 0
            if status == 200 and isinstance(fresh, dict) and isinstance(fresh.get("accounts"), list):
                data = fresh
            else:
                hdrs = {}  # без ETag: следующая синхронизация заберёт свежий список

    result = merge_into_map([a for a in data["accounts"] if isinstance(a, dict)], state["names"],
                            sync_hidden=state["sync_hidden"])
    _save_state(result["names"], str(hdrs.get("etag") or ""), sync_hidden=result["sync_hidden"])
    out = {"ok": True, "status": "updated", "changed": result["changed"],
           "hidden": len(result["hidden"]), "shown": len(result["shown"])}
    if seeded:
        out["seeded"] = seeded
    return out


def _get_my_accounts(http: Http, key: str, etag: str) -> tuple:
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "acl-dashboard"}
    if etag:
        headers["If-None-Match"] = etag
    return http("GET", hub_url() + "/v1/my-accounts", headers, None)


def _email_of(key: str) -> str:
    """Email из ключа юзера <email>.<exp>.<hmac>; пусто, если ключ не такого вида."""
    parts = key.rsplit(".", 2)
    return parts[0].lower() if len(parts) == 3 and "@" in parts[0] else ""


# -- первый перенос карты (ACL-2901) ----------------------------------------

#: Поля строки сида: ровно то, что принимает хаб. proxy_*, tracking_link и прочие локальные колонки не уходят.
SEED_FIELDS = ("account_name", *SERVER_FIELDS)
SEED_MAX_ROWS = 2000


def seed_rows(map_path: Path | None = None) -> list[dict]:
    """Строки локальной карты для сида: без скрытых на этой машине, без дублей имён, только SEED_FIELDS."""
    from config.paths import get_ad_account_map_path
    from src.web import hidden_accounts

    path = map_path or Path(get_ad_account_map_path())
    if not path.is_file():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    hidden = hidden_accounts.load()
    out, seen = [], set()
    for row in rows:
        name = " ".join(str(row.get("account_name") or "").split())
        key = _name_key(name)
        if not key or key in seen or hidden_accounts._key(name) in hidden:
            continue
        seen.add(key)
        item = {"account_name": name}
        for field in SERVER_FIELDS:
            val = str(row.get(field) or "").strip()
            if val:
                item[field] = val
        out.append(item)
    return out[:SEED_MAX_ROWS]


def _seed(http: Http, key: str) -> "int | None":
    """POST /v1/my-accounts/seed. Число перенесённых строк, если перенос состоялся или больше не нужен
    (0 — пустая карта или хаб отказал 409: у юзера уже есть назначения). None — не вышло, повторим позже."""
    rows = seed_rows()
    if not rows:
        return 0
    body = json.dumps({"rows": rows}).encode("utf-8")
    try:
        status, _h, data = http("POST", hub_url() + "/v1/my-accounts/seed",
                                {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                 "User-Agent": "acl-dashboard"}, body)
    except Exception:  # noqa: BLE001
        log.info("User accounts seed: hub is unreachable.")
        return None
    if status == 200:
        n = int((data or {}).get("seeded") or 0) if isinstance(data, dict) else 0
        log.info("User accounts seed: %s accounts moved to the hub.", n)
        return n
    if status == 409:
        return 0
    log.info("User accounts seed: hub answered %s.", status)
    return None
