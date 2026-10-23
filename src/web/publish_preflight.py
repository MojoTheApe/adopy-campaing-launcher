"""Проверка аккаунтов до «Publish all» (ACL-1604, стабильная сборка, этап 0.3).

05.10 несколько аккаунтов отвалились через час прогона по причинам, которые
были видны заранее: 5265 разлогинен, у 9403A страница проверки безопасности,
Elf403 открыт руками. Здесь — быстрый разбор до постановки заданий в очередь:
готовые идут, неготовые перечисляются с точной причиной и тем, что делать.

Правила владельца, которые модуль не обходит:

* **Профили не открываются.** Проверка читает только то, что уже известно:
  карту аккаунтов, срок токена Multilogin (по самому токену, без сети),
  последнее известное состояние кабинета (`account_spend.get_status`), недавние
  сигналы телеметрии прошлых прогонов (`profile_logged_out`, `profile_network`,
  `profile_restart_failed`, `profile_hand_window` с отказом) и список уже
  открытых профилей от лаунчера (локальный GET, профиль не трогается).
  Ни скриншотов, ни перезапусков, ни обращений к Meta.
* **Один разбор на нажатие**, только по аккаунтам, которые ставятся в очередь.
  Таймеров и опроса нет.
* Блокирует только то, что запуститься не может вовсе: нет в карте / нет
  профиля, мёртвый токен Multilogin, кабинет disabled/restricted. Сигналы
  прошлых прогонов (вход, сеть, перезапуск, чекпоинт) — заметки «will try»
  (решение владельца 06.10): робот пробует, а разлогиненное окно откроется
  впереди (`browser.front_for_login`).
* Окно, открытое руками, — не «заблокирован»: робот мягко переоткроет его сам
  (ACL-1562/1606). В списке такой профиль — заметка «will be reopened».
* Чего без открытия профиля увидеть НЕЛЬЗЯ: вышел ли аккаунт из Facebook
  после последнего прогона, появился ли чекпоинт, пока профиль был закрыт.
  Эти случаи ловятся только если они уже попали в телеметрию/статус; иначе
  первый заход робота остановит аккаунт `profile_logged_out`, как и раньше.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

#: Сколько часов сигнал прошлого прогона показывается заметкой. На запуск он
#: не влияет (с 06.10 не блокирует): только чтобы не пугать старым.
SIGNAL_TTL_H = 12.0
ENV_TTL = "ACL_PREFLIGHT_TTL_H"
#: Статус кабинета старше этого — не блок, а заметка.
STATUS_FRESH = timedelta(hours=24)

# Устойчивые коды причин.
NOT_IN_MAP = "not_in_map"
NO_PROFILE = "no_profile"
NO_TOKEN = "no_token"
TOKEN_EXPIRED = "token_expired"
LOGGED_OUT = "logged_out"
NETWORK = "network"
CHECKPOINT = "checkpoint"
RESTART_FAILED = "restart_failed"
HAND_WINDOW_STUCK = "hand_window_stuck"
DISABLED = "disabled"
RESTRICTED = "restricted"
# Не блокируют.
OPEN_NOW = "open_now"
REVIEW = "review"

#: Что сказать человеку (English — это видно в дешборде): причина и что делать.
MESSAGES: dict[str, tuple[str, str]] = {
    NOT_IN_MAP: ("not in the account map", "connect the account in the Accounts table"),
    NO_PROFILE: ("no Multilogin profile is set", "add its Multilogin profile in the Accounts table"),
    NO_TOKEN: ("Multilogin is not connected for this account's workspace",
               "connect Multilogin (Sign in to Multilogin)"),
    TOKEN_EXPIRED: ("the Multilogin token has expired", "sign in to Multilogin again"),
    # Прошлые сигналы — заметки, а не блок (решение владельца 06.10): робот всё
    # равно пробует; если аккаунт и вправду разлогинен, окно откроется впереди.
    LOGGED_OUT: ("logged out last time", "will try; the window will open in front if still logged out"),
    NETWORK: ("the profile's network failed last time", "will try; check the proxy in Multilogin if it fails again"),
    CHECKPOINT: ("security checkpoint last time",
                 "will try; the window will open in front if the check is still there"),
    RESTART_FAILED: ("the profile could not be restarted last time", "will try"),
    HAND_WINDOW_STUCK: ("the window opened by hand could not be reopened last time", "will try"),
    DISABLED: ("the ad account is disabled by Meta", "resolve it in Meta, or skip this account"),
    RESTRICTED: ("the ad account is restricted by Meta", "resolve it in Meta, or skip this account"),
    OPEN_NOW: ("the profile is open now", "the robot will reuse it or gently reopen it"),
    REVIEW: ("the ad account is under review by Meta", "drafts can still be built"),
}

#: Шаги телеметрии → код причины.
_STEP_CODES = {
    "profile_logged_out": LOGGED_OUT,
    "profile_network": NETWORK,
    "profile_restart_failed": RESTART_FAILED,
}
#: Признаки чекпоинта в тексте (статус кабинета, заметка/диагностика строки).
_CHECKPOINT_RE = re.compile(
    r"checkpoint|security check|confirm your identity|account (?:is )?(?:temporarily )?locked"
    r"|we suspect.{0,30}hack|hacked|compromised|protect your account|secure your account",
    re.I)
#: Свои строки проверки успехом профиля не считаются.
_OWN_STEPS = ("publish_preflight", "publish_preflight_decision")


@dataclass
class Reason:
    code: str
    message: str
    hint: str
    seen_at: str = ""          # ISO UTC; пусто — не из журнала
    seen_label: str = ""       # «05.10 14:02» (местное время)
    source: str = ""           # map | token | telemetry | status | launcher

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint,
                "seen_at": self.seen_at, "seen_label": self.seen_label, "source": self.source}

    def text(self) -> str:
        out = self.message
        if self.seen_label:
            out += f" (seen {self.seen_label})"
        return out


@dataclass
class Verdict:
    account: str
    blocking: list[Reason] = field(default_factory=list)
    notes: list[Reason] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {"account": self.account, "ready": self.ready,
                "reasons": [r.to_dict() for r in self.blocking],
                "notes": [r.to_dict() for r in self.notes]}


def account_key(name: Any) -> str:
    """Имя аккаунта без хвоста «- Имя» и id профиля, в нижнем регистре."""
    s = str(name or "").split(" - ")[0].strip()
    s = re.sub(r"_\d{10,}", "", s)
    return s.lower()


def _reason(code: str, source: str, ts: str = "") -> Reason:
    message, hint = MESSAGES[code]
    return Reason(code, message, hint, ts, label_of(ts), source)


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def label_of(ts: str) -> str:
    """«05.10 14:02» — местное время машины, как его видит владелец."""
    parsed = parse_ts(ts)
    if parsed is None:
        return ""
    return parsed.astimezone().strftime("%d.%m %H:%M")


def ttl_hours() -> float:
    try:
        return max(0.0, float(os.environ.get(ENV_TTL) or SIGNAL_TTL_H))
    except ValueError:
        return SIGNAL_TTL_H


def token_state(token: str, *, now: float | None = None) -> str:
    """"ok" | "missing" | "expired" — по самому токену, без обращения в сеть.

    Токен автоматизации Multilogin — JWT; срок читается из `exp`. Не JWT или
    без `exp` — считаем живым: настоящий отказ придёт при подключении.
    """
    token = (token or "").strip()
    if not token:
        return "missing"
    parts = token.split(".")
    if len(parts) != 3:
        return "ok"
    try:
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body.encode())).get("exp")
    except Exception:
        return "ok"
    if isinstance(exp, (int, float)) and exp <= (now if now is not None else time.time()):
        return "expired"
    return "ok"


def _is_outcome(step: str, ok: Any) -> bool:
    try:
        from src.web.robot_health import is_outcome_step

        return is_outcome_step(step, ok)
    except Exception:
        return False


def signals_from_telemetry(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Сигналы по аккаунтам: {account_key: [{code, ts, ok_after}…]} по возрастанию.

    Строка телеметрии несёт аккаунт не всегда — только `job_phase` и некоторые
    другие. Остальные строки задания получают аккаунт по `job`. Строка без
    аккаунта и без задания не приписывается никому.
    """
    rows = [r for r in rows if isinstance(r, dict)]
    job_account: dict[str, str] = {}
    for r in rows:
        diag = r.get("diag")
        if r.get("job") and isinstance(diag, dict) and diag.get("account"):
            job_account.setdefault(str(r["job"]), account_key(diag["account"]))
    out: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda x: str(x.get("ts") or "")):
        step = str(r.get("step") or "")
        diag = r.get("diag") if isinstance(r.get("diag"), dict) else {}
        who = account_key(diag.get("account")) if diag.get("account") else job_account.get(str(r.get("job") or ""), "")
        if not who:
            continue
        ts = str(r.get("ts") or "")
        code = ""
        if step in _STEP_CODES and r.get("ok") is False:
            code = _STEP_CODES[step]
        elif step == "profile_hand_window" and r.get("ok") is False and "restart_failed" in str(r.get("note") or ""):
            code = HAND_WINDOW_STUCK
        if not code and r.get("ok") is False:
            text = " ".join([step, str(r.get("note") or ""), str(diag.get("detail") or ""),
                             str(diag.get("message") or "")])
            if _CHECKPOINT_RE.search(text):
                code = CHECKPOINT
        if code:
            out.setdefault(who, []).append({"code": code, "ts": ts})
        elif r.get("ok") is True and step not in _OWN_STEPS and _is_outcome(step, True):
            # Исход шага мастера после трудности снимает её: робот в этом
            # профиле уже работал, значит, вход и сеть были в порядке.
            out.setdefault(who, []).append({"code": "", "ts": ts})
    return out


