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
* держит «защёлку» пачки (ACL-2668, решение владельца 16.11): после первого
  `mlx_session_lost` / протухшего токена слот один раз перепроверяется — офлайн
  токен и один следующий старт этого слота (это вызов MLX, не Meta). Жив —
  пачка идёт дальше; снова отказ — остальные старты слота в пачке пропускаются
  с одной общей короткой причиной. Защёлка живёт до первого удачного старта
  слота или нового входа (сменился токен), а не по часам. Слот A идёт дальше;
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
#: ACL-2821: приложение Multilogin вошло в пространство другого слота — лаунчер отвечает 403.
WORKSPACE_MISMATCH = "mlx_workspace_mismatch"

# ACL-2821: отказ 403 лаунчера не говорит, какая из двух причин настоящая, поэтому
# текст называет обе (слот B = KLD, слот A = Inhouse; имена слотов подставляются).
SESSION_LOST_MESSAGE = (
    "Multilogin refused to open this KLD profile - the Multilogin app may be signed in to "
    "Inhouse (switch it to KLD), or the KLD sign-in is in use on another computer"
)
PROXY_AUTH_MESSAGE = "The proxy login for this profile is wrong or expired — update it in Multilogin"
TOKEN_EXPIRED_MESSAGE = "Multilogin token expired — sign in again"
TOKEN_WORKSPACE_MESSAGE = ("The Multilogin token for this slot belongs to another workspace. "
                           "Connect Multilogin again and choose the right workspace, then press Retry.")

#: Имена пространств по умолчанию, если в .env их нет (слот A = Inhouse, B = KLD).
SLOT_NAMES = {"A": "Inhouse", "B": "KLD"}
WORKSPACE_MISMATCH_TEMPLATE = ("Multilogin refused to open this {want} profile - the Multilogin app may be "
                               "signed in to {app} (switch it to {want}), or the {want} sign-in is in use "
                               "on another computer")
WORKSPACE_MISMATCH_MESSAGE = ("Multilogin refused to open this profile - the Multilogin app may be signed in to "
                              "the other workspace, or this sign-in is in use on another computer")

MESSAGES = {
    WORKSPACE_MISMATCH: WORKSPACE_MISMATCH_MESSAGE,
    SESSION_LOST: SESSION_LOST_MESSAGE,
    PROXY_AUTH: PROXY_AUTH_MESSAGE,
    TOKEN_EXPIRED: TOKEN_EXPIRED_MESSAGE,
    TOKEN_WORKSPACE: TOKEN_WORKSPACE_MESSAGE,
}

#: Коды, после которых остальные старты слота в пачке пропускаются.
BATCH_STOPS = frozenset({SESSION_LOST, TOKEN_EXPIRED, TOKEN_WORKSPACE, WORKSPACE_MISMATCH})

#: ACL-2668: одна общая причина у всех пропущенных стартов слота (полный текст —
#: только у настоящего отказа), чтобы не было потока одинаковых ошибок.
SLOT_SKIP_MESSAGE = "Multilogin: sign in again, then press Retry."

#: Сколько пачек помнить (только предел памяти; срока жизни у защёлки нет).
BATCH_MAX = 64
#: Запас до `exp`, при котором токен ещё считается рабочим.
TOKEN_MARGIN_S = 60.0


def message_for(code: str, instance: str = "") -> str:
    if code == WORKSPACE_MISMATCH and instance:
        return mismatch_message(instance)
    return MESSAGES.get(code or "", "")


def _slot_name(slot: str) -> str:
    slot = (slot or "").strip().upper()
    try:
        from src.web import mlx_slots as _slots

        name = _slots.trusted_name(slot) or _slots.workspace_name(slot)
    except Exception:
        name = ""
    return name or SLOT_NAMES.get(slot, slot or "another workspace")


def mismatch_message(instance: str) -> str:
    """ACL-2821: осторожный текст отказа 403 — только для аккаунтов этого слота, без утверждений."""
    want = (instance or "").strip().upper()
    other = "B" if want == "A" else "A"
    return WORKSPACE_MISMATCH_TEMPLATE.format(app=_slot_name(other), want=_slot_name(want))


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
    slot = (instance or "").strip().upper()
    if slot not in ("A", "B"):
        return ""
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    error_code = str(getattr(err, "error_code", "") or "").upper()
    forbidden = (status == 403 or "http 403" in low or error_code == "FORBIDDEN_REQUEST"
                 or "forbidden_request" in low)
    if forbidden and ("restricted permission" in low or "forbidden_request" in low
                      or error_code == "FORBIDDEN_REQUEST"):
        # ACL-2821: слот A отказ получает, когда приложение вошло в KLD (или вход
        # занят на другом компьютере). Слот B остаётся ACL-1826 `mlx_session_lost`,
        # его текст теперь называет обе причины. Состояния «что стартовало раньше»
        # не используем: из 403 причину не узнать.
        return WORKSPACE_MISMATCH if slot == "A" else SESSION_LOST
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


