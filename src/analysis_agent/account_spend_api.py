"""Чтение спенда, состояния кабинета и дерева через Graph API внутри профиля (ACL-1760).

Включается `ACL_FB_BACKEND=api` в `.env` машины (по умолчанию `rpa`). Тогда
«Read spend» / «Read all spend» и разбивка по странам идут не кликами по Ads
Manager, а GET-запросами `fetch()` из своей вкладки профиля (`src/fb_api`,
ACL-1756). Ответ — ровно тот же `SpendReading` / `CountrySplit`, что отдаёт
RPA, поэтому экран не меняется; отличается только `source: "api"`.

Что берётся там же, где у RPA-чтения, и не обходится:

* очередь и замок — вызов приходит из `account_spend.start_read` /
  `country_split.start_split`, то есть под `browser_operation.reading()` и
  замком профиля, как и RPA-чтение;
* место по памяти (ACL-1603) и жизнь профиля — как у RPA-чтения (ACL-1580):
  закрытый профиль поднимается только после `publish_jobs._slots.acquire(...)`
  (та же очередь, `share_profile=True`), а после чтения мягко останавливается
  ТОЛЬКО профиль, который поднял это чтение (`account_spend._finish_started` →
  `stop_profile_on_request`, `allow_kill=False`), и место отпускается — и при
  отказе тоже. Удачный Read spend, после которого ещё нужна разбивка стран,
  держит окно `CLOSE_GRACE_S`; страны подхватывают его (`_take_over_pending`).
  Уже работавший профиль, окно руками, профиль Publish не останавливаются;
* открытый профиль цепляем по его локальному порту отладки, без MLX и без
  очереди (как `attach_existing_only` у RPA); окно, открытое руками, не
  трогаем — отказ с кодом `open_by_hand`.

Лимиты: потолок вызовов на одно чтение (`ACL_FB_API_MAX_CALLS`) и пауза между
ними (`ACL_FB_API_MIN_INTERVAL_S`). Заголовков лимитов из страницы не видно,
поэтому опора — коды 4/17/32/613/80000–80014: после такого кода профиль на
время «остывает», новое чтение отказывает без единого вызова.

Токен сессии остаётся во вкладке: сюда приходят только JSON-ответы.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from src.analysis_agent import account_spend as _as
from src.fb_ads_rpa.operations import browser_operation
from src.fb_api import profile as _fb_profile
from src.fb_api import telemetry as _tm
from src.fb_api.graph import (
    CallBudget,
    Checkpoint,
    GraphClient,
    GraphError,
    GraphStop,
    LoggedOut,
    PageFailed,
    PolicyBlocked,
    RateLimited,
    WritesBlocked,
    WriteUnknown,
    normalize_act,
)

log = logging.getLogger(__name__)

BACKEND_ENV = "ACL_FB_BACKEND"
MAX_CALLS_ENV = "ACL_FB_API_MAX_CALLS"
MIN_INTERVAL_ENV = "ACL_FB_API_MIN_INTERVAL_S"

#: Сколько вызовов Graph одно чтение делает самое большее: кабинет, итог,
#: кампании, объявления, итоги по кампаниям и по объявлениям — с запасом на
#: страницы и одну пересъёмку токена.
DEFAULT_MAX_CALLS = 24
DEFAULT_MIN_INTERVAL_S = 1.0
#: Сколько ждать токен Ads Manager в своей вкладке.
READY_TIMEOUT_S = 90.0

#: Сколько секунд профиль «остывает» после остановки, чтобы не стучать снова.
#: Исчерпанный свой потолок вызовов (`call_budget`) сюда не входит: Meta молчала.
COOLDOWN_S = {"rate_limited": 300.0, "checkpoint": 600.0, "policy_block": 1800.0,
              "logged_out": 60.0, "page_unresponsive": 600.0}


def backend() -> str:
    """`api` или `rpa` (по умолчанию). Незнакомое значение — это `rpa`."""
    value = (os.environ.get(BACKEND_ENV) or "").strip().lower()
    return "api" if value == "api" else "rpa"


def api_enabled() -> bool:
    return backend() == "api"


def _env_number(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


# ───────────────────────── состояние кабинета ─────────────────────────

#: `account_status` кабинета → (слово колонки состояния, фраза человеку).
#: Слова — те же, что у RPA (`clear|disabled|restricted|review|unknown`).
_STATUS_MAP: dict[int, tuple[str, str, str]] = {
    1: ("clear", "ACTIVE", "Ad account is active"),
    2: ("disabled", "DISABLED", "Ad account is disabled"),
    3: ("restricted", "UNSETTLED", "Ad account is restricted: payment is unsettled"),
    7: ("review", "PENDING_RISK_REVIEW", "Ad account is under review"),
    8: ("review", "PENDING_SETTLEMENT", "Ad account is under review: settlement is pending"),
    9: ("restricted", "IN_GRACE_PERIOD", "Ad account is restricted: in a payment grace period"),
    100: ("disabled", "PENDING_CLOSURE", "Ad account is disabled: closure is pending"),
    101: ("disabled", "CLOSED", "Ad account is disabled: closed"),
    202: ("disabled", "ANY_CLOSED", "Ad account is disabled: closed"),
}

_DISABLE_REASONS = {
    1: "ADS_INTEGRITY_POLICY", 2: "ADS_IP_REVIEW", 3: "RISK_PAYMENT", 4: "GRAY_ACCOUNT_SHUT_DOWN",
    5: "ADS_AFC_REVIEW", 6: "BUSINESS_INTEGRITY_RAR", 7: "PERMANENT_CLOSE",
    8: "UNUSED_RESELLER_ACCOUNT", 9: "UNUSED_ACCOUNT", 10: "UMBRELLA_AD_ACCOUNT",
    11: "BUSINESS_MANAGER_INTEGRITY_POLICY", 12: "MISREPRESENTED_AD_ACCOUNT",
    13: "AOAB_DESHARE_LEGAL_ENTITY", 14: "CTX_THREAD_REVIEW", 15: "COMPROMISED_AD_ACCOUNT",
}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def map_status(account_status: Any, disable_reason: Any = None) -> tuple[str, str]:
    """`/act_X` `account_status` + `disable_reason` → `(status, status_text)`.

    Неизвестный код — `unknown` со словами Meta, а не `clear`: незнание не
    выдаём за проверку (правило `_read_status`).
    """
    code = _as_int(account_status)
    if code is None:
        return "unknown", ""
    row = _STATUS_MAP.get(code)
    if row is None:
        return "unknown", f"Meta API returned an unrecognised account_status {code}"
    status, word, text = row
    reason = _as_int(disable_reason)
    tail = f" (Meta API: {word}"
    if status != "clear" and reason:
        tail += f", reason {_DISABLE_REASONS.get(reason, reason)}"
    return status, text + tail + ")"


# ───────────────────────── дерево ─────────────────────────

#: `effective_status` → слово колонки Delivery. Слова подобраны так, чтобы
#: экран (`/off|review|reject|not deliver|error/`) красил остановленное.
_DELIVERY = {
    "ACTIVE": "Active",
    "PAUSED": "Off",
    "CAMPAIGN_PAUSED": "Campaign off",
    "ADSET_PAUSED": "Ad set off",
    "PENDING_REVIEW": "In review",
    "IN_PROCESS": "In review",
    "PREAPPROVED": "In review",
    "DISAPPROVED": "Rejected",
    "WITH_ISSUES": "Active, with issues",
    "PENDING_BILLING_INFO": "Error: billing info needed",
}

LEAD_ACTIONS = ("lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead")


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


#: Знак, которым Ads Manager подписывает сумму (RPA записывал его, а не код:
#: `_written_currency`, ACL-1760 live 10.10). Только четыре знака, которые
#: знают `money_format.fmt_currency` и `amapMoney`; остальное остаётся кодом
#: (`BRL 21.61`), а `R$`/`MX$` экран не знает бы и показал «currency unknown».
_SIGNS = {"USD": "$", "EUR": "€", "GBP": "£", "ILS": "₪"}


def display_currency(code: Any) -> str:
    """ISO-код из Graph → то, что хранило RPA-чтение: знак или код как есть."""
    text = str(code or "").strip().upper()
    return _SIGNS.get(text, text)


def _money(amount: float, currency: str) -> str:
    """«$100.00» / «BRL 100.00» — как пишет Ads Manager; число разбирает `amapMoneyIn`."""
    shown = display_currency(currency)
    if shown in _SIGNS.values():
        return f"{shown}{amount:,.2f}"
    return f"{shown} {amount:,.2f}".strip()


def _leads(row: dict) -> int:
    """Лиды строки: `lead` — общий счёт, если его нет — самый большой из частных."""
    by_kind: dict[str, int] = {}
    for action in row.get("actions") or []:
        if isinstance(action, dict) and str(action.get("action_type") or "") in LEAD_ACTIONS:
            by_kind[str(action["action_type"])] = int(_num(action.get("value")))
    if "lead" in by_kind:
        return by_kind["lead"]
    return max(by_kind.values()) if by_kind else 0


def _object(level: str, row: dict, ins: dict | None, currency: str, parent_id: str = "") -> dict:
    status = str(row.get("status") or "").upper()
    effective = str(row.get("effective_status") or "").upper()
    spend = results = cost = ""
    if ins:
        amount = _num(ins.get("spend"))
        spend = _money(amount, currency)
        leads = _leads(ins)
        if leads:
            results = f"{leads} Leads"
            cost = _money(amount / leads, currency) + " per lead"
    return _as.AdObject(
        level=level,
        id=str(row.get("id") or ""),
        name=_as.clean_object_name(row.get("name")),
        switch="on" if status == "ACTIVE" else "off" if status == "PAUSED" else "unknown",
        switch_label=f"Meta status {status}" if status else "",
        delivery=_DELIVERY.get(effective, effective.replace("_", " ").capitalize()),
        spend=spend,
        results=results,
        cost_per_result=cost,
        id_from="graph api",
        parent_id=parent_id,
    ).to_dict()


def build_objects(campaigns: list[dict], ads: list[dict], campaign_ins: dict[str, dict],
                  ad_ins: dict[str, dict], currency: str) -> list[dict]:
    """Дерево той же формы, что `_read_tree`: кампании, потом объявления по родителям."""
    out = [_object("campaign", c, campaign_ins.get(str(c.get("id"))), currency) for c in campaigns]
    order = {str(c.get("id")): i for i, c in enumerate(campaigns)}
    shown = [a for a in ads if str(a.get("campaign_id") or "") in order]
    shown.sort(key=lambda a: order[str(a.get("campaign_id"))])
    out += [_object("ad", a, ad_ins.get(str(a.get("id"))), currency, parent_id=str(a.get("campaign_id")))
            for a in shown]
    return out


# ───────────────────────── охлаждение после лимита ─────────────────────────

_COOLDOWN: dict[str, tuple[float, str]] = {}
_COOLDOWN_LOCK = threading.Lock()


def _cooldown_state(key: str) -> tuple[float, str]:
    """`(сколько секунд ещё остывать, по какой причине)`."""
    with _COOLDOWN_LOCK:
        until, kind = _COOLDOWN.get(key, (0.0, ""))
    return max(0.0, until - time.monotonic()), kind


def _cooldown_left(key: str) -> float:
    return _cooldown_state(key)[0]


def _launch_note_session(profile_id: str, state: dict, account: str) -> None:
    """ACL-2117: телеметрия — была ли сессия Graph на первой странице после переоткрытия."""
    try:
        from src.fb_api import launch as _launch

        _launch.note_graph_session(profile_id, bool(state.get("has_token")), account=account)
    except Exception:  # noqa: BLE001
        pass


def _start_cooldown(key: str, stop: GraphStop) -> None:
    secs = COOLDOWN_S.get(stop.kind)
    if secs is None:
        return
    secs = max(secs, float(getattr(stop, "retry_after_s", None) or 0))
    with _COOLDOWN_LOCK:
        _COOLDOWN[key] = (time.monotonic() + secs, stop.kind)


# ───────────────────────── сессия: профиль → вкладка → клиент ─────────────────────────

class ApiReadError(RuntimeError):
    """Отказ чтения с текстом человеку и устойчивым кодом для экрана."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(_tm.scrub(message))
        self.code = code


