# -*- coding: utf-8 -*-
"""Чем авторизоваться в Multilogin — один поставщик на слот (A/B), ACL-1335.

До 27.09 всё держалось на токене автоматизации `MULTILOGIN_AUTOMATION_TOKEN_<S>`.
Потом Multilogin перестал их выдавать (`/workspace/automation_token` → 403
всем, даже владельцу), а токен, выданный на другой машине, профили не
запускает («can't lock profile»). Поэтому каждая машина теперь входит сама —
обычным входом — и продлевает его сама через `/user/refresh_token`.

Порядок, в котором выбирается токен:

1. Живой токен автоматизации — как раньше, чтобы машины с сохранённым
   токеном работали без переподключения. «Живой» — не истёк по своему `exp`
   и не получил от Multilogin 401 в этом процессе.
2. Сохранённый вход: почта, refresh-токен и рабочее пространство слота в
   машинном `.env`. Токен сессии живёт только в памяти: продлевается, когда
   до конца меньше `RENEW_MARGIN`, или после 401 (одно продление, один
   повтор — это делает вызывающий через `renew_after_401`).

Пароля и его хеша здесь нет нигде. В журнал не пишется ни один токен.
Продление — под замком слота: два потока не продлевают одновременно, а второй
получает то, что продлил первый. Если Multilogin повернул refresh-токен, новый
сразу записывается в машинный `.env` тем же писателем, что и всё остальное.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

SLOTS = ("A", "B")

#: Продлеваем заранее: запуск профиля с токеном, истекающим посреди запроса,
#: даёт 401, который потом не отличить от настоящего отказа.
RENEW_MARGIN = 5 * 60
#: Токен без читаемого `exp` считаем живущим столько (с запасом вниз).
DEFAULT_LIFETIME = 25 * 60
#: Токен автоматизации, истекающий в ближайшую минуту, уже не живой.
AUTOMATION_MARGIN = 60

SIGNIN_EXPIRED = "Multilogin sign-in expired — press Reconnect in Accounts."


class MlxCredentialError(RuntimeError):
    """Причина, по которой токена нет, — словами для человека (English)."""


class MlxNotConnected(MlxCredentialError):
    pass


class MlxSignInExpired(MlxCredentialError):
    pass


def env_key(kind: str, slot: str) -> str:
    return f"MULTILOGIN_{kind}_{slot}"


#: Что хранится в машинном `.env` для входа слота. Пароля тут нет.
SESSION_KINDS = ("EMAIL", "REFRESH_TOKEN", "WORKSPACE_ID", "WORKSPACE_NAME", "WORKSPACE_ROLE")


def _now() -> float:
    """Часы — отдельной функцией, чтобы тест мог их подменить."""
    return time.time()


def _http():
    """Модуль HTTP — отдельной функцией, чтобы тест подставил поддельный Multilogin."""
    import requests

    return requests


def _persist(values: dict[str, str]) -> None:
    from config.paths import update_machine_env

    update_machine_env(values)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass(frozen=True)
class SavedSession:
    email: str
    refresh_token: str
    workspace_id: str
    workspace_name: str = ""
    role: str = ""


def saved_session(slot: str) -> SavedSession | None:
    """Сохранённый вход слота или None, если его нет (или он неполный)."""
    slot = _slot(slot)
    email = _env(env_key("EMAIL", slot))
    refresh = _env(env_key("REFRESH_TOKEN", slot))
    workspace = _env(env_key("WORKSPACE_ID", slot))
    if not (email and refresh and workspace):
        return None
    return SavedSession(email=email, refresh_token=refresh, workspace_id=workspace,
                        workspace_name=_env(env_key("WORKSPACE_NAME", slot)),
                        role=_env(env_key("WORKSPACE_ROLE", slot)).lower())


def has_saved_session(slot: str) -> bool:
    return saved_session(slot) is not None


def _slot(slot: str) -> str:
    key = (slot or "A").strip().upper()
    if key not in SLOTS:
        raise ValueError(f"multilogin_instance must be A or B, got {slot!r}")
    return key


#: Токены автоматизации, на которые Multilogin ответил 401 в этом процессе.
#: Храним только их отпечатки — сами токены в памяти лишний раз не держим.
_REJECTED_AUTOMATION: set[int] = set()


class SlotCredentials:
    """Поставщик токена одного слота. Один экземпляр на слот — см. `for_slot`."""

    def __init__(self, slot: str) -> None:
        self.slot = _slot(slot)
        self._lock = threading.RLock()
        self._bearer = ""
        self._bearer_exp = 0.0
        self._bearer_workspace = ""
        #: Отпечатки токенов сессии, выданных этим поставщиком: по ним
        #: `renew_after_401` отличает отказ сессии от отказа токена автоматизации.
        self._issued: set[int] = set()
        #: refresh-токен, который Multilogin отверг: пока он же лежит в
        #: настройке, в сеть не ходим — ответ известен.
        self._dead_refresh = ""

    # ── что есть ─────────────────────────────────────────────────────────

    def automation_token(self) -> str:
        return _env(env_key("AUTOMATION_TOKEN", self.slot))

    def _automation_usable(self, token: str) -> bool:
        if not token or hash(token) in _REJECTED_AUTOMATION:
            return False
        from src.web.multilogin_auth import token_expiry

        exp = token_expiry(token)
        return exp is None or exp > _now() + AUTOMATION_MARGIN

    def reject_automation(self, token: str) -> None:
        """Multilogin не принял токен автоматизации (проверка статуса): дальше — вход."""
        if token:
            _REJECTED_AUTOMATION.add(hash(token.strip()))

    def method(self, automation_token: str | None = None) -> str:
        """"automation", "signin" или "" — чем слот авторизуется сейчас."""
        token = self.automation_token() if automation_token is None else (automation_token or "").strip()
        session = saved_session(self.slot)
        if token and (session is None or self._automation_usable(token)):
            return "automation"
        return "signin" if session is not None else ""

    # ── токен ────────────────────────────────────────────────────────────

    def bearer(self, automation_token: str | None = None) -> str:
        """Действующий токен для заголовка Authorization.

        `automation_token` — то, что лежит в конфиге лаунчера (None — прочитать
        из окружения). Без сохранённого входа токен автоматизации отдаётся
        как есть, даже истёкший: так было всегда, и ответит на него сам
        Multilogin — поведение машин без входа не меняется ни на байт.
        """
        token = self.automation_token() if automation_token is None else (automation_token or "").strip()
        session = saved_session(self.slot)
        if token and (session is None or self._automation_usable(token)):
            return token
        if session is not None:
            return self._session_bearer(session)
        raise MlxNotConnected(
            f"Multilogin {self.slot} is not connected on this machine — open Accounts and "
            f"press Connect Multilogin (MULTILOGIN_AUTOMATION_TOKEN_{self.slot} is not set "
            "and there is no saved sign-in)."
        )

    def renew_after_401(self, used: str, automation_token: str | None = None) -> str:
        """Multilogin ответил 401 на `used`: чем повторить, или "" — нечем.

        Токен сессии продлевается принудительно (ровно один раз: если другой
        поток уже продлил, отдаётся его результат). Токен автоматизации при
        сохранённом входе помечается отвергнутым, и дальше слот идёт входом;
        без входа повторять нечем — отказ остаётся отказом, как раньше.
        """
        used = (used or "").strip()
        with self._lock:
            if used and hash(used) not in self._issued:
                session = saved_session(self.slot)
                if session is None:
                    return ""
                _REJECTED_AUTOMATION.add(hash(used))
                log.info("Multilogin %s: automation token refused, switching to the saved sign-in", self.slot)
                return self._session_bearer(session)
            session = saved_session(self.slot)
            if session is None:
                return ""
            if self._bearer and self._bearer != used:
                return self._bearer
            return self._session_bearer(session, force=True)

    def _session_bearer(self, session: SavedSession, *, force: bool = False) -> str:
        with self._lock:
            # Сессию могли переподключить, пока мы ждали замок.
            session = saved_session(self.slot) or session
            if (not force and self._bearer and self._bearer_workspace == session.workspace_id
                    and self._bearer_exp - RENEW_MARGIN > _now()):
                return self._bearer
            if self._dead_refresh and self._dead_refresh == session.refresh_token:
                raise MlxSignInExpired(SIGNIN_EXPIRED)
            return self._renew(session)

    def _renew(self, session: SavedSession) -> str:
        from src.web.multilogin_auth import refresh_session

        result = refresh_session(session.email, session.refresh_token, session.workspace_id,
                                 requests_module=_http(), timeout=15)
        if not result.ok:
            if result.rejected:
                self._dead_refresh = session.refresh_token
                self._bearer = ""
                log.warning("Multilogin %s: the saved sign-in was refused; Reconnect is needed", self.slot)
                raise MlxSignInExpired(SIGNIN_EXPIRED)
            raise MlxCredentialError(f"Could not renew the Multilogin sign-in: {result.error}")
        if result.refresh_token and result.refresh_token != session.refresh_token:
            key = env_key("REFRESH_TOKEN", self.slot)
            # Старый refresh-токен после поворота может быть уже мёртв: сначала
            # окружение этого процесса, потом машинный `.env` для следующего.
            os.environ[key] = result.refresh_token
            try:
                _persist({key: result.refresh_token})
            except Exception as exc:  # диск, права — процесс продолжит новым токеном
                log.warning("Multilogin %s: could not save the renewed sign-in (%s)",
                            self.slot, type(exc).__name__)
        self._adopt(result.bearer, session.workspace_id)
        log.info("Multilogin %s: sign-in renewed (%s)", self.slot, session.workspace_name or "workspace")
        return result.bearer

    def _adopt(self, bearer: str, workspace_id: str) -> None:
        from src.web.multilogin_auth import token_expiry

        now = _now()
        exp = token_expiry(bearer)
        self._bearer = bearer
        self._bearer_exp = exp if exp is not None else now + DEFAULT_LIFETIME
        self._bearer_workspace = workspace_id
        self._dead_refresh = ""
        if len(self._issued) > 64:
            self._issued.clear()
        self._issued.add(hash(bearer))

    def adopt(self, bearer: str, workspace_id: str) -> None:
        """Принять токен, только что полученный при подключении."""
        with self._lock:
            self._adopt(bearer, workspace_id)

    def describe(self) -> dict:
        """Как подключён слот — для строки статуса. Без токенов."""
        session = saved_session(self.slot)
        return {
            "method": self.method(),
            "email": session.email if session else "",
            "workspace": session.workspace_name if session else "",
            "role": session.role if session else "",
        }


_REGISTRY: dict[str, SlotCredentials] = {}
_REGISTRY_LOCK = threading.Lock()


def for_slot(slot: str) -> SlotCredentials:
    key = _slot(slot)
    with _REGISTRY_LOCK:
        creds = _REGISTRY.get(key)
        if creds is None:
            creds = _REGISTRY[key] = SlotCredentials(key)
        return creds


def reset() -> None:
    """Забыть токены в памяти (тесты и переподключение)."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _REJECTED_AUTOMATION.clear()


