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
* место по памяти (ACL-1603) — закрытый профиль поднимается только после
  `publish_jobs._slots.acquire(...)` (та же очередь, `share_profile=True`).
  Профиль, который подняли мы, остаётся открытым (`fb_api` не останавливает
  профили никогда), поэтому место остаётся занятым через `keep` — его
  подберёт следующее задание на этот профиль или снимет человек;
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
from src.fb_api import profile as _fb_profile
from src.fb_api import telemetry as _tm
from src.fb_api.graph import (
    Checkpoint,
    GraphClient,
    GraphError,
    GraphStop,
    LoggedOut,
    PageFailed,
    PolicyBlocked,
    RateLimited,
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
COOLDOWN_S = {"rate_limited": 300.0, "checkpoint": 600.0, "policy_block": 1800.0}


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


def _money(amount: float, currency: str) -> str:
    return f"{amount:,.2f} {currency}".strip()


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

_COOLDOWN: dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()


def _cooldown_left(key: str) -> float:
    with _COOLDOWN_LOCK:
        until = _COOLDOWN.get(key, 0.0)
    return max(0.0, until - time.monotonic())


def _start_cooldown(key: str, stop: GraphStop) -> None:
    secs = COOLDOWN_S.get(stop.kind)
    if secs is None:
        return
    secs = max(secs, float(getattr(stop, "retry_after_s", None) or 0))
    with _COOLDOWN_LOCK:
        _COOLDOWN[key] = time.monotonic() + secs


# ───────────────────────── сессия: профиль → вкладка → клиент ─────────────────────────

class ApiReadError(RuntimeError):
    """Отказ чтения с текстом человеку и устойчивым кодом для экрана."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(_tm.scrub(message))
        self.code = code


@dataclass
class _Held:
    account: str
    slot_id: str = ""
    started_now: bool = False


@dataclass
class _Session:
    client: GraphClient
    act: str
    account: str
    notes: dict = field(default_factory=dict)


# Точки подмены в тестах: настоящий браузер здесь не нужен.
def _probe_local(profile_id: str) -> tuple[str, int | None]:
    return _fb_profile.local_debug_port(profile_id, profile_dir=_fb_profile.find_profile_dir(profile_id))


def _open_profile(profile_id: str, folder_id: str, instance: str):
    return _fb_profile.open_profile(profile_id, folder_id, instance)


def _open_tab(cdp_url: str):
    from src.fb_api.cdp import PageTab

    return PageTab.open(cdp_url)


def _make_graph(tab):
    from src.fb_api.token_capture import InPageGraph

    return InPageGraph(tab)


def _open_gated(entry, account: str, held: _Held):
    """Профиль с портом отладки; закрытый поднимается только с местом в очереди."""
    profile_id = (getattr(entry, "profile_id", "") or "").strip()
    if not profile_id:
        raise ApiReadError(f"{account} has no profile_id in the account map")
    folder_id = (getattr(entry, "folder_id", "") or "").strip()
    instance = (getattr(entry, "multilogin_instance", "") or "A").strip() or "A"

    gated = False
    kind, _port = _probe_local(profile_id)
    if kind == "none":
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
        if not (grant and grant.get("shared")):
            held.slot_id = slot_id        # своё место: отпустить или оставить в конце
        gated = True
        _as._set_wait_note(account, _as.STARTING_NOTE.format(account=account))
    try:
        opened = _open_profile(profile_id, folder_id, instance)
    finally:
        if gated:
            _as._set_wait_note(account, "")
    held.started_now = bool(getattr(opened, "started_now", False))
    return opened


def _settle_slot(held: _Held) -> None:
    """Место в очереди: профиль, поднятый нами, остаётся открытым — место тоже."""
    if not held.slot_id:
        return
    try:
        queue = _as._slot_queue()
        if held.started_now:
            queue.keep(held.slot_id, held.account)
        else:
            queue.release(held.slot_id)
    except Exception:
        log.exception("Could not settle the profile slot of %s", held.account)


@contextmanager
def _session(account: str):
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

    key = _as._profile_key(account)
    left = _cooldown_left(key)
    if left > 0:
        raise ApiReadError(
            f"Meta asked to slow down for {account}; waiting before the next read "
            f"(about {int(left // 60) + 1} min). Nothing was called.")

    held = _Held(account)
    tab = None
    try:
        try:
            opened = _open_gated(entry, account, held)
        except _fb_profile.ProfileError as exc:
            raise ApiReadError(str(exc), code=getattr(exc, "code", "")) from exc
        except ApiReadError:
            raise
        except Exception as exc:  # noqa: BLE001 — MLX/сеть: отказ с понятным текстом
            raise ApiReadError(
                f"{account}: the profile could not be opened with automation: {_tm.scrub(str(exc))}"
            ) from exc

        tab = _open_tab(opened.cdp_url)
        graph = _make_graph(tab)
        graph.install()
        graph.navigate(act, timeout=60.0)
        state = graph.wait_ready(timeout=READY_TIMEOUT_S)
        if state.get("kind") == "checkpoint":
            raise Checkpoint("the profile is on a Facebook checkpoint")
        if state.get("kind") == "login":
            raise LoggedOut("the profile is logged out of Facebook")
        if not state.get("has_token"):
            raise PageFailed("Ads Manager did not finish loading (no Graph session in the page); "
                             "the profile may be logged out")
        client = GraphClient(
            graph,
            min_interval_s=_env_number(MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL_S, 0.5, 10.0),
            max_calls=int(_env_number(MAX_CALLS_ENV, DEFAULT_MAX_CALLS, 4, 60)),
        )
        sess = _Session(client=client, act=act, account=account)
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
        _settle_slot(held)


def _failure(account: str, exc: BaseException) -> tuple[str, str]:
    """Исключение чтения → `(текст человеку, код для экрана)`."""
    if isinstance(exc, ApiReadError):
        return str(exc), exc.code
    if isinstance(exc, LoggedOut):
        return _as.logged_out_message(account), _as.LOGGED_OUT_CODE
    if isinstance(exc, Checkpoint):
        return (f"The {account} profile is on a Facebook checkpoint — solve it inside Multilogin, "
                "then Read again"), ""
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
    _tm.record(event)
    _as._record("api_read", ok, took_s,
                {"account": account, "kind": kind, "source": "api",
                 "calls": sess_notes.get("calls", 0), **({"stop": stop} if stop else {})})


def _report_stop(exc: BaseException, account: str) -> None:
    if isinstance(exc, GraphStop) and not isinstance(exc, LoggedOut):
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
                out.spend, out.currency = spend, currency
                out.how = f"graph api: insights {date_from}..{date_to}"
                out.state = "ok"
                # Дерево — после принятого спенда; его отказ спенд не отменяет.
                try:
                    out.objects, out.objects_how, out.objects_error, reached = _tree(
                        sess.client, sess.act, date_from, date_to, currency)
                    out.objects_read = reached
                    if not reached:
                        out.objects_how = ""
                    if sess.client.stopped is not None:
                        stop = sess.client.stopped.kind
                        _report_stop(sess.client.stopped, account)
                except Exception as exc:  # noqa: BLE001
                    out.objects_error = f"Campaign tree was not read: {_failure(account, exc)[0]}"
                    out.objects_read = False
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


# ───────────────────────── разбивка по странам ─────────────────────────

def _country_name(code: str) -> str:
    """Название страны, как пишет Reporting, если код известен; иначе сам код."""
    from src.analysis_agent.country_names import COUNTRY_CODES

    code = (code or "").strip().upper()
    for name, known in COUNTRY_CODES.items():
        if known == code:
            return name.title()
    return code


@_as.serialized_browser(key=_as._profile_key, reading=True)
def read_country_split_via_api(account: str, date_from: str, date_to: str = ""):
    """Разбивка спенда по странам через `insights?breakdowns=country`."""
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
                rows = sess.client.insights_range(
                    sess.act, date_from, date_to, breakdowns="country",
                    fields="spend,account_currency", max_pages=3)
                if "level=account" in _truncated(sess.client):
                    raise ApiReadError("The country list is partial (page limit reached); nothing was taken.")
                fallback = str(acc.get("currency") or "")
                parsed = []
                for row in rows:
                    code = str(row.get("country") or "").strip()
                    if not code:
                        raise ApiReadError("A row of the country report has no country; nothing was taken.")
                    parsed.append({"country": _country_name(code), "spend": round(_num(row.get("spend")), 2),
                                   "currency": str(row.get("account_currency") or fallback)})
                parsed.sort(key=lambda r: -r["spend"])
                out.rows = parsed
                out.state = "ok" if parsed else "empty"
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