@dataclass
class _Session:
    client: GraphClient
    act: str
    account: str
    notes: dict = field(default_factory=dict)
    #: Профиль, который это чтение подняло, держится ещё `CLOSE_GRACE_S` после
    #: удачного чтения: за ним придёт чтение стран (ACL-1580).
    keep_next: bool = False
    #: ACL-2117: сколько секунд держать такое окно (вместо `CLOSE_GRACE_S`); None — обычно.
    keep_grace_s: float | None = None
    #: ACL-2117: keep-open включён — окно остаётся «удобным» kept-окном без своего таймера.
    keep_convenience: bool = False
    #: Immutable actual producer identity; never inferred from a later map read.
    profile_id: str = ""


# Точки подмены в тестах: настоящий браузер здесь не нужен.
def _probe_local(profile_id: str) -> tuple[str, int | None]:
    # Браузер, переживший нашу недавнюю остановку, — "closing": к его порту не цепляемся.
    return _fb_profile.probe_local(profile_id)


def _open_profile(profile_id: str, folder_id: str, instance: str):
    return _fb_profile.open_profile(profile_id, folder_id, instance)


def _open_tab(cdp_url: str):
    from src.fb_api.cdp import PageTab

    return PageTab.open(cdp_url)


def _make_graph(tab, *, commands_act: str | None = None):
    from src.fb_api.token_capture import InPageGraph

    if commands_act:
        # ACL-1800: правка существующих объектов — отдельный режим записи.
        return InPageGraph(tab, allow_object_updates=True, write_act=commands_act)
    return InPageGraph(tab)