def classify(account: str, *, entry: Any = None, token: str | None = None,
             signals: list[dict[str, Any]] | None = None, status: dict[str, str] | None = None,
             open_profile: bool = False, now: datetime | None = None,
             ttl_h: float | None = None) -> Verdict:
    """Годен ли аккаунт к постановке в очередь — по уже известным данным.

    Чистая функция: все источники приходят аргументами, ничего не открывается.
    `token=None` — токен не проверяли (нет записи в карте).
    """
    v = Verdict(account)
    now = now or datetime.now(timezone.utc)
    ttl = timedelta(hours=ttl_hours() if ttl_h is None else ttl_h)

    if entry is None:
        v.blocking.append(_reason(NOT_IN_MAP, "map"))
        return v
    if not (getattr(entry, "profile_id", "") or "").strip():
        v.blocking.append(_reason(NO_PROFILE, "map"))
    if token is not None:
        state = token_state(token)
        # Профиль уже открыт с портом автоматизации: подключение (attach-only)
        # токена не требует, так что это заметка, а не блок.
        bucket = v.notes if open_profile else v.blocking
        if state == "missing":
            bucket.append(_reason(NO_TOKEN, "token"))
        elif state == "expired":
            bucket.append(_reason(TOKEN_EXPIRED, "token"))

    # Последний сигнал каждого вида — если после него не было успеха и он свежий.
    last_ok = ""
    latest: dict[str, str] = {}
    for sig in signals or []:
        if not sig.get("code"):
            last_ok = sig.get("ts") or last_ok
        else:
            latest[sig["code"]] = sig.get("ts") or ""
    status = status or {}
    # Чтение статуса кабинета, которое прошло позже, — тоже доказательство
    # входа: аккаунт был виден в Ads Manager.
    status_at = str(status.get("at") or "")
    seen_alive = max([x for x in (last_ok, status_at) if x] or [""])
    for code, ts in sorted(latest.items(), key=lambda kv: kv[1]):
        when = parse_ts(ts)
        if when is not None and now - when > ttl:
            continue
        if seen_alive and ts and seen_alive > ts:
            continue
        v.notes.append(_reason(code, "telemetry", ts))

    kind = (status.get("status") or "").lower()
    text = status.get("status_text") or ""
    if kind in ("disabled", "restricted"):
        code = DISABLED if kind == "disabled" else RESTRICTED
        read_at = parse_ts(status_at)
        old = bool(status.get("stale")) or (read_at is not None and now - read_at > STATUS_FRESH)
        if old:
            # Старое чтение: кабинет мог быть возвращён в работу — заметка.
            r = _reason(code, "status", status_at)
            day = status.get("at_short") or (read_at.astimezone().strftime("%d.%m") if read_at else "")
            r.message = f"was {kind} at the last read" + (f" ({day})" if day else "")
            r.hint = "will try; check it in Meta if it fails"
            r.seen_label = ""
            v.notes.append(r)
        else:
            v.blocking.append(_reason(code, "status", status_at))
    elif kind == "review":
        v.notes.append(_reason(REVIEW, "status", status_at))
    if kind not in ("clear", "") and _CHECKPOINT_RE.search(text) and not any(r.code == CHECKPOINT for r in v.notes):
        v.notes.append(_reason(CHECKPOINT, "status", status_at))

    if open_profile:
        v.notes.append(_reason(OPEN_NOW, "launcher"))
    return v