def _prune() -> None:
    # ACL-2668: срока в 3 ч больше нет — защёлку снимает удачный старт или новый вход.
    while len(_BATCHES) > BATCH_MAX:
        _BATCHES.popitem(last=False)


def _token_mark(instance: str) -> str:
    """Отпечаток токена слота: сменился — значит, вошли заново. Сам токен не хранится."""
    import hashlib

    token = (os.environ.get(f"MULTILOGIN_AUTOMATION_TOKEN_{instance}") or "").strip()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else ""


def _new_state(now: float) -> dict:
    # suspect: был отказ, перепроверки ещё не было; probing: перепроверка
    # (один следующий старт) уже пущена; stopped: перепроверка подтвердила отказ.
    return {"t": now, "stopped": {}, "suspect": {}, "probing": {}, "checked": set()}


def begin_batch(batch_id: str) -> None:
    """Завести пачку (повторный вызов с тем же id ничего не сбрасывает)."""
    batch_id = (batch_id or "").strip()[:80]
    if not batch_id:
        return
    with _LOCK:
        _BATCHES.setdefault(batch_id, _new_state(_clock()))
        _prune()


def note_failure(code: str, instance: str, account: str = "", *, detail: str = "",
                 checked: bool = False) -> None:
    """Зафиксировать отказ старта: запись + защёлка всех живых пачек этого слота.

    Первый отказ только помечает слот «под подозрением»: следующий его старт
    станет перепроверкой. Отказ на перепроверке (или `checked=True` — отказ
    самой проверки, например офлайн-токена) защёлкивает слот до удачного старта
    или нового входа.
    """
    instance = (instance or "").strip().upper()
    record_event(code, instance, account, detail=detail)
    if code not in BATCH_STOPS:
        return
    mark = _token_mark(instance)
    with _LOCK:
        for state in _BATCHES.values():
            if instance in state["stopped"]:
                continue
            if checked or instance in state["probing"]:
                state["stopped"][instance] = {"code": code, "token": mark}
                state["suspect"].pop(instance, None)
                state["probing"].pop(instance, None)
            else:
                state["suspect"].setdefault(instance, code)


def note_started(instance: str) -> None:
    """Старт слота прошёл: сессия MLX жива — снять подозрение и защёлку во всех пачках."""
    instance = (instance or "").strip().upper()
    if not instance:
        return
    with _LOCK:
        for state in _BATCHES.values():
            for key in ("stopped", "suspect", "probing"):
                state[key].pop(instance, None)


def refusal(batch_id: str, instance: str, account: str = "") -> dict | None:
    """Пропустить ли старт: `{code, message, skipped: True}` или None.

    Первый вопрос про слот B в пачке заодно прогоняет офлайн-проверку токена.
    После отказа слот перепроверяется один раз: офлайн-токен и затем этот
    старт (вызов MLX, к Meta не ходим). Если и перепроверка не прошла,
    остальные старты слота получают одну общую причину `SLOT_SKIP_MESSAGE`.
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
            note_failure(verdict["code"], "B", account, detail="preflight", checked=True)
    with _LOCK:
        state = _BATCHES.get(batch_id)
        stop = state["stopped"].get(instance) if state else None
        if stop and stop["token"] != _token_mark(instance):
            # Человек вошёл заново (токен другой) — защёлка снята, начинаем с чистого.
            state["stopped"].pop(instance, None)
            stop = None
        recheck = None if stop or not state else state["suspect"].pop(instance, None)
    if recheck:
        # Перепроверка: офлайн-токен (сеть не нужна). Плохой — отказ подтверждён.
        verdict = token_preflight(instance)
        if not verdict["ok"]:
            note_failure(verdict["code"], instance, account, detail="recheck", checked=True)
        else:
            with _LOCK:
                state = _BATCHES.get(batch_id)
                if state is not None and instance not in state["stopped"]:
                    # Токен цел — этот старт и есть проверка сессии MLX.
                    state["probing"][instance] = recheck
        with _LOCK:
            state = _BATCHES.get(batch_id)
            stop = state["stopped"].get(instance) if state else None
    if not stop:
        return None
    code = stop["code"]
    record_event(code, instance, account, skipped=True)
    # Причина пространства своя у каждого слота и нужна на строке аккаунта целиком.
    message = mismatch_message(instance) if code == WORKSPACE_MISMATCH else SLOT_SKIP_MESSAGE
    return {"ok": False, "code": code, "message": message, "skipped": True}


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
    return message_for(code, slot), code