def _open_gated(entry, account: str, held: "_as._ReadOpen"):
    """Профиль с портом отладки; закрытый поднимается только с местом в очереди.

    Что чтение подняло само, отмечается в `held` (`_ReadOpen`, как у RPA):
    после чтения `_finish_started` мягко остановит ровно его и отпустит место.
    Профиль, который уже работал, был открыт руками или делится с Publish,
    `held` не получает — его чтение не закроет.
    """
    profile_id = (getattr(entry, "profile_id", "") or "").strip()
    if not profile_id:
        raise ApiReadError(f"{account} has no profile_id in the account map")
    folder_id = (getattr(entry, "folder_id", "") or "").strip()
    instance = (getattr(entry, "multilogin_instance", "") or "A").strip() or "A"

    queue = None
    slot_id = ""
    shared = False
    gated = False
    kind, _port = _probe_local(profile_id)
    if kind == "none":
        # ACL-1812 (ревью #813, B1): закрыли недавно — не поднимаем, и в очередь за местом не встаём.
        from src.fb_api import stop_log as _stop_log

        # ACL-2117: ждём, пока MLX досохранит профиль после остановки; отказ — только если ждать слишком долго.
        why = _stop_log.wait_or_refusal(profile_id, on_wait=lambda text: _as._set_wait_note(account, text))
        if why:
            raise ApiReadError(why, code="restart_cooldown")
        # Профиль не работает: его поднимет MLX — значит, нужно место по памяти
        # (ACL-1603), та же очередь, что у RPA-чтений и Publish.
        queue = _as._slot_queue()
        slot_id = f"read-api:{account}:{uuid.uuid4().hex[:8]}"
        try:
            grant = queue.acquire(
                slot_id, profile_id,
                on_wait=lambda verdict: _as._set_wait_note(account, _as._slot_wait_note(verdict)),
                share_profile=True,
            )
        finally:
            _as._set_wait_note(account, "")
        shared = bool(grant and grant.get("shared"))
        gated = True
        # Ожидание места долгое, а профиль мог открыть кто-то ещё (Publish,
        # человек): тогда он не наш, место отпускаем (ревью PR #583, F2).
        if _probe_local(profile_id)[0] != "none":
            if not shared:
                queue.release(slot_id)
            gated = False
    began = time.monotonic()
    try:
        if gated:
            _as._set_wait_note(account, _as.STARTING_NOTE.format(account=account))
        from src.fb_api import mlx_session as _mlx

        with _mlx.scope(account=account):
            opened = _open_profile(profile_id, folder_id, instance)
    except BaseException:
        if gated and not shared:
            try:
                queue.release(slot_id)
            except Exception:
                log.exception("Could not release the profile slot of %s", account)
        raise
    finally:
        if gated:
            _as._set_wait_note(account, "")

    if gated and getattr(opened, "started_now", False):
        # Подняли мы: чтение закроет профиль и отпустит место (если оно своё).
        held.started = not shared
        held.slot_id = "" if shared else slot_id
        held.profile_id = profile_id
        held.entry = entry
        held.started_mono = began
        try:
            held.grants_seen = int(queue.grants(profile_id))
        except Exception:
            held.grants_seen = 0
    else:
        if gated and not shared:
            queue.release(slot_id)           # профиль уже работал — место не нужно
        # Профиль, поднятый прошлым чтением и ждущий закрытия, закроет это чтение.
        _as._take_over_pending(profile_id, held)
    return opened


#: Что видит человек, пока действие ждёт, пока кончится чужое тяжёлое (ACL-1812).
WARM_WAIT_NOTE = "Waiting for another account action to finish"
WARM_MEMORY_NOTE = "Waiting for free memory"


def _warm_registry():
    from src.fb_api import warm

    return warm.registry()


