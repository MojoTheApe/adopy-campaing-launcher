"""ACL-1826: потеря общей сессии Multilogin (слот B, KLD) и отказ входа в прокси.

Что случилось (C:\\acl-notes\\mlx-403-investigation.md). Общий пользователь KLD
входит в приложение Multilogin на нескольких компьютерах, а действующая
десктоп-сессия у него одна. Когда её забирает другой компьютер, облако отвечает
локальному лаунчеру `FORBIDDEN_REQUEST`, и старт любого KLD-профиля падает с
«Start profile failed HTTP 403: restricted permission» — при живом токене и
правильном пространстве. Слот A (Inhouse) в те же секунды работает.

Что делает модуль:

* узнаёт эту ошибку (и отказ входа в прокси) по тексту/атрибутам исключения и
  даёт устойчивые коды `mlx_session_lost` / `proxy_auth`;
* держит «защёлку» пачки: после первого `mlx_session_lost` на слоте B остальные
  старты слота B в этой пачке пропускаются с той же причиной, слот A идёт дальше;
* пишет каждое событие в телеметрию и в Sentry (время, слот, аккаунт);
* до пачки на слоте B офлайн проверяет токен: пространство и срок (только
  claims JWT, подпись и сам токен никуда не выводятся).

Живых вызовов Multilogin здесь нет.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

SESSION_LOST = "mlx_session_lost"
PROXY_AUTH = "proxy_auth"
#: Предполётная проверка токена слота B не прошла (срок / чужое пространство).
TOKEN_EXPIRED = "mlx_token_expired"
TOKEN_WORKSPACE = "mlx_token_workspace"

SESSION_LOST_MESSAGE = (
    "Multilogin on this computer lost the shared KLD sign-in (it was used on another computer). "
    "Sign in again in the Multilogin app here, then press Retry."
)
PROXY_AUTH_MESSAGE = "The proxy login for this profile is wrong or expired — update it in Multilogin"
TOKEN_EXPIRED_MESSAGE = "Multilogin token expired — sign in again"
TOKEN_WORKSPACE_MESSAGE = ("The Multilogin token for this slot belongs to another workspace. "
                           "Connect Multilogin again and choose the right workspace, then press Retry.")

MESSAGES = {
    SESSION_LOST: SESSION_LOST_MESSAGE,
    PROXY_AUTH: PROXY_AUTH_MESSAGE,
    TOKEN_EXPIRED: TOKEN_EXPIRED_MESSAGE,
    TOKEN_WORKSPACE: TOKEN_WORKSPACE_MESSAGE,
}

#: Коды, после которых остальные старты слота в пачке пропускаются.
BATCH_STOPS = frozenset({SESSION_LOST, TOKEN_EXPIRED, TOKEN_WORKSPACE})

BATCH_TTL_S = 3 * 3600.0
BATCH_MAX = 64
#: Запас до `exp`, при котором токен ещё считается рабочим.
TOKEN_MARGIN_S = 60.0


def message_for(code: str) -> str:
    return MESSAGES.get(code or "", "")


# ───────────────────────── распознавание ─────────────────────────

def classify(err: Any, instance: str = "") -> str:
    """Код по исключению или тексту: `mlx_session_lost` / `proxy_auth` / "".

    403 «restricted permission» считается потерей сессии только на слоте B:
    на слоте A тот же текст может значить другое, и подменять его нельзя.
    """
    text = str(err or "")
    low = text.lower()
    if "err_invalid_auth_credentials" in low:
        return PROXY_AUTH
    # ACL-2185: 401 на старте профиля — токен автоматизации слота погашен или
    # истёк (на любом слоте). Сообщение про токен, а не общий сбой профиля.
    status401 = getattr(err, "status", None) or getattr(err, "status_code", None)
    if status401 == 401 or "start profile failed http 401" in low:
        return TOKEN_EXPIRED
    if (instance or "").strip().upper() != "B":
        return ""
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    error_code = str(getattr(err, "error_code", "") or "").upper()
    forbidden = (status == 403 or "http 403" in low or error_code == "FORBIDDEN_REQUEST"
                 or "forbidden_request" in low)
    if forbidden and ("restricted permission" in low or "forbidden_request" in low
                      or error_code == "FORBIDDEN_REQUEST"):
        return SESSION_LOST
    return ""


def instance_of(account: str) -> str:
    """Слот аккаунта из карты ("A" / "B"); пусто, если аккаунта нет."""
    try:
        from src.analysis_agent import account_spend as _as

        entry = _as._entry_for(account)
    except Exception:
        return ""
    if entry is None:
        return ""
    return (getattr(entry, "multilogin_instance", "") or "A").strip().upper() or "A"


# ───────────────────────── запись события ─────────────────────────

def record_event(code: str, instance: str, account: str, *, skipped: bool = False,
                 detail: str = "") -> None:
    """Каждое событие: строка телеметрии (время ставит `record`) и событие Sentry.

    Пропущенные старты пишутся в телеметрию, но в Sentry не уходят: настоящий
    отказ один, остальное — следствие.
    """
    diag = {"code": code, "slot": (instance or "").upper(), "account": account or ""}
    if detail:
        diag["detail"] = detail[:200]
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        _rt.record("mlx_start_skipped" if skipped else "mlx_start_failed", False, diag=diag)
    except Exception:
        log.debug("telemetry row for %s was not written", code, exc_info=True)
    try:
        from src.fb_api import telemetry as _tm

        _tm.record({"event": "mlx_start_skipped" if skipped else "mlx_start_failed", **diag})
    except Exception:
        log.debug("pilot journal row for %s was not written", code, exc_info=True)
    if skipped:
        return
    try:
        from src import acl_sentry

        acl_sentry.report_step_failure(
            "mlx_start", why=f"{code} slot={diag['slot']}", account=account or "",
            reason=code, group_step="mlx_start", kind="blocking", diag={"slot": diag["slot"]})
    except Exception:
        log.debug("sentry event for %s was not sent", code, exc_info=True)


# ───────────────────────── защёлка пачки ─────────────────────────

_LOCK = threading.Lock()
_BATCHES: "OrderedDict[str, dict]" = OrderedDict()


def _clock() -> float:
    return time.monotonic()


def _prune(now: float) -> None:
    for key in [k for k, v in _BATCHES.items() if now - v["t"] > BATCH_TTL_S]:
        _BATCHES.pop(key, None)
    while len(_BATCHES) > BATCH_MAX:
        _BATCHES.popitem(last=False)


def begin_batch(batch_id: str) -> None:
    """Завести пачку (повторный вызов с тем же id ничего не сбрасывает)."""
    batch_id = (batch_id or "").strip()[:80]
    if not batch_id:
        return
    now = _clock()
    with _LOCK:
        _prune(now)
        _BATCHES.setdefault(batch_id, {"t": now, "stopped": {}, "checked": set()})


def note_failure(code: str, instance: str, account: str = "", *, detail: str = "") -> None:
    """Зафиксировать отказ старта: запись + защёлка всех живых пачек этого слота."""
    instance = (instance or "").strip().upper()
    record_event(code, instance, account, detail=detail)
    if code not in BATCH_STOPS:
        return
    with _LOCK:
        _prune(_clock())
        for state in _BATCHES.values():
            state["stopped"].setdefault(instance, code)


def refusal(batch_id: str, instance: str, account: str = "") -> dict | None:
    """Пропустить ли старт: `{code, message, skipped: True}` или None.

    Первый вопрос про слот B в пачке заодно прогоняет офлайн-проверку токена;
    провал защёлкивает слот на всю пачку (все B-аккаунты получают причину).
    """
    batch_id = (batch_id or "").strip()[:80]
    instance = (instance or "").strip().upper()
    if not batch_id or not instance:
        return None
    begin_batch(batch_id)
    with _LOCK:
        state = _BATCHES.get(batch_id)
        need_check = state is not None and instance == "B" and "B" not in state["checked"]
        if state is not None and need_check:
            state["checked"].add("B")
    if need_check:
        verdict = token_preflight("B")
        if not verdict["ok"]:
            note_failure(verdict["code"], "B", account, detail="preflight")
    with _LOCK:
        state = _BATCHES.get(batch_id)
        code = state["stopped"].get(instance) if state else None
    if not code:
        return None
    record_event(code, instance, account, skipped=True)
    return {"ok": False, "code": code, "message": message_for(code), "skipped": True}


# Аккаунт и пачка «в воздухе»: места, где старт глубоко, не знают, чей он, а
# передавать им лишние аргументы значит ломать все подмены в тестах.
_CTX_BATCH: contextvars.ContextVar[str] = contextvars.ContextVar("acl_mlx_batch", default="")
_CTX_ACCOUNT: contextvars.ContextVar[str] = contextvars.ContextVar("acl_mlx_account", default="")


def current_batch() -> str:
    return _CTX_BATCH.get()


def current_account() -> str:
    return _CTX_ACCOUNT.get()


@contextlib.contextmanager
def scope(*, account: str = "", batch: str = ""):
    """Пометить текущий поток: чей это старт и из какой пачки."""
    tokens = [(_CTX_ACCOUNT, _CTX_ACCOUNT.set(account or _CTX_ACCOUNT.get())),
              (_CTX_BATCH, _CTX_BATCH.set(batch or _CTX_BATCH.get()))]
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def reset_for_tests() -> None:
    with _LOCK:
        _BATCHES.clear()


# ───────────────────────── предполётная проверка токена ─────────────────────────

def _claims(token: str) -> dict | None:
    """Payload JWT без проверки подписи; токен наружу не уходит."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def token_preflight(instance: str = "B", *, env: dict | None = None, now: float | None = None) -> dict:
    """Офлайн: срок токена и пространство. `{ok, code, message}`.

    Не декодируемый или отсутствующий токен здесь не отказ: старт сам
    скажет, чего ему не хватает. Блокируем только то, что видно точно:
    истёк срок или пространство в токене не то, что настроено.
    """
    env = os.environ if env is None else env
    key = (instance or "B").strip().upper()
    claims = _claims((env.get(f"MULTILOGIN_AUTOMATION_TOKEN_{key}") or "").strip())
    if not claims:
        return {"ok": True, "code": "", "message": ""}
    exp = claims.get("exp")
    moment = time.time() if now is None else now
    if isinstance(exp, (int, float)) and exp - TOKEN_MARGIN_S <= moment:
        return {"ok": False, "code": TOKEN_EXPIRED, "message": TOKEN_EXPIRED_MESSAGE}
    want = (env.get(f"MULTILOGIN_WORKSPACE_ID_{key}") or "").strip().lower()
    have = str(claims.get("workspaceID") or claims.get("workspace_id") or "").strip().lower()
    if want and have and want != have:
        return {"ok": False, "code": TOKEN_WORKSPACE, "message": TOKEN_WORKSPACE_MESSAGE}
    return {"ok": True, "code": "", "message": ""}