# ── сбор данных (единственное место, где что-то читается) ─────────────────────

def _default_token(instance: str) -> str:
    return (os.environ.get(f"MULTILOGIN_AUTOMATION_TOKEN_{(instance or 'A').strip().upper()}") or "").strip()


#: Сколько секунд ждём каждый источник списка открытых профилей: вся проверка
#: должна укладываться в 8 с таймаута страницы, а зависший лаунчер держит GET 4 с
#: на каждый слот (A и B подряд).
OPEN_PROFILES_WAIT_S = 2.5


def _default_open_profile_ids() -> set[str]:
    """Открытые профили: локальный GET лаунчера (read-only) и просмотр процессов
    машины — параллельно, не дольше `OPEN_PROFILES_WAIT_S`. Процессы нужны ещё и
    тогда, когда токен мёртв и лаунчер не отвечает."""
    from concurrent.futures import ThreadPoolExecutor, wait

    def launcher() -> set[str]:
        from src.fb_ads_rpa.mlx_profiles import running_profiles_from_launcher

        return {p["profile_id"] for p in running_profiles_from_launcher() if p.get("profile_id")}

    def machine() -> set[str]:
        from src.fb_ads_rpa.mlx_profiles import open_profile_ids

        return {str(x) for x in open_profile_ids() if x}

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="acl-preflight")
    try:
        futures = [pool.submit(launcher), pool.submit(machine)]
        done, _ = wait(futures, timeout=OPEN_PROFILES_WAIT_S)
        found: set[str] = set()
        for f in done:
            try:
                found |= f.result()
            except Exception:
                pass
        return found
    finally:
        pool.shutdown(wait=False)