@contextmanager
def _warm_session(account: str, act: str, key: str, profile_id: str, commands: bool):
    """Сессия поверх тёплой вкладки: замок профиля, ворота, проверка живости, клиент.

    Вкладку после действия НЕ закрываем и профиль НЕ останавливаем. Охлаждения,
    потолок вызовов и причины остановки — те же, что у обычного пути, и
    считаются на каждое действие.
    """
    from src.fb_api import warm as _warm

    mode = f"commands:{act}" if commands else "read"

    def make(tab):
        return _make_graph(tab, commands_act=act) if commands else _make_graph(tab)

    try:
        lease = _warm_registry().checkout(
            profile_id, account=account, act=act, mode=mode, make_graph=make,
            ready_timeout=READY_TIMEOUT_S,
            on_wait=lambda why: _as._set_wait_note(
                account, WARM_MEMORY_NOTE if why in ("below_reserve", "memory_low", "memory_unknown")
                else WARM_WAIT_NOTE))
    except _warm.WarmError as exc:
        raise ApiReadError(str(exc), code=exc.code) from exc
    except GraphStop as exc:
        _start_cooldown(key, exc)
        raise
    finally:
        _as._set_wait_note(account, "")
    stop: BaseException | None = None
    try:
        client = GraphClient(
            lease.graph,
            allow_writes=bool(commands),
            min_interval_s=_env_number(MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL_S, 0.5, 10.0),
            max_calls=int(_env_number(MAX_CALLS_ENV, DEFAULT_MAX_CALLS, 4, 60)),
        )
        sess = _Session(client=client, act=act, account=account, profile_id=profile_id)
        try:
            yield sess
        except GraphStop as exc:
            stop = exc
            raise
        finally:
            if client.stopped is not None:
                _start_cooldown(key, client.stopped)
                stop = stop or client.stopped
            sess.notes["calls"] = client.calls
    finally:
        lease.release(stop)


def _touch_kept_window(account: str) -> None:
    """ACL-2113: окно аккаунта использовали — оставленное «для удобства» окно не простаивает."""
    try:
        from src.web import publish_jobs

        publish_jobs.touch_kept(account)
    except Exception:
        pass


@contextmanager
def _session(account: str, *, commands: bool = False):
    """Сессия чтения; по её окончании срок простоя оставленного окна идёт с нуля (ACL-2113)."""
    from src.analysis_agent.account_refresh_state import read_session, target_activity

    try:
        # Вызывающий уже держит физическую аренду профиля. Порядок тот же
        # у чтения после команды/публикации: профиль, затем одна общая читалка.
        with target_activity(_as._profile_key(account)), read_session(priority=commands):
            with _open_session(account, commands=commands) as sess:
                yield sess
    finally:
        _touch_kept_window(account)


