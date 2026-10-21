"""Graph API только на чтение — поверх `fetch()` во вкладке профиля.

Транспорт — `token_capture.InPageGraph`: запрос уходит с IP, отпечатком и
куками профиля, токен из вкладки не выходит.

Политика (решение владельца, ACL-1756):
- ни опроса, ни таймеров — вызов только по явному запросу; между вызовами
  пауза не меньше `min_interval_s`, всего не больше `max_calls`;
- 190 (и 102) — токен умер: ОДИН раз переснять во вкладке и повторить; снова
  190 — стоп «logged out / checkpoint»; 190 с подкодом checkpoint (459, 464) —
  стоп сразу, без перезагрузки;
- сбой сети при вызове — стоп, без повтора;
- 4/17/32/613/80000–80014 — лимит: не повторяем, стоп с подсказкой, сколько
  ждать (`estimated_time_to_regain_access`);
- 368 — блок по правилам: стоп сразу, никогда не повторяем;
- страница ушла на checkpoint/логин — стоп;
- заголовки `x-business-use-case-usage` / `x-ad-account-usage` /
  `x-app-usage`: от `SLOW_PCT` паузы втрое длиннее, от `STOP_PCT` — стоп
  до следующего вызова.

После любого стопа клиент больше не делает вызовов: решает человек.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from src.fb_api.telemetry import scrub

TOKEN_CODES = {102, 190}
RATE_CODES = {4, 17, 32, 613} | set(range(80000, 80015))
POLICY_CODES = {368}
#: Подкоды 190, при которых сессия стоит на checkpoint: перезагрузка Ads
#: Manager не поможет, только лишний заход в кабинет — стоп сразу (ревью O3).
CHECKPOINT_SUBCODES = {459, 464}

SLOW_PCT = 50.0
STOP_PCT = 80.0

_ACT_RE = re.compile(r"^(?:act_)?(\d{5,})$")
_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

#: Что показывает таблица Ads Manager по умолчанию: всё, кроме архива и удалённого.
SHOWN_STATES = ["ACTIVE", "PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED",
                "PENDING_BILLING_INFO", "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS",
                "WITH_ISSUES"]


class GraphError(RuntimeError):
    """Graph вернул ошибку, после которой можно продолжать (не стоп)."""

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None,
                 status: int | None = None, details: dict | None = None):
        super().__init__(scrub(message))
        self.code = code
        self.subcode = subcode
        self.status = status
        self.details = details or {}


def error_details(gerr: dict, reply: dict) -> dict:
    """Что Meta сказала об ошибке, плюс куда и как мы ходили — для разбора.

    Значения вычищаются сразу при сборке (ревью r5): `.details` уходит дальше
    как есть, и номера кабинетов, бизнесов и токены там не нужны.
    """
    out: dict[str, Any] = {}
    for key in ("type", "error_subcode", "fbtrace_id", "is_transient",
                "error_user_title", "error_user_msg"):
        value = gerr.get(key)
        if value not in (None, ""):
            out[key] = scrub(str(value)[:200])
    endpoint = reply.get("endpoint")
    if isinstance(endpoint, dict):
        for key in ("host", "version", "credentials"):
            if endpoint.get(key):
                out[key] = scrub(str(endpoint[key])[:80])
    if reply.get("status") is not None:
        out["http_status"] = str(reply.get("status"))
    return out


def _with_details(text: str, details: dict) -> str:
    if not details:
        return text
    return text + " [" + ", ".join(f"{k}={v}" for k, v in details.items()) + "]"


def _read_context(path: str, params: dict | None) -> dict[str, str]:
    """Only finite local categories may leave the request as diagnostics.

    Do not parse URLs, decode paths or copy parameters: malformed/unknown input
    stays unknown, rather than allowing a request value to become error text.
    """
    context = {"method": "GET", "operation": "unknown"}
    if not isinstance(path, str) or len(path) > 128:
        return context
    if path == "/search":
        context["operation"] = "search"
        search_type = params.get("type") if isinstance(params, dict) else None
        if isinstance(search_type, str) and search_type in {"adgeolocation", "adlocale", "adinterest"}:
            context["search_type"] = search_type
    elif path == "/me/adaccounts":
        context["operation"] = "adaccounts"
    elif match := re.fullmatch(r"/act_[0-9]{1,32}/(campaigns|promote_pages|adspixels|insights)", path):
        context["operation"] = match.group(1)
    elif re.fullmatch(r"/[0-9]{1,32}/leadgen_forms", path):
        context["operation"] = "leadgen_forms"
    return context


class GraphStop(RuntimeError):
    """Остановка: дальше этот клиент не зовёт Graph вовсе."""

    kind = "stopped"

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None,
                 retry_after_s: float | None = None):
        super().__init__(scrub(message))
        self.code = code
        self.subcode = subcode
        self.retry_after_s = retry_after_s


class LoggedOut(GraphStop):
    kind = "logged_out"


class Checkpoint(GraphStop):
    kind = "checkpoint"


class RateLimited(GraphStop):
    kind = "rate_limited"


class CallBudget(RateLimited):
    """Свой потолок вызовов на одно чтение исчерпан (ACL-1760).

    Meta ничего не говорила: это не её лимит, охлаждения профиля нет. Наследует
    `RateLimited`, чтобы прежние `except RateLimited` продолжали ловить стоп.
    """

    kind = "call_budget"


class PolicyBlocked(GraphStop):
    kind = "policy_block"


class PageFailed(GraphStop):
    kind = "page_failed"


class PageUnresponsive(GraphStop):
    """Вкладка Ads Manager перестала отвечать (ACL-1761): не логин и не checkpoint.

    Охлаждение профиля 10 минут, повторов нет.
    """

    kind = "page_unresponsive"


class WriteUnknown(RuntimeError):
    """Запись ушла (или могла уйти), а ответа нет (ACL-1761).

    Не повторяется никогда. Пока вызывающий не сверит чтением (`reconciled`),
    клиент не делает НИ ОДНОЙ новой записи; чтения разрешены — ими и сверяют.
    """

    def __init__(self, message: str, *, path: str = ""):
        super().__init__(scrub(message))
        self.path = path


class WritesBlocked(RuntimeError):
    """Новая запись не отправлена: у прошлой результат неизвестен и не сверен (ревью M4).

    Отдельно от `WriteUnknown`: эта запись к Meta точно НЕ уходила, сверять её
    нечего, и снимать флаг прошлой записи нельзя.
    """

    def __init__(self, path: str = ""):
        super().__init__(scrub("an earlier write has an unknown result and was not reconciled; "
                               "no further writes until it is"))
        self.path = path


#: Ответы Graph, после которых неизвестно, создан ли объект: «неизвестная
#: ошибка», «сервис временно недоступен», 5xx.
UNKNOWN_RESULT_CODES = {1, 2}
#: Отказы помощника страницы ДО отправки: к Meta ничего не ушло.
_NOT_SENT = {"write_refused", "read_only", "no_file", "bad_option", "no_fetch", "retired",
             "bad_path", "forbidden_param"}


def classify(code: int | None) -> str:
    if code in TOKEN_CODES:
        return "token"
    if code in RATE_CODES:
        return "rate_limit"
    if code in POLICY_CODES:
        return "policy"
    return "other"


def normalize_act(act_id: str) -> str:
    m = _ACT_RE.match((act_id or "").strip())
    if not m:
        raise ValueError("ad account id must look like act_<digits>")
    return f"act_{m.group(1)}"


def _as_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def usage_from_headers(headers: dict | None) -> dict:
    """Самый нагруженный лимит из заголовков и сколько ждать (сек).

    Номера бизнесов (ключи `x-business-use-case-usage`) наружу не идут.
    """
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    pcts: list[float] = []
    wait_min = 0.0

    app = _as_json(headers.get("x-app-usage"))
    if isinstance(app, dict):
        pcts += [float(app.get(k) or 0) for k in ("call_count", "total_cputime", "total_time")]

    acc = _as_json(headers.get("x-ad-account-usage"))
    if isinstance(acc, dict):
        pcts.append(float(acc.get("acc_id_util_pct") or 0))

    buc = _as_json(headers.get("x-business-use-case-usage"))
    if isinstance(buc, dict):
        for items in buc.values():
            for item in items if isinstance(items, list) else [items]:
                if not isinstance(item, dict):
                    continue
                pcts += [float(item.get(k) or 0) for k in ("call_count", "total_cputime", "total_time")]
                wait_min = max(wait_min, float(item.get("estimated_time_to_regain_access") or 0))

    return {"max_pct": max(pcts) if pcts else None, "wait_s": wait_min * 60.0,
            "seen": bool(pcts)}


class GraphClient:
    """Чтения Graph API для пилота. Одна вкладка — один клиент."""

    def __init__(self, transport: Any, *, min_interval_s: float = 2.0, max_calls: int = 40,
                 reacquire_timeout_s: float = 60.0, allow_writes: bool = False,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.transport = transport
        self.min_interval_s = float(min_interval_s)
        self.max_calls = int(max_calls)
        self.reacquire_timeout_s = reacquire_timeout_s
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        self._slow = False
        self._reacquired = False
        self.calls = 0
        #: Пути, чей список не влез в лимит страниц (ACL-1760).
        self.truncated: list[str] = []
        self.stopped: GraphStop | None = None
        self.last_usage: dict = {}
        #: Видел ли клиент хоть раз заголовки лимитов (CORS может их прятать).
        self.usage_headers_seen = False
        #: ACL-1761: запись разрешена только явно (создатель кампаний).
        self.allow_writes = bool(allow_writes)
        #: Путь записи с неизвестным результатом; пока не `None`, записей нет.
        self.unreconciled: str | None = None
        self.writes = 0

    def reset_budget(self, max_calls: int) -> None:
        """Новый потолок вызовов (на одну кампанию).

        Снимается только стоп по СВОЕМУ потолку (`CallBudget`, Meta молчала):
        он не должен кончать следующие кампании задания (ревью, minor 5).
        Стопы Meta (190/368/лимит/checkpoint) остаются.
        """
        self.max_calls = int(max_calls)
        self.calls = 0
        if type(self.stopped) is CallBudget:
            self.stopped = None

    def reconciled(self, path: str) -> bool:
        """Сверка чтением нашла объект ИМЕННО этой записи — снять её флаг.

        Флаг другой записи не трогается (ревью M4). True — снят.
        """
        if self.unreconciled is not None and self.unreconciled == path:
            self.unreconciled = None
            return True
        return False

    # -- служебное
    def _stop(self, exc: GraphStop) -> GraphStop:
        self.stopped = exc
        return exc

    def _pace(self) -> None:
        if self._last_call is None:
            return
        interval = self.min_interval_s * (3 if self._slow else 1)
        wait = self._last_call + interval - self._clock()
        if wait > 0:
            self._sleep(wait)

    def _check_page(self) -> None:
        try:
            state = self.transport.state() or {}
        except GraphStop as exc:
            # Транспорт сам распознал причину зависания вкладки (логин/checkpoint/молчит).
            raise self._stop(exc) from exc
        kind = state.get("kind")
        if kind == "checkpoint":
            raise self._stop(Checkpoint("the profile is on a Facebook checkpoint"))
        if kind == "login":
            raise self._stop(LoggedOut("the profile is logged out of Facebook"))

    # -- один вызов
    def get(self, path: str, params: dict | None = None) -> dict:
        if self.stopped is not None:
            raise self.stopped
        if self.calls >= self.max_calls:
            raise self._stop(CallBudget(f"call budget of {self.max_calls} reached"))
        self._pace()
        self.calls += 1
        self._last_call = self._clock()
        try:
            reply = self.transport.request("GET", path, params or {})
        except GraphStop as exc:
            raise self._stop(exc) from exc
        self._last_call = self._clock()

        err = reply.get("error")
        if isinstance(err, str):
            if err == "no_token":
                self._check_page()
                return self._token_dead(path, params, None, None)
            if err == "network":
                self._check_page()
            raise self._stop(PageFailed(f"the page could not call Graph API ({err})"))

        usage = usage_from_headers(reply.get("headers"))
        self.last_usage = usage
        self.usage_headers_seen = self.usage_headers_seen or bool(usage["seen"])
        body = reply.get("body") if isinstance(reply.get("body"), dict) else {}
        status = reply.get("status")

        gerr = body.get("error") if isinstance(body, dict) else None
        if isinstance(gerr, dict):
            code = _int(gerr.get("code"))
            sub = _int(gerr.get("error_subcode"))
            msg = str(gerr.get("message") or "Graph API error")
            details = error_details(gerr, reply)
            details.update(_read_context(path, params))
            msg = _with_details(msg, details)
            kind = classify(code)
            if kind == "policy":
                raise self._stop(PolicyBlocked(f"Graph API refused (368): {msg}", code=code, subcode=sub))
            if kind == "rate_limit":
                raise self._stop(RateLimited(f"Graph API rate limit ({code}): {msg}", code=code,
                                             subcode=sub, retry_after_s=usage["wait_s"] or None))
            if kind == "token":
                if sub in CHECKPOINT_SUBCODES:
                    raise self._stop(Checkpoint(f"the session is on a Facebook checkpoint ({code}/{sub})",
                                                code=code, subcode=sub))
                self._check_page()
                return self._token_dead(path, params, code, sub)
            raise GraphError(f"Graph API error {code}: {msg}", code=code, subcode=sub, status=status,
                             details=details)

        if "non_json" in body:
            # HTML вместо JSON — чаще всего перенаправление на логин/checkpoint.
            self._check_page()
            raise self._stop(PageFailed("Graph API answered with a page instead of JSON"))

        if usage["wait_s"] > 0:
            raise self._stop(RateLimited("Meta asked to wait before the next call",
                                         retry_after_s=usage["wait_s"]))
        pct = usage["max_pct"]
        if pct is not None and pct >= STOP_PCT:
            # Этот ответ отдаём, а следующий вызов уже не делаем.
            self._stop(RateLimited(f"API usage at {pct:.0f}% — stopping before the limit"))
        elif pct is not None and pct >= SLOW_PCT:
            self._slow = True
        return body

    # -- одна запись (ACL-1761)
    def post(self, path: str, params: dict | None = None, *, files: dict | None = None,
             filename: str = "", on_ad: bool = False, form_page: str = "",
             progress: Callable[[float], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> dict:
        """POST ровно один раз. Никаких повторов, пересъёмки токена и т.п.

        - отказ до отправки (политика, нет токена) — стоп, к Meta ничего не ушло;
        - сбой после отправки, 5xx, код 1/2, не-JSON — `WriteUnknown`: сверить чтением;
        - 190/368/лимит/checkpoint — стоп, как у чтений, но без повтора;
        - прочая ошибка Graph — `GraphError`: Meta отказала, объекта нет.
        """
        if not self.allow_writes:
            raise ValueError("read-only client: writes are not enabled")
        if self.stopped is not None:
            raise self.stopped
        if self.unreconciled is not None:
            raise WritesBlocked(self.unreconciled)
        if self.calls >= self.max_calls:
            raise self._stop(CallBudget(f"call budget of {self.max_calls} reached"))
        self._pace()
        self.calls += 1
        self.writes += 1
        self._last_call = self._clock()
        try:
            # `on_ad` (ACL-1775) передаём, только когда он есть: прежние транспорты его не знают.
            extra = {"on_ad": True} if on_ad else {}
            # ACL-1776: создание формы Страницы - своя цепочка политики, Страница плана.
            if form_page:
                extra["form_page"] = str(form_page)
            # ACL-1899: прогресс и Stop нужны только записи с файлом; прежние транспорты их не знают.
            if files and progress is not None:
                extra["progress"] = progress
            if files and cancelled is not None:
                extra["cancelled"] = cancelled
            reply = self.transport.write(path, params or {}, files=files, filename=filename, **extra)
        except ValueError as exc:
            # Политика Python: ничего не отправлено.
            raise self._stop(PageFailed(f"write refused before sending: {exc}")) from exc
        except GraphStop as exc:
            # Вкладка не отвечала ДО отправки (мир готовится до самого POST).
            raise self._stop(exc) from exc
        self._last_call = self._clock()

        err = reply.get("error") if isinstance(reply, dict) else "bad_reply"
        if isinstance(err, str):
            if err in _NOT_SENT:
                why = reply.get("why") if isinstance(reply, dict) else ""
                raise self._stop(PageFailed(f"the page refused the write ({err}{': ' + str(why) if why else ''})"))
            if err == "no_token":
                self._check_page()
                raise self._stop(LoggedOut("no session token in the page; the write was not sent"))
            self.unreconciled = path
            raise WriteUnknown(f"the write result is unknown ({err})", path=path)

        body = reply.get("body") if isinstance(reply.get("body"), dict) else {}
        status = reply.get("status")
        gerr = body.get("error") if isinstance(body, dict) else None
        if isinstance(gerr, dict):
            code = _int(gerr.get("code"))
            sub = _int(gerr.get("error_subcode"))
            details = error_details(gerr, reply)
            msg = _with_details(str(gerr.get("message") or "Graph API error"), details)
            kind = classify(code)
            if kind == "policy":
                raise self._stop(PolicyBlocked(f"Graph API refused (368): {msg}", code=code, subcode=sub))
            if kind == "rate_limit":
                raise self._stop(RateLimited(f"Graph API rate limit ({code}): {msg}", code=code, subcode=sub))
            if kind == "token":
                if sub in CHECKPOINT_SUBCODES:
                    raise self._stop(Checkpoint(f"the session is on a Facebook checkpoint ({code}/{sub})",
                                                code=code, subcode=sub))
                self._check_page()
                raise self._stop(LoggedOut("the session token is no longer valid; the write was not retried",
                                           code=code, subcode=sub))
            if code in UNKNOWN_RESULT_CODES or (isinstance(status, int) and status >= 500):
                self.unreconciled = path
                raise WriteUnknown(f"Graph API error {code} — the write result is unknown: {msg}", path=path)
            raise GraphError(f"Graph API error {code}: {msg}", code=code, subcode=sub, status=status,
                             details=details)
        if "non_json" in body or (isinstance(status, int) and status >= 500):
            # Запись могла дойти до Meta: метку ставим ДО любой проверки страницы,
            # которая теперь может сама кинуть стоп (вкладка молчит, ACL-1761 B1).
            self.unreconciled = path
            self._check_page_after_write(path)
            raise WriteUnknown("Graph API did not answer the write with JSON; the result is unknown", path=path)
        return body

    def _check_page_after_write(self, path: str) -> None:
        """Проверка страницы после записи с неизвестным результатом.

        Стоп страницы не превращает запись в «не отправлена»: остаётся
        `unreconciled`, наружу — `WriteUnknown` (сверка чтением и отчёт
        «проверьте Ads Manager»), а клиент остановлен честным текстом.
        """
        try:
            self._check_page()
        except GraphStop as exc:
            stop = type(exc)(f"the page stopped ({exc.kind}) after a write whose result is unknown; "
                             "check Ads Manager before publishing again")
            self.stopped = stop
            raise WriteUnknown(str(stop), path=path) from exc

    def _token_dead(self, path: str, params: dict | None, code: int | None, sub: int | None) -> dict:
        if self._reacquired:
            raise self._stop(LoggedOut("the session token is no longer valid (logged out or checkpoint)",
                                       code=code, subcode=sub))
        self._reacquired = True
        state = self.transport.reacquire(timeout=self.reacquire_timeout_s) or {}
        if state.get("kind") == "checkpoint":
            raise self._stop(Checkpoint("the profile is on a Facebook checkpoint", code=code, subcode=sub))
        if state.get("kind") == "login" or not state.get("has_token"):
            raise self._stop(LoggedOut("could not get a fresh session token (logged out or checkpoint)",
                                       code=code, subcode=sub))
        return self.get(path, params)

    # -- постраничное чтение
    def get_all(self, path: str, params: dict | None = None, *, max_pages: int = 5) -> list[dict]:
        rows: list[dict] = []
        query = dict(params or {})
        for page in range(max_pages):
            body = self.get(path, query)
            rows += [r for r in (body.get("data") or []) if isinstance(r, dict)]
            paging = body.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next"):
                break  # следующей страницы нет — список полный
            if not after or self.stopped is not None or page == max_pages - 1:
                # Следующая страница есть, но взять её нельзя (лимит страниц, остановка
                # по usage или нет курсора): вызывающий должен сказать «неполно» (ACL-1760).
                level = (params or {}).get("level")
                self.truncated.append(path + (f"?level={level}" if level else ""))
                break
            query = {**query, "after": after}
        return rows

    # -- чтения пилота
    def ad_accounts(self) -> list[dict]:
        return self.get_all("/me/adaccounts", {
            "fields": "account_id,name,account_status,currency,timezone_name",
            "limit": 50,
        }, max_pages=3)

    def campaigns(self, act_id: str) -> list[dict]:
        return self.get_all(f"/{normalize_act(act_id)}/campaigns", {
            "fields": "name,status,effective_status,objective",
            "limit": 100,
        }, max_pages=5)

    def insights(self, act_id: str, date_preset: str) -> dict:
        if date_preset not in {"yesterday", "last_7d"}:
            raise ValueError("date_preset must be yesterday or last_7d")
        rows = self.get_all(f"/{normalize_act(act_id)}/insights", {
            "level": "account",
            "date_preset": date_preset,
            "fields": "spend,impressions,clicks,actions,account_currency",
        }, max_pages=1)
        return rows[0] if rows else {}


    # -- чтения дешборда (ACL-1760)
    def account(self, act_id: str) -> dict:
        """Сам кабинет: состояние, причина отключения, валюта."""
        return self.get(f"/{normalize_act(act_id)}", {
            "fields": "account_id,name,account_status,disable_reason,currency,timezone_offset_hours_utc,timezone_name",
        })

    def insights_range(self, act_id: str, date_from: str, date_to: str, *, level: str = "account",
                       breakdowns: str = "", fields: str = "spend,account_currency",
                       max_pages: int = 1) -> list[dict]:
        """Insights за период экрана, `date_from`…`date_to` включительно."""
        if level not in {"account", "campaign", "adset", "ad"}:
            raise ValueError("level must be account, campaign, adset or ad")
        if not (_DAY_RE.fullmatch(date_from or "") and _DAY_RE.fullmatch(date_to or "")):
            raise ValueError("dates must look like YYYY-MM-DD")
        params: dict[str, Any] = {
            "level": level,
            "time_range": {"since": date_from, "until": date_to},
            "fields": fields,
            "limit": 500,
        }
        if breakdowns:
            if breakdowns != "country":
                raise ValueError("only the country breakdown is allowed")
            params["breakdowns"] = breakdowns
        return self.get_all(f"/{normalize_act(act_id)}/insights", params, max_pages=max_pages)

    def ads(self, act_id: str, *, max_pages: int = 5) -> list[dict]:
        return self.get_all(f"/{normalize_act(act_id)}/ads", {
            "fields": "name,status,effective_status,campaign_id",
            "effective_status": SHOWN_STATES,
            "limit": 200,
        }, max_pages=max_pages)

    def shown_campaigns(self, act_id: str, *, max_pages: int = 5) -> list[dict]:
        return self.get_all(f"/{normalize_act(act_id)}/campaigns", {
            "fields": "name,status,effective_status",
            "effective_status": SHOWN_STATES,
            "limit": 200,
        }, max_pages=max_pages)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