#: За сколько до `exp` дешборд начинает предупреждать (ACL-2185).
EXPIRY_WARN_S = 3600.0


def token_expiry(instance: str = "A", *, env: dict | None = None, now: float | None = None) -> dict:
    """Офлайн: когда истекает токен слота. Сам токен наружу не уходит.

    `state`: `none` (токена нет), `unknown` (не JWT или без `exp`), `ok`,
    `warning` (осталось не больше часа), `expired`. Сеть не нужна.
    """
    env = os.environ if env is None else env
    key = (instance or "A").strip().upper()
    out = {"instance": key, "has_token": False, "state": "none",
           "expires_at": None, "expires_epoch": None, "seconds_left": None}
    token = (env.get(f"MULTILOGIN_AUTOMATION_TOKEN_{key}") or "").strip()
    if not token:
        return out
    out["has_token"] = True
    out["state"] = "unknown"
    exp = (_claims(token) or {}).get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return out
    moment = time.time() if now is None else now
    left = float(exp) - moment
    out["expires_epoch"] = float(exp)
    out["expires_at"] = datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat()
    out["seconds_left"] = int(left)
    out["state"] = "expired" if left <= 0 else "warning" if left <= EXPIRY_WARN_S else "ok"
    return out


# ───────────────────────── для мест, где ловят исключения ─────────────────────────

def humanize(err: Any, instance: str = "", account: str = "") -> tuple[str, str]:
    """(человеческий текст, код) для исключения старта; ("", "") — не наш случай.

    Побочный эффект: событие записано и пачки защёлкнуты. Слот берётся из
    аргумента, иначе из карты по аккаунту.
    """
    slot = (instance or "").strip().upper() or instance_of(account)
    code = classify(err, slot)
    if not code:
        return "", ""
    note_failure(code, slot, account)
    return message_for(code), code