@contextmanager
def _open_session(account: str, *, commands: bool = False):
    """Профиль → вкладка → клиент. `commands=True` (ACL-1800) — клиент с правом
    ТОЛЬКО на правку существующих объектов (статус/бюджет/ставка)."""
    entry = _as._entry_for(account)
    if entry is None:
        raise ApiReadError(f"{account} is not in the account map")
    raw_act = (getattr(entry, "ad_account_id", "") or "").strip()
    if not raw_act:
        raise ApiReadError(f"{account} has no ad_account_id in the account map")
    try:
        act = normalize_act(raw_act)
    except ValueError as exc:
        raise ApiReadError(str(exc)) from exc

    if not (getattr(entry, "profile_id", "") or "").strip():
        raise ApiReadError(f"{account} has no profile_id in the account map")
    from src.analysis_agent.account_refresh_machine import verify_binding
    from src.fb_ads_rpa.profile_lease import physical_profile_key
    profile_id = entry.profile_id.strip()
    verify_binding(act, profile_id)
    # The entry is immutable. A later map lookup must not move this actual
    # session's cooldown to another physical profile.
    key = physical_profile_key(profile_id)
    left, why = _cooldown_state(key)
    if left > 0:
        mins = int(left // 60) + 1
        if why == "logged_out":
            raise ApiReadError(_as.logged_out_message(account), code=_as.LOGGED_OUT_CODE)
        if why == "checkpoint":
            raise ApiReadError(
                f"The {account} profile is on a Facebook checkpoint; Ads Manager is not reopened "
                f"for about {mins} min. Solve it inside Multilogin, then Read again. Nothing was called.")
        if why == "page_unresponsive":
            raise ApiReadError(
                f"The Ads Manager tab for {account} stopped answering on the last read; it is not "
                f"reopened for about {mins} min. Nothing was called.")
        raise ApiReadError(
            f"Meta asked to slow down for {account}; waiting before the next read "
            f"(about {mins} min). Nothing was called.")

    # ACL-1812: профиль, который человек держит «тёплым» (Open profiles for today),
    # работает через реестр: одна вкладка робота на все действия, без перезагрузки.
    if profile_id and _warm_registry().is_warm(profile_id):
        with _warm_session(account, act, key, profile_id, commands) as warm_sess:
            yield warm_sess
        return

    held = _as._ReadOpen(account)
    tab = None
    sess: _Session | None = None
    try:
        try:
            opened = _open_gated(entry, account, held)
        except _fb_profile.ProfileError as exc:
            raise ApiReadError(str(exc), code=getattr(exc, "code", "")) from exc
        except ApiReadError:
            raise
        except Exception as exc:  # noqa: BLE001 — MLX/сеть: отказ с понятным текстом
            from src.fb_api import mlx_session as _mlx

            text, code = _mlx.humanize(exc, "", account)   # ACL-1826: потеря сессии / вход в прокси
            if code:
                raise ApiReadError(text, code=code) from exc
            raise ApiReadError(
                f"{account}: the profile could not be opened with automation: {_tm.scrub(str(exc))}"
            ) from exc

        tab = _open_tab(opened.cdp_url)
        graph = _make_graph(tab, commands_act=act) if commands else _make_graph(tab)
        graph.install()
        try:
            graph.navigate(act, timeout=60.0)
            state = graph.wait_ready(timeout=READY_TIMEOUT_S)
        except GraphStop as exc:
            # Вкладка не ответила, браузер назвал причину (ACL-1761 F1): охлаждение и стоп.
            _start_cooldown(key, exc)
            raise
        page_stop: GraphStop | None = None
        _launch_note_session(getattr(opened, "profile_id", ""), state, account)
        if state.get("kind") == "checkpoint":
            page_stop = Checkpoint("the profile is on a Facebook checkpoint")
        elif state.get("kind") == "login":
            page_stop = LoggedOut("the profile is logged out of Facebook")
        elif not state.get("has_token"):
            raise PageFailed("Ads Manager did not finish loading (no Graph session in the page); "
                             "the profile may be logged out")
        if page_stop is not None:
            # Найдено на странице, ещё до клиента: охлаждение тоже (ревью S2).
            _start_cooldown(key, page_stop)
            raise page_stop
        client = GraphClient(
            graph,
            allow_writes=bool(commands),
            min_interval_s=_env_number(MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL_S, 0.5, 10.0),
            max_calls=int(_env_number(MAX_CALLS_ENV, DEFAULT_MAX_CALLS, 4, 60)),
        )
        sess = _Session(client=client, act=act, account=account, profile_id=profile_id)
        try:
            yield sess
        finally:
            if client.stopped is not None:
                _start_cooldown(key, client.stopped)
            sess.notes["calls"] = client.calls
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:
                log.exception("Could not close our tab for %s", account)
        # ACL-1580: профиль, который поднято этим чтением, закрывается мягкой
        # остановкой (и при отказе тоже) или ждёт `CLOSE_GRACE_S` под страны.
        try:
            _as._finish_started(held, keep_for_next=bool(sess and sess.keep_next),
                                grace_s=(sess.keep_grace_s if sess else None),
                                convenience=bool(sess and sess.keep_convenience))
        except Exception:
            log.exception("Could not finish the profile the read started (%s)", account)


def _failure(account: str, exc: BaseException) -> tuple[str, str]:
    """Исключение чтения → `(текст человеку, код для экрана)`."""
    if isinstance(exc, ApiReadError):
        return str(exc), exc.code
    from src.fb_ads_rpa.profile_lease import ProfileLeaseError

    if isinstance(exc, ProfileLeaseError):   # ACL-1853: отказ замка профиля уже написан словами
        return str(exc), ""
    if isinstance(exc, LoggedOut):
        return _as.logged_out_message(account), _as.LOGGED_OUT_CODE
    if isinstance(exc, Checkpoint):
        return (f"The {account} profile is on a Facebook checkpoint — solve it inside Multilogin, "
                "then Read again"), ""
    if isinstance(exc, CallBudget):
        return ("Read stopped: call budget reached. Meta did not limit us; "
                f"raise {MAX_CALLS_ENV} or read again. ({exc})"), "call_budget"
    if isinstance(exc, RateLimited):
        wait = f" Try again in about {int(exc.retry_after_s // 60) + 1} min." if exc.retry_after_s else ""
        return f"Meta rate limit reached; the read was stopped.{wait} ({exc})", ""
    if isinstance(exc, PolicyBlocked):
        return f"Meta refused the request and it was not retried: {exc}", ""
    if isinstance(exc, GraphError):
        if exc.code in (10, 100, 200, 273):
            return (f"This profile has no access to the ad account for {account} "
                    f"(Graph API error {exc.code}). Check the account map and the profile."), ""
        return f"Graph API error: {exc}", ""
    if isinstance(exc, GraphStop):
        return f"Graph API read stopped: {exc}", ""
    return f"{type(exc).__name__}: {_tm.scrub(str(exc))}", ""


def _telemetry(kind: str, account: str, ok: bool, took_s: float, sess_notes: dict,
               error: str = "", stop: str = "") -> None:
    event = {"event": "fb_api_read", "kind": kind, "source": "api", "ok": ok,
             "took_s": round(took_s, 1), "calls": sess_notes.get("calls", 0)}
    if error:
        event["error"] = error[:200]
    if stop:
        event["stop"] = stop
    trigger = sess_notes.get("trigger")
    if trigger:
        event["trigger"] = trigger  # ACL-2063: чем вызвано чтение (schedule/attention/...)
    _tm.record(event)
    _as._record("api_read", ok, took_s,
                {"account": account, "kind": kind, "source": "api",
                 "calls": sess_notes.get("calls", 0), **({"stop": stop} if stop else {}),
                 **({"trigger": trigger} if trigger else {})})


def _telemetry_skip(account: str, trigger: str, reason: str = "skipped_fresh") -> None:
    """ACL-2063: чтение пропущено (свежо) — отдельное событие, не шаг `api_read`.

    Счёт `api_read` остаётся числом настоящих открытий профиля.
    """
    _tm.record({"event": "fb_api_skip", "kind": reason, "source": "api", "trigger": trigger})
    _as._record("api_read_skip", True, 0.0, {"account": account, "reason": reason, "trigger": trigger})


def _report_stop(exc: BaseException, account: str) -> None:
    if isinstance(exc, GraphStop) and not isinstance(exc, (LoggedOut, CallBudget)):
        _tm.report_stop(exc.kind, str(exc), {"account": account, "code": exc.code, "source": "dashboard"})


# ───────────────────────── чтение спенда ─────────────────────────

def _check_account(row: dict, act: str) -> None:
    got = str(row.get("account_id") or "").strip()
    if got and got != act.removeprefix("act_"):
        raise ApiReadError("Graph API answered for a different ad account; the reading was refused.")


def _period_total(client: GraphClient, act: str, date_from: str, date_to: str,
                  fallback_currency: str) -> tuple[float, str]:
    rows = client.insights_range(act, date_from, date_to)
    row = rows[0] if rows else {}
    currency = str(row.get("account_currency") or fallback_currency or "")
    return round(_num(row.get("spend")), 2), currency


def _tree(client: GraphClient, act: str, date_from: str, date_to: str,
          currency: str) -> tuple[list[dict], str, str, bool]:
    """Кампании и объявления. Возвращает `(объекты, как, ошибка, дошли_до_списка)`."""
    errors: list[str] = []
    try:
        campaigns = client.shown_campaigns(act)
    except GraphStop as exc:
        return [], "", f"Campaign tree was not read: {_failure('', exc)[0]}", False
    except GraphError as exc:
        return [], "", f"Campaign tree was not read: {exc}", False
    if "/campaigns" in _truncated(client):
        errors.append("the campaign list is partial: the page limit was reached")

    ads: list[dict] = []
    camp_ins: dict[str, dict] = {}
    ad_ins: dict[str, dict] = {}
    steps = (
        ("ads", lambda: client.ads(act)),
        ("campaign spend", lambda: client.insights_range(
            act, date_from, date_to, level="campaign",
            fields="campaign_id,spend,actions", max_pages=3)),
        ("ad spend", lambda: client.insights_range(
            act, date_from, date_to, level="ad", fields="ad_id,spend,actions", max_pages=3)),
    )
    for name, call in steps:
        if not campaigns and name == "ads":
            continue
        try:
            got = call()
        except GraphStop as exc:
            errors.append(f"{name} were not read: {_failure('', exc)[0]}")
            break
        except GraphError as exc:
            errors.append(f"{name} were not read: {exc}")
            continue
        if name == "ads":
            ads = got
        elif name == "campaign spend":
            camp_ins = {str(r.get("campaign_id")): r for r in got}
        else:
            ad_ins = {str(r.get("ad_id")): r for r in got}
    truncated = _truncated(client)
    for path_key, label in (("/ads", "ad list"), ("level=campaign", "campaign spend"),
                            ("level=ad", "ad spend")):
        if path_key in truncated:
            errors.append(f"the {label} is partial: the page limit was reached")
    objects = build_objects(campaigns, ads, camp_ins, ad_ins, currency)
    how = f"graph api: {len(campaigns)} campaigns, {len([o for o in objects if o['level'] == 'ad'])} ads"
    return objects, how, "; ".join(errors), True


def _truncated(client: GraphClient) -> str:
    return " ".join(getattr(client, "truncated", []) or [])


# ───────────────────────── чтение спенда + страны ─────────────────────────

def _country_name(code: str) -> str:
    """Название страны, как пишет Reporting, если код известен; иначе сам код."""
    from src.analysis_agent.country_names import COUNTRY_CODES

    code = (code or "").strip().upper()
    for name, known in COUNTRY_CODES.items():
        if known == code:
            return name.title()
    return code


def _country_rows(client: GraphClient, act: str, date_from: str, date_to: str,
                  fallback_currency: str) -> list[dict]:
    """Строки `{country, spend, currency}` из `insights?breakdowns=country`.

    Неполный или нечитаемый список — отказ, как у RPA: половина стран выглядела
    бы целой.
    """
    before = len(getattr(client, "truncated", []) or [])
    rows = client.insights_range(act, date_from, date_to, breakdowns="country",
                                 fields="spend,account_currency", max_pages=3)
    # Только пометки этого чтения: страница, не взятая из-за usage или лимита, — «неполно».
    fresh = " ".join((getattr(client, "truncated", []) or [])[before:])
    if "level=account" in fresh:
        raise ApiReadError("The country list is partial (page limit reached); nothing was taken.")
    parsed = []
    for row in rows:
        code = str(row.get("country") or "").strip()
        if not code:
            raise ApiReadError("A row of the country report has no country; nothing was taken.")
        parsed.append({"country": _country_name(code), "spend": round(_num(row.get("spend")), 2),
                       "currency": str(row.get("account_currency") or fallback_currency)})
    parsed.sort(key=lambda r: -r["spend"])
    return parsed


def _split_needed(account: str, date_from: str, date_to: str, spend: float, currency: str) -> bool:
    """Нужна ли разбивка стран сейчас — тем же правилом, что у страницы (ACL-1282).

    Сбой вопроса — «нужна»: лишний вызов дешевле молча потерянной разбивки.
    """
    try:
        from src.analysis_agent import country_split
        from src.web import account_links

        countries = len({str(v).strip().casefold()
                         for v in account_links.values_for(account, "country") if str(v).strip()})
        why = country_split.why_split_is_needed(
            countries=countries, split=country_split.get_split(account, date_from, date_to),
            spend_finished_at=_as._now(), spend=spend, currency=currency)
        return bool(why)
    except Exception:  # noqa: BLE001
        return True


def _split_in_session(sess: _Session, out: "_as.SpendReading", acc: dict) -> None:
    """Разбивка стран в той же вкладке, что и спенд (ревью S1): второй загрузки нет."""
    from src.analysis_agent import country_split

    if not _split_needed(sess.account, out.date_from, out.date_to, out.spend or 0.0, out.currency):
        return
    split = country_split.CountrySplit(
        account=sess.account, date_from=out.date_from, date_to=out.date_to, source="api",
        status=out.status, status_text=out.status_text, started_at=_as._now())
    try:
        rows = _country_rows(sess.client, sess.act, out.date_from, out.date_to,
                             str(acc.get("currency") or out.currency))
        split.rows = rows
        split.state = "ok" if rows else "empty"
        split.how = f"graph api: insights breakdowns=country {out.date_from}..{out.date_to}"
    except Exception as exc:  # noqa: BLE001 — разбивка спенд не отменяет
        split.state, split.rows = "failed", []
        split.error, split.error_code = _failure(sess.account, exc)
        # Страница спросит страны отдельно: окно держим под это (ACL-1580).
        sess.keep_next = True
    # Какой спенд эта разбивка объясняет (ACL-1282).
    split.finished_at = split.spend_at = _as._now()
    split.spend, split.spend_currency = out.spend, out.currency
    try:
        country_split.save_split(split)
    except Exception:  # noqa: BLE001
        log.exception("Could not save the country split of %s", sess.account)


def read_via_api(account: str, date_from: str, date_to: str = "") -> "_as.SpendReading":
    """Спенд, состояние кабинета и дерево через Graph API. Тот же ответ, что у `read_via_rpa`."""
    date_to = date_to or date_from
    out = _as.SpendReading(account=account, date_from=date_from, date_to=date_to,
                           source="api", state="failed")
    started = time.monotonic()
    notes: dict = {}
    stop = ""
    try:
        with _session(account) as sess:
            try:
                acc = sess.client.account(sess.act)
                _check_account(acc, sess.act)
                out.status, out.status_text = map_status(acc.get("account_status"), acc.get("disable_reason"))
                spend, currency = _period_total(sess.client, sess.act, date_from, date_to,
                                                str(acc.get("currency") or ""))
                out.spend, out.currency = spend, display_currency(currency)
                out.how = f"graph api: insights {date_from}..{date_to}"
                out.state = "ok"
                # Дерево — после принятого спенда; его отказ спенд не отменяет.
                try:
                    out.objects, out.objects_how, out.objects_error, reached = _tree(
                        sess.client, sess.act, date_from, date_to, currency)
                    out.objects_read = reached
                    if not reached:
                        out.objects_how = ""
                except Exception as exc:  # noqa: BLE001
                    out.objects_error = f"Campaign tree was not read: {_failure(account, exc)[0]}"
                    out.objects_read = False
                # Страны — в той же вкладке, если они сейчас нужны (ревью S1).
                if sess.client.stopped is None:
                    _split_in_session(sess, out, acc)
                if sess.client.stopped is not None:
                    stop = sess.client.stopped.kind
                    _report_stop(sess.client.stopped, account)
            finally:
                notes = sess.notes
    except Exception as exc:  # noqa: BLE001 — чтение не должно ронять сервер
        if out.state != "ok":
            out.state = "failed"
            out.error, out.error_code = _failure(account, exc)
        if isinstance(exc, GraphStop):
            stop = exc.kind
            _report_stop(exc, account)
        if out.state == "ok":                       # спенд уже прочитан
            out.objects_error = out.objects_error or _failure(account, exc)[0]
    if out.state == "ok" and not out.objects_read and not out.objects_error:
        out.objects_error = "Campaign tree was not read"
    _telemetry("spend", account, out.state == "ok", time.monotonic() - started, notes,
               error=out.error, stop=stop)
    return out


# ───────────────────────── разбивка по странам, отдельно ─────────────────────────

def _split_lease_error(exc, account, date_from, date_to=""):
    from src.analysis_agent import country_split

    return country_split._lease_error(exc, account, date_from, date_to)  # noqa: SLF001


@_as.serialized_browser(key=_as._profile_key, reading=True, on_lease_error=_split_lease_error)
def read_country_split_via_api(account: str, date_from: str, date_to: str = ""):
    """Разбивка спенда по странам через `insights?breakdowns=country`.

    Отдельный путь: страница просит его, когда разбивка нужна, а в спенде она
    не прочиталась. Окно, оставленное спенд-чтением на `CLOSE_GRACE_S`,
    подхватывается (`_take_over_pending`), второй загрузки профиля нет.
    """
    from src.analysis_agent import country_split

    date_to = date_to or date_from
    out = country_split.CountrySplit(account=account, date_from=date_from, date_to=date_to,
                                     source="api")
    started = time.monotonic()
    notes: dict = {}
    stop = ""
    try:
        with _session(account) as sess:
            try:
                acc = sess.client.account(sess.act)
                _check_account(acc, sess.act)
                out.status, out.status_text = map_status(acc.get("account_status"), acc.get("disable_reason"))
                out.rows = _country_rows(sess.client, sess.act, date_from, date_to,
                                         str(acc.get("currency") or ""))
                out.state = "ok" if out.rows else "empty"
                out.how = f"graph api: insights breakdowns=country {date_from}..{date_to}"
            finally:
                notes = sess.notes
    except Exception as exc:  # noqa: BLE001
        out.state, out.rows = "failed", []
        out.error, out.error_code = _failure(account, exc)
        if isinstance(exc, GraphStop):
            stop = exc.kind
            _report_stop(exc, account)
    _telemetry("countries", account, out.state != "failed", time.monotonic() - started, notes,
               error=out.error, stop=stop)
    return out


# ───────────────────────── выключить кампанию по id (ACL-1945) ─────────────────────────
#
# Под `ACL_FB_BACKEND=api` выключение идёт не кликом по тумблеру в Ads Manager, а
# `POST /<id>` со `status=PAUSED` из своей вкладки профиля, потом чтение обратно.
# id известен всегда (дерево, прочитанное через API, даёт его каждой строке), поэтому
# одноимённые кампании различимы и разметка страницы Meta ни при чём.
#
# **Только выключение** (решение владельца 12.09): ACTIVE отсюда не ставится.
# Политика записи (`write_policy.check_object_update`) пускает только `/<id>` с одним
# полем; сам этот путь шлёт ровно `status=PAUSED` и больше ничего.

#: Отказ, пока в этом же профиле идёт сборка кампаний (тот же текст, что у ACL-1379).
PROFILE_BUSY_ERROR = "The robot is building campaigns in this profile — wait for it to finish."

#: Вызовов на одно выключение: чтение, запись, чтение обратно + запас на пересъёмку токена.
TURN_OFF_API_CALLS = 8
#: Пауза перед сверкой записи, на которую Meta не ответила: запрос мог ещё лететь.
TURN_OFF_RECONCILE_PAUSE_S = 4.0
#: `effective_status`, при котором PAUSED у самого объекта доставкой не подтверждён.
_STILL_RUNNING = {"ACTIVE", "WITH_ISSUES"}


def _api_result(account: str, level: str, object_id: str, expected_name: str,
                requester: str) -> "_as.TurnOffResult":
    return _as.TurnOffResult(account=account, level=level, object_id=str(object_id),
                             name=expected_name, at=_as._now(), requester=requester,
                             route="api", matched_by="id")


def _delivery_word(effective: str) -> str:
    return _DELIVERY.get(effective, effective.replace("_", " ").capitalize())


def _turn_off_in_session(out: "_as.TurnOffResult", sess: "_Session", parent_id: str,
                         expected_name: str, wrote: list) -> None:
    """Прочитать → записать PAUSED → прочитать обратно. Итог — в `out`.

    `wrote[0]` становится True до POST: после этого любой обрыв — `unconfirmed`,
    а не `failed` (объект мог и выключиться).
    """
    from src.fb_api import commands as _cmd

    client, level, oid = sess.client, out.level, out.object_id
    if not getattr(client, "allow_writes", False):
        out.error = "This session cannot write; nothing was changed."
        return
    client.reset_budget(TURN_OFF_API_CALLS)
    try:
        snap = _cmd.read_object(client, sess.act, level, oid)
    except _cmd.CommandRefused as exc:
        out.error = f"{exc} Nothing was changed."
        return
    except GraphStop:
        raise
    except GraphError as exc:
        out.error = f"Meta could not read this {level} (Graph error {exc.code}); nothing was changed."
        return
    if level == "campaign":
        # Поля чтения кампании есть и у группы объявлений: id группы прошёл бы как
        # кампания. `objective` бывает только у кампании.
        try:
            kind = client.get(f"/{oid}", {"fields": "objective"})
        except GraphStop:
            raise
        except GraphError as exc:
            out.error = f"Meta could not confirm this is a campaign (Graph error {exc.code}); nothing was changed."
            return
        if not str((kind or {}).get("objective") or "").strip():
            out.error = "Meta does not confirm that this id is a campaign; nothing was touched."
            return
    out.name = snap["name"]
    if expected_name and _as.clean_object_name(expected_name)[:160] != _as.clean_object_name(snap["name"])[:160]:
        out.error = (f"Meta shows \"{snap['name']}\" under this id, but the dashboard showed "
                     f"\"{expected_name}\"; nothing was touched. Read the account again.")
        return
    if level == "ad":
        # Объявление — только после того, как кампания-родитель подтверждена самим API.
        camp = next((p for p in snap["parents"] if p["level"] == "campaign"), None)
        if camp is None or camp["id"] != parent_id:
            out.error = ("Meta does not confirm that this ad belongs to the campaign shown; "
                         "nothing was touched. Read the account again.")
            return
    status = snap["status"]
    if status == "PAUSED":
        out.state = "already_off"
        out.delivery = _delivery_word(snap["effective_status"])
        out.how = "graph api: status is already PAUSED; nothing was sent"
        return
    if status != "ACTIVE":
        out.error = f"Status {status or 'unknown'} cannot be changed here; nothing was touched."
        return

    path = f"/{oid}"
    unknown = False
    wrote[0] = True
    try:
        client.post(path, {"status": "PAUSED"})
    except WriteUnknown:
        unknown = True
    except GraphStop:
        # Не ответила вкладка или Meta остановила нас: запись могла уйти только если
        # стоп пришёл после отправки, а `post` в этом случае бросает `WriteUnknown`.
        wrote[0] = False
        raise
    except GraphError as exc:
        wrote[0] = False                 # Meta ответила отказом: записи нет
        out.error = f"Meta refused: {exc}"[:240]
        return
    except (WritesBlocked, ValueError) as exc:
        wrote[0] = False                 # к Meta ничего не ушло
        out.error = f"Not sent: {exc}"[:240]
        return
    if unknown:
        time.sleep(TURN_OFF_RECONCILE_PAUSE_S)
    try:
        back = _cmd.read_object(client, sess.act, level, oid)
    except (GraphStop, GraphError, _cmd.CommandRefused) as exc:
        out.state = "unconfirmed"
        out.error = f"The change was sent but could not be read back: {exc}"[:300]
        return
    effective = back["effective_status"]
    out.delivery = _delivery_word(effective)
    if back["status"] == "PAUSED" and effective not in _STILL_RUNNING:
        if unknown:
            client.reconciled(path)
        out.state = "off"
        out.how = f"graph api: POST status=PAUSED, read back status=PAUSED effective={effective or '?'}"
    elif back["status"] == "PAUSED":
        out.state = "unconfirmed"
        out.error = (f"Meta saved PAUSED but still reports delivery {effective}; "
                     "read the account again to confirm.")
    elif unknown:
        out.state = "unconfirmed"
        out.error = "No answer from Meta and the read-back still shows it on; it may yet apply. Not retried."
    else:
        out.error = f"Meta accepted the change but the read-back shows status {back['status'] or 'unknown'}."


def _record_api_turn_off(out: "_as.TurnOffResult") -> "_as.TurnOffResult":
    if out.state in ("off", "already_off"):
        try:
            _as._mark_object_off(out.account, out.object_id, out.delivery)
        except Exception:
            pass
    _as._record_turn_off(out)
    return out


@_as.serialized_browser(key=_as._profile_key)
def _turn_off_locked(account: str, level: str, object_id: str, *, parent_id: str,
                     expected_name: str, requester: str, profile_busy=None) -> "_as.TurnOffResult":
    out = _api_result(account, level, object_id, expected_name, requester)
    # Под замком профиля спрашиваем ещё раз: публикация могла встать в очередь после
    # проверки маршрута. Не смогли спросить — считаем занятым.
    busy = False
    if profile_busy is not None:
        try:
            busy = bool(profile_busy(_as._profile_key(account)))
        except Exception:
            busy = True
    if busy:
        out.error = PROFILE_BUSY_ERROR
        return _record_api_turn_off(out)
    wrote = [False]
    try:
        # Читатель браузера: обновление программы видит запись в полёте (как у команд ACL-1800).
        with browser_operation.reading(), _session(account, commands=True) as sess:
            _turn_off_in_session(out, sess, parent_id, expected_name, wrote)
    except Exception as exc:  # noqa: BLE001 — любой исход оставляет след в журнале
        if out.state not in ("off", "already_off"):
            out.state = "unconfirmed" if wrote[0] else "failed"
            known = isinstance(exc, (GraphStop, ApiReadError, GraphError))
            out.error = (_failure(account, exc)[0] if known
                         else f"{type(exc).__name__}: {_tm.scrub(str(exc))}")[:300]
            if wrote[0]:
                out.error += " The change may have been applied; check Ads Manager. Nothing was retried."
        if isinstance(exc, GraphStop):
            _report_stop(exc, account)
    return _record_api_turn_off(out)


def turn_off(account: str, level: str, object_id: str, *, parent_id: str = "",
             expected_name: str = "", requester: str = "",
             profile_busy=None) -> "_as.TurnOffResult":
    """Выключить кампанию (или объявление) через Graph API по id и записать, что вышло.

    Отказы до записи — `failed`, и к Meta в них ничего не ушло. Журнал —
    `turn_off_log.jsonl`, запись с `route: api`, `matched_by: id`.
    """
    from src.fb_api import commands as _cmd

    who = (requester or "").strip()
    level, oid, parent = (level or "").strip(), str(object_id or "").strip(), str(parent_id or "").strip()
    out = _api_result(account, level, oid, expected_name, who)
    if level not in ("campaign", "ad"):
        out.error = f"only a campaign or an ad can be turned off, not {level!r}"
    elif not _cmd._ID_RE.fullmatch(oid):
        out.error = "object id must be a numeric Meta object id"
    elif level == "ad" and not _cmd._ID_RE.fullmatch(parent):
        out.error = "turning off an ad requires its campaign"
    if out.error:
        return _record_api_turn_off(out)
    return _turn_off_locked(account, level, oid, parent_id=parent, expected_name=expected_name,
                            requester=who, profile_busy=profile_busy)