def bearer_for(config) -> str:
    """Токен для конфига лаунчера (`MultiloginInstanceConfig` или похожего)."""
    slot = getattr(config, "instance_id", None) or "A"
    return for_slot(slot).bearer(getattr(config, "automation_token", None))


def renew_after_401(config, used: str) -> str:
    slot = getattr(config, "instance_id", None) or "A"
    return for_slot(slot).renew_after_401(used, getattr(config, "automation_token", None))


def forbidden_hint(config) -> str:
    """Пояснение к 403 на запуске, когда слот идёт входом: дело, скорее всего, в роли."""
    slot = getattr(config, "instance_id", None) or "A"
    try:
        creds = for_slot(slot)
    except ValueError:
        return ""
    if creds.method(getattr(config, "automation_token", None)) != "signin":
        return ""
    session = saved_session(slot)
    role = session.role if session else ""
    where = (session.workspace_name if session else "") or "this workspace"
    return (f" Your Multilogin role in {where}{f' ({role})' if role else ''} may not be allowed "
            "to start profiles — ask the workspace owner for the Launcher role or higher.")


def save_session(slot: str, *, email: str, refresh_token: str, workspace_id: str,
                 workspace_name: str = "", role: str = "", bearer: str = "") -> None:
    """Сохранить вход слота в машинный `.env` и принять токен сессии в память.

    Токен автоматизации слота откладывается в `_PREVIOUS`: человек подключил
    машину входом, а оставшийся чужой токен шёл бы первым и упирался в
    «can't lock profile». Отложен, а не стёрт — вернуть руками можно.
    """
    key = _slot(slot)
    values = {
        env_key("EMAIL", key): email.strip(),
        env_key("REFRESH_TOKEN", key): refresh_token,
        env_key("WORKSPACE_ID", key): workspace_id,
        env_key("WORKSPACE_NAME", key): workspace_name,
        env_key("WORKSPACE_ROLE", key): role,
    }
    old = _env(env_key("AUTOMATION_TOKEN", key))
    if old:
        values[env_key("AUTOMATION_TOKEN", key) + "_PREVIOUS"] = old
        values[env_key("AUTOMATION_TOKEN", key)] = ""
    _persist(values)
    for name, value in values.items():
        os.environ[name] = value
    creds = for_slot(key)
    if bearer:
        creds.adopt(bearer, workspace_id)