def _default_telemetry_rows() -> list[dict[str, Any]]:
    try:
        from src.web.publish_progress import _telemetry_path, tail_rows

        return tail_rows(_telemetry_path())
    except Exception:
        return []


def _default_status(account: str) -> dict[str, str]:
    try:
        from src.analysis_agent import account_spend

        return account_spend.get_status(account) or {}
    except Exception:
        return {}


def preflight(accounts: Iterable[str], *, amap: dict[str, Any] | None = None,
              resolve: Callable[[dict, str], Any] | None = None,
              token_for: Callable[[str], str] | None = None,
              telemetry_rows: Callable[[], list[dict[str, Any]]] | None = None,
              status_for: Callable[[str], dict[str, str]] | None = None,
              open_profile_ids: Callable[[], set[str]] | None = None,
              now: datetime | None = None) -> list[Verdict]:
    """Вердикты по аккаунтам, которые собираются поставить в очередь.

    Источники подменяются аргументами (тесты). Никаких обращений к Meta и
    Multilogin, кроме одного локального GET со списком открытых профилей.
    """
    # Источники ищутся при вызове, а не при определении: тесты подменяют модуль.
    token_for = token_for or _default_token
    telemetry_rows = telemetry_rows or _default_telemetry_rows
    status_for = status_for or _default_status
    open_profile_ids = open_profile_ids or _default_open_profile_ids
    names = []
    for a in accounts:
        a = str(a or "").strip()
        if a and a not in names:
            names.append(a)
    if resolve is None:
        from src.meta_campaign_agent.ad_account_map import resolve_account_entry as resolve
    signals = signals_from_telemetry(telemetry_rows())
    opened = open_profile_ids() if names else set()
    out = []
    for name in names:
        entry = resolve(amap or {}, name)
        token = None
        if entry is not None:
            token = token_for(getattr(entry, "multilogin_instance", "") or "A")
        pid = (getattr(entry, "profile_id", "") or "").strip() if entry is not None else ""
        out.append(classify(
            name, entry=entry, token=token, signals=signals.get(account_key(name), []),
            status=status_for(name), open_profile=bool(pid and pid in opened), now=now))
    return out


