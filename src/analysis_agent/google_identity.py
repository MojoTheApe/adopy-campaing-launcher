"""Вход в дешборд рабочим аккаунтом Google (ACL-1242).

Идентичность — это метка, а не замок: ни один маршрут не закрыт, идущая или
стоящая в очереди задача не затрагивается, недоступный Google не останавливает
работу. Менеджер один раз входит в браузере аккаунтом @adopy.com; сессия
лежит в защищённом хранилище машины, раз в сутки молча перепроверяется, а
подтверждённое имя CM попадает в отчёты о проблемах, Sentry, правки библиотеки
и общий журнал действий.

Отдельный грант (D5): свой файл ``token_identity.json`` со скоупами
``openid email profile``. Токены таблиц (``token_accounts.json``,
``token_telemetry.json``) не трогаются никогда.

Файлы лежат в ``get_local_config_dir()``, то есть у каждого пользователя
Windows свои:

* ``token_identity.json`` — ``Credentials.to_json()`` с refresh-токеном, через
  ``token_vault.write_secret_text`` (DPAPI);
* ``identity.json`` — email, имя, привязанное имя CM, состояние, метки времени.
  Секретов нет; читается на горячих путях (``before_send``), поэтому не
  зависит от DPAPI.

Все внешние части (запуск OAuth, проверка id_token, обновление, часы, список
CM) подменяются в тестах. Сеть из тестов не нужна. Тексты ошибок фиксированные:
тело ответа Google наружу не идёт.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

DOMAIN = "adopy.com"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
PROMPT = "select_account consent"
SIGN_IN_TIMEOUT_S = 180
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

TOKEN_FILE = "token_identity.json"
IDENTITY_FILE = "identity.json"
SCHEMA = 1

RECHECK_AFTER = timedelta(hours=24)
STALE_AFTER = timedelta(days=7)
_CACHE_TTL_S = 2.0

#: Состояния (спека §3.3).
NONE, UNAVAILABLE, DECLINED, VERIFIED, STALE, EXPIRED = (
    "none", "unavailable", "declined", "verified", "stale", "expired")
#: В этих состояниях подтверждённое имя CM уже можно ставить на события.
ATTACHED_STATES = frozenset({VERIFIED, STALE, EXPIRED})

#: Фиксированные тексты (UI английский, тела ответов Google не пересказываем).
MESSAGES = {
    "unavailable": "Google sign-in is not set up in this version. Update ACL, or ask your team lead. You can keep working.",
    "busy": "Waiting for the Google sign-in in your browser...",
    "timeout": "Sign-in was not finished. Try again.",
    "offline": "Could not reach Google. Check the internet connection and try again. You can keep working and sign in later.",
    "wrong_domain": "That is not an Adopy Google account. Sign in with your @adopy.com account.",
    "failed": "Could not sign in. Try again.",
}


class IdentityError(Exception):
    """Ошибка с безопасным для показа текстом."""


@dataclass(frozen=True)
class Identity:
    state: str = NONE
    email: str = ""
    name: str = ""
    cm_name: str = ""
    cm_checked: bool = False
    stale_days: int = 0

    @property
    def identity_id(self) -> str:
        """Псевдоним для группировки событий; не анонимность (почту можно угадать)."""
        return hash_email(self.email)

    @property
    def bound_name(self) -> str:
        """Подтверждённое имя CM или пусто, если ставить на события нечего."""
        return self.cm_name if self.state in ATTACHED_STATES and self.cm_name else ""


def hash_email(email: str) -> str:
    value = (email or "").strip().lower()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else ""


# ── пути и часы ───────────────────────────────────────────────────────────────

def _dir() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir())


def token_path() -> Path:
    return _dir() / TOKEN_FILE


def identity_path() -> Path:
    return _dir() / IDENTITY_FILE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── файл identity.json ────────────────────────────────────────────────────────

_cache: dict[str, Any] = {"at": 0.0, "value": None}
_lock = threading.Lock()


def _changed() -> None:
    """Состояние изменилось: сбросить кэш и снимок идентичности журнала действий."""
    _cache.update(at=0.0, value=None)
    try:
        from config import token_vault

        token_vault.reset_secret_cache()
    except Exception:
        pass
    try:
        from src.acl_event_log import writer

        writer.reset_identity()
    except Exception:
        pass


def reset_cache() -> None:
    _cache.update(at=0.0, value=None)


def _read_record() -> Optional[dict]:
    """Запись identity.json или None, если её нет или она повреждена."""
    try:
        document = json.loads(identity_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _write_record(record: dict) -> None:
    from config.paths import _atomic_write

    record = {**record, "schema": SCHEMA}
    _atomic_write(identity_path(), json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))


def _client_available() -> bool:
    try:
        from config import paths

        return bool(paths.is_valid_google_oauth_client(paths.get_google_oauth_client_path()))
    except Exception:
        return False


def _from_record(record: Optional[dict], now: datetime) -> Identity:
    stored = str((record or {}).get("state") or "")
    if record is None or stored not in (VERIFIED, EXPIRED, DECLINED):
        return Identity(state=NONE if _client_available() else UNAVAILABLE)
    state, stale_days = stored, 0
    if stored == VERIFIED:
        checked = _parse(record.get("checked_at"))
        if checked is None:
            state = EXPIRED
        elif now - checked > STALE_AFTER:
            state, stale_days = STALE, (now - checked).days
    return Identity(
        state=state,
        email=str(record.get("email") or ""),
        name=str(record.get("name") or ""),
        cm_name=str(record.get("cm_name") or ""),
        cm_checked=bool(record.get("cm_checked")),
        stale_days=stale_days,
    )


def current(*, clock: Optional[Callable[[], datetime]] = None) -> Identity:
    """Единственный читатель идентичности. Не бросает, кэшируется на пару секунд."""
    try:
        if clock is None:
            cached = _cache["value"]
            if cached is not None and 0 <= time.monotonic() - _cache["at"] < _CACHE_TTL_S:
                return cached
            value = _from_record(_read_record(), _now())
            _cache.update(at=time.monotonic(), value=value)
            return value
        return _from_record(_read_record(), clock())
    except Exception:
        return Identity()


def public(identity: Optional[Identity] = None) -> dict:
    """Что страница получает о своём менеджере (почта своя, её видит только он)."""
    who = identity or current()
    return {
        "state": who.state,
        "email": who.email,
        "name": who.name,
        "cm_name": who.cm_name,
        "cm_checked": who.cm_checked,
        "stale_days": who.stale_days,
        "my_cm_name": (os.environ.get("MY_CM_NAME") or "").strip(),
        "client_available": _client_available(),
    }


# ── события журнала ───────────────────────────────────────────────────────────

def _emit(kind: str, state: str, reason: str = "") -> None:
    """identity.* : только состояние и код причины, ни почты, ни токена."""
    try:
        from src.acl_event_log import writer

        writer.emit(f"identity.{kind}", {"state": state, "reason": reason})
    except Exception:
        pass


# ── проверка claims ───────────────────────────────────────────────────────────

def _claims_ok(claims: Any) -> bool:
    """hd, email_verified и домен почты — всё сразу, без учёта регистра."""
    if not isinstance(claims, dict):
        return False
    email = str(claims.get("email") or "").strip().lower()
    hd = str(claims.get("hd") or "").strip().lower()
    return (
        claims.get("email_verified") is True
        and hd == DOMAIN
        and email.endswith("@" + DOMAIN)
        and email.count("@") == 1
        and len(email) > len(DOMAIN) + 1
    )


def _client_id(client_path: str) -> str:
    document = json.loads(Path(client_path).read_text(encoding="utf-8-sig"))
    for key in ("installed", "web"):
        section = document.get(key) if isinstance(document, dict) else None
        if isinstance(section, dict) and str(section.get("client_id") or "").strip():
            return str(section["client_id"]).strip()
    raise IdentityError(MESSAGES["unavailable"])


def _default_verifier(token: str, client_id: str) -> dict:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, Request(), audience=client_id)


def _default_runner(*, client_path: str, scopes: list, timeout_seconds: int, **extra):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_path, scopes)
    return flow.run_local_server(
        port=0, timeout_seconds=timeout_seconds, authorization_prompt_message="", **extra)


def _is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    try:
        from google.auth import exceptions as gexc

        if isinstance(exc, gexc.TransportError):
            return True
    except Exception:
        pass
    try:
        import requests

        return isinstance(exc, requests.exceptions.RequestException)
    except Exception:
        return False


def _is_timeout(exc: BaseException) -> bool:
    try:
        from google_auth_oauthlib.flow import WSGITimeoutError

        return isinstance(exc, WSGITimeoutError)
    except Exception:
        return False


# ── вход ──────────────────────────────────────────────────────────────────────

_signin_lock = threading.Lock()


def _fail(reason: str, extra_message: str = "") -> dict:
    return {"ok": False, "reason": reason, "message": extra_message or MESSAGES.get(reason, MESSAGES["failed"])}


def known_cm_names() -> Optional[list]:
    """Имена CM из карты BI (локальный файл, без сети) или None, если списка нет."""
    try:
        from src.analysis_agent import bi_stats

        names = sorted({n for n in bi_stats.load_cm_map().values() if n}, key=str.casefold)
        return names or None
    except Exception:
        return None


def similar_names(name: str, candidates, limit: int = 3) -> list:
    import difflib

    by_fold: dict[str, str] = {}
    for candidate in candidates or []:
        by_fold.setdefault(candidate.casefold(), candidate)
    close = difflib.get_close_matches((name or "").casefold(), list(by_fold), n=limit, cutoff=0.6)
    return [by_fold[key] for key in close]


def _name_step(previous: Optional[dict], email: str, claim_name: str,
               known: Optional[list]) -> tuple[str, str, bool, list]:
    """Что делать с именем CM после входа: (шаг, cm_name, cm_checked, подсказки).

    bound — привязано без вопросов; confirm — спросить имя; who — на этой машине
    уже был другой человек: «Who works here?».
    """
    mine = (os.environ.get("MY_CM_NAME") or "").strip()
    if previous and str(previous.get("cm_name") or ""):
        same = str(previous.get("email") or "").lower() == email.lower()
        if same:
            return "bound", str(previous["cm_name"]), bool(previous.get("cm_checked")), []
        return "who", "", False, [str(previous["cm_name"])]
    if mine and known and mine.casefold() in {n.casefold() for n in known}:
        return "bound", mine, True, []
    seed = mine or claim_name
    return "confirm", "", False, similar_names(seed, known)[:3] if known else []


def sign_in(*, runner: Optional[Callable] = None, verifier: Optional[Callable] = None,
            clock: Optional[Callable[[], datetime]] = None,
            names: Optional[Callable[[], Optional[list]]] = None) -> dict:
    """Интерактивный вход: браузер, проверка, запись. Блокирует поток до конца."""
    from config import paths, token_vault

    if not _signin_lock.acquire(blocking=False):
        return _fail("busy")
    try:
        client_path = paths.get_google_oauth_client_path()
        if not paths.is_valid_google_oauth_client(client_path):
            _emit("rejected", NONE, "unavailable")
            return _fail("unavailable")
        try:
            client_id = _client_id(client_path)
            creds = (runner or _default_runner)(
                client_path=client_path, scopes=list(SCOPES),
                timeout_seconds=SIGN_IN_TIMEOUT_S, hd=DOMAIN, prompt=PROMPT)
            claims = (verifier or _default_verifier)(getattr(creds, "id_token", None), client_id)
        except IdentityError:
            _emit("rejected", NONE, "unavailable")
            return _fail("unavailable")
        except Exception as exc:
            if _is_timeout(exc):
                reason = "timeout"
            elif _is_network_error(exc):
                reason = "offline"
            else:
                reason = "failed"
            _emit("rejected", NONE, reason)
            return _fail(reason)
        if not _claims_ok(claims):
            _emit("rejected", NONE, "wrong_domain")
            return _fail("wrong_domain")

        email = str(claims["email"]).strip().lower()
        claim_name = str(claims.get("name") or "").strip()[:200]
        moment = (clock or _now)()
        previous = _read_record()
        step, cm_name, cm_checked, suggestions = _name_step(
            previous, email, claim_name, (names or known_cm_names)())
        try:
            token_vault.write_secret_text(token_path(), creds.to_json())
            _write_record({
                "email": email, "name": claim_name, "cm_name": cm_name, "cm_checked": cm_checked,
                "state": VERIFIED, "verified_at": _stamp(moment), "checked_at": _stamp(moment),
            })
        except Exception:
            log.warning("Google identity could not be saved")
            _emit("rejected", NONE, "storage")
            return _fail("failed")
        _changed()
        _emit("signed_in", VERIFIED)
        if cm_name:
            _sync_my_cm_name(cm_name)
        return {"ok": True, "state": VERIFIED, "name_step": step, "suggestions": suggestions,
                "identity": public(current())}
    finally:
        _signin_lock.release()


def decline() -> dict:
    """«Not now»: работаем как раньше, события помечаются identity=declined."""
    if current().state in ATTACHED_STATES:
        return {"ok": True, "identity": public()}
    try:
        _write_record({"state": DECLINED})
    except Exception:
        return _fail("failed")
    _changed()
    _emit("rejected", DECLINED, "declined")
    return {"ok": True, "identity": public(current())}


# ── имя CM ────────────────────────────────────────────────────────────────────

def _clean_name(name: Any) -> str:
    value = " ".join(str(name or "").split())
    if not value or len(value) > 100 or any(ord(c) < 32 for c in value):
        raise IdentityError("Enter your name as it appears in the CM column.")
    return value


def _sync_my_cm_name(name: str) -> None:
    """MY_CM_NAME и привязанное имя всегда равны: источник списка аккаунтов — он."""
    from config import paths

    try:
        paths.update_machine_env({"MY_CM_NAME": name})
    except Exception:
        log.warning("MY_CM_NAME could not be saved")
    os.environ["MY_CM_NAME"] = name


def set_name(name: str, *, cm_checked: bool = False) -> dict:
    """Привязать имя CM к вошедшему и записать то же в MY_CM_NAME."""
    clean = _clean_name(name)
    record = _read_record()
    if not record or record.get("state") not in (VERIFIED, EXPIRED):
        raise IdentityError("Sign in first.")
    record.update(cm_name=clean, cm_checked=bool(cm_checked))
    _write_record(record)
    _sync_my_cm_name(clean)
    _changed()
    return public(current())


def check_name(name: str, known: Optional[list]) -> tuple[bool, list]:
    """(есть ли имя в списке, подсказки). Без списка — (False, [])."""
    if not known:
        return False, []
    clean = " ".join(str(name or "").split())
    if clean.casefold() in {n.casefold() for n in known}:
        return True, []
    return False, similar_names(clean, known)


# ── выход ─────────────────────────────────────────────────────────────────────

def sign_out() -> dict:
    """Удалить оба файла. MY_CM_NAME остаётся. Revoke не зовём: грант общий с таблицами."""
    for path in (token_path(), identity_path()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Identity file could not be removed")
    _changed()
    _emit("signed_out", NONE)
    return {"ok": True, "identity": public(current())}


# ── тихая перепроверка ────────────────────────────────────────────────────────

def _default_refresher(creds) -> dict:
    """Обновить токен и вернуть claims: id_token из ответа, иначе userinfo [verify]."""
    from google.auth.transport.requests import Request

    request = Request()
    creds.refresh(request)
    token = getattr(creds, "id_token", None)
    if token:
        client_id = getattr(creds, "client_id", "") or ""
        return _default_verifier(token, client_id)
    import requests

    reply = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {creds.token}"}, timeout=15)
    reply.raise_for_status()
    return reply.json()


def recheck_due(*, clock: Optional[Callable[[], datetime]] = None) -> bool:
    record = _read_record()
    if not record or record.get("state") != VERIFIED:
        return False
    checked = _parse(record.get("checked_at"))
    return checked is None or (clock or _now)() - checked > RECHECK_AFTER


def _expire(record: dict, reason: str, *, drop_token: bool) -> str:
    if drop_token:
        try:
            token_path().unlink(missing_ok=True)
        except OSError:
            pass
    record["state"] = EXPIRED
    _write_record(record)
    _changed()
    _emit("expired", EXPIRED, reason)
    return EXPIRED


def recheck(*, refresher: Optional[Callable] = None, clock: Optional[Callable[[], datetime]] = None,
            force: bool = False) -> str:
    """Тихая перепроверка. Возвращает итоговое состояние; сеть не понижает verified."""
    from config import token_vault

    if not force and not recheck_due(clock=clock):
        return current(clock=clock).state
    record = _read_record()
    if not record or record.get("state") != VERIFIED:
        return current(clock=clock).state
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_info(
            json.loads(token_vault.read_secret_text(token_path())), SCOPES)
    except Exception:
        # Нет файла, запечатан чужим пользователем Windows или повреждён.
        return _expire(record, "token_unreadable", drop_token=False)
    try:
        claims = (refresher or _default_refresher)(creds)
    except Exception as exc:
        if _is_network_error(exc):
            return current(clock=clock).state
        try:
            from google.auth.exceptions import RefreshError

            refused = isinstance(exc, RefreshError)
        except Exception:
            refused = False
        return _expire(record, "invalid_grant" if refused else "check_failed", drop_token=refused)
    if not _claims_ok(claims) or str(claims.get("email") or "").strip().lower() != str(record.get("email") or "").lower():
        return _expire(record, "reverify_failed", drop_token=True)
    record["checked_at"] = _stamp((clock or _now)())
    try:
        _write_record(record)
        if getattr(creds, "refresh_token", None) and hasattr(creds, "to_json"):
            token_vault.write_secret_text(token_path(), creds.to_json())
    except Exception:
        log.warning("Google identity check could not be saved")
    _changed()
    return current(clock=clock).state


_recheck_running = threading.Event()


def recheck_async_if_due() -> bool:
    """Фоновая перепроверка, если пора. Не больше одной за раз, без таймера."""
    try:
        if not recheck_due() or _recheck_running.is_set():
            return False
        _recheck_running.set()

        def work() -> None:
            try:
                recheck()
            except Exception:
                log.info("Google identity check did not finish")
            finally:
                _recheck_running.clear()

        threading.Thread(target=work, name="acl-identity-recheck", daemon=True).start()
        return True
    except Exception:
        return False