def summary(verdicts: list[Verdict]) -> dict[str, Any]:
    """Ответ эндпоинта: списки и готовая строка «Not ready: …» для экрана."""
    bad = [v for v in verdicts if not v.ready]
    line = ""
    if bad:
        line = "Not ready: " + "; ".join(
            f"{v.account} — " + ", ".join(r.text() for r in v.blocking) for v in bad)
    return {"accounts": [v.to_dict() for v in verdicts],
            "ready": [v.account for v in verdicts if v.ready],
            "not_ready": [v.account for v in bad],
            "text": line,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "opened_profiles": False}


def record(summary_: dict[str, Any], *, step: str = "publish_preflight",
           extra: dict[str, Any] | None = None) -> None:
    """Строка телеметрии с итогом проверки. Не поднимает исключений."""
    try:
        from src.fb_ads_rpa import run_telemetry as rt

        diag: dict[str, Any] = {
            "accounts": len(summary_.get("accounts") or []),
            "ready": len(summary_.get("ready") or []),
            "not_ready": [
                {"account": a["account"], "codes": [r["code"] for r in a["reasons"]]}
                for a in summary_.get("accounts") or [] if not a["ready"]],
        }
        diag.update(extra or {})
        rt.record(step, not summary_.get("not_ready"), note="preflight", diag=diag)
    except Exception:
        log.debug("preflight telemetry failed", exc_info=True)


def meta_rules_errors(
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    drafts: list[Any] | None = None,
) -> dict[str, list[str]]:
    """Нарушения правил Meta по кампаниям формы до загрузки (ACL-1781).

    Возвращает {имя кампании: [сообщения на English]}; пусто — всё проходит.
    Сети нет. Когда переданы `drafts` (путь RPA), проверяется то, что
    черновик реально публикует: его objective ("Leads" -> OUTCOME_LEADS).
    Цель оптимизации, billing event и CTA в RPA выбирает рецепт Ads Manager,
    черновик их не несёт — они не проверяются (это делает `check_job` для
    Graph API-задания). Без `drafts` слоты превращаются в задания (как в
    превью) и сверяются целиком. Сбой самой проверки (нет реестра, нет
    книги) кампанию не блокирует — пишется в лог.
    """
    try:
        from src.campaign_bridge import meta_rules

        ruleset = meta_rules.load_rules()
        out: dict[str, list[str]] = {}

        def _add(name: str, violations: list[Any]) -> None:
            texts = meta_rules.format_violations(violations)
            if texts:
                bucket = out.setdefault(name, [])
                bucket.extend(t for t in texts if t not in bucket)

        if drafts is not None:
            for draft in drafts:
                _add(
                    str(getattr(draft, "name", "") or ""),
                    meta_rules.check_job(
                        meta_rules.DraftView(draft), ruleset, skip_kinds=("required_field", "text_limit")
                    ),
                )
            return out

        from src.campaign_bridge.dashboard_publisher import slots_to_publish_jobs

        jobs = slots_to_publish_jobs(slots, account_label=account_label, country=country, preview_mode=True)
        for job in jobs:
            _add(job.campaign_name, meta_rules.check_job(job, ruleset))
        return out
    except Exception:
        log.exception("Meta rules check failed; campaigns are not blocked by it")
        return {}
