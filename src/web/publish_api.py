"""«Publish selected» через Graph API в профиле — кампании на паузе (ACL-1761).

Включается `ACL_FB_BACKEND=api` в `.env` машины (по умолчанию `rpa`). Тогда
задание публикации (`publish_jobs._run_job_inner`) вместо робота Ads Manager
зовёт `ApiPublisher`: тот же черновик билдера (`slots_to_campaign_drafts` →
`enrich_campaign_drafts`), та же очередь мест по памяти и замок профиля (их
берёт `_run_job_inner` до вызова), тот же вид итогов задания.

Решение владельца: всё создаётся в статусе PAUSED; включает только человек.

Жизнь профиля — как у чтений ACL-1760 и у робота (ACL-1444):
* работающий профиль цепляем по его локальному порту отладки, окно руками —
  отказ `open_by_hand`, ничего не останавливаем;
* закрытый поднимаем через MLX (место по памяти уже держит задание);
* в конце закрываем мягко ТОЛЬКО профиль, который подняло это задание, и
  только если всё чисто и на профиль не ждёт другое задание; иначе окно
  остаётся (место держится до «Close profile», как у робота).

Токен сессии остаётся во вкладке; сюда приходят только JSON-ответы.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from src.fb_api import targeting_resolver as _tr_cap
from src.fb_api import telemetry as _tm
from src.fb_api.creator import (
    resolve_page,
    AccountFacts,
    CampaignCreator,
    Outcome,
    PlanError,
    TargetingResolver,
    plan_from_draft,
    preflight_account,
)
from src.web.api_build_progress import BuildCounter
from src.fb_api.graph import CallBudget, GraphClient, GraphError, GraphStop, normalize_act

log = logging.getLogger(__name__)

BACKEND_ENV = "ACL_FB_BACKEND"
CREATE_MAX_CALLS_ENV = "ACL_FB_API_CREATE_MAX_CALLS"
MIN_INTERVAL_ENV = "ACL_FB_API_MIN_INTERVAL_S"
#: Потолок вызовов Graph на одну кампанию (ACL-1768): сам бюджет считается по плану
#: (`creator.estimate_create_calls`: основа + за набор + за объявление + опросы видео +
#: чтения форм Instant Form) и зажимается этим потолком. Он же верхняя граница для
#: `ACL_FB_API_CREATE_MAX_CALLS`. Кампания, которой нужно больше, отклоняется заранее
#: ("split it"), а не останавливается на полпути. Свой потолок кончает только эту
#: кампанию, следующие идут со своим (ревью, minor 5).
DEFAULT_CREATE_MAX_CALLS = 250
#: Бюджет вызовов на кампанию не опускается ниже прежнего фиксированного (ревью 833, раунд 2).
CREATE_BUDGET_FLOOR = 150
CREATE_CALLS_FLOOR = 20
#: Потолок на предпроверку кабинета (один раз на задание).
PREFLIGHT_MAX_CALLS = 12
READY_TIMEOUT_S = 90.0
PROFILE_CLOSED_NOTICE = "Closed the profile — all campaigns are created and paused"


def backend() -> str:
    """`api` или `rpa` (по умолчанию). Незнакомое значение — `rpa`."""
    return "api" if (os.environ.get(BACKEND_ENV) or "").strip().lower() == "api" else "rpa"


def api_enabled() -> bool:
    return backend() == "api"


def _env_number(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def create_call_budget(plan: Any) -> tuple[int, str]:
    """(бюджет вызовов на кампанию, отказ). Бюджет - по плану, не больше потолка из окружения.

    Если плану нужно больше потолка, отказ вслух сразу (и в Check plan, и в запуске), а не
    остановка на полпути.
    """
    from src.fb_api.creator import estimate_create_calls, estimate_targeting_reads

    # Записи кампании - не больше DEFAULT_CREATE_MAX_CALLS; сверх них бюджет несёт запас на чтения
    # таргетинга (ACL-1771), которые идут из того же счётчика вызовов.
    hard_max = DEFAULT_CREATE_MAX_CALLS + _tr_cap.JOB_READ_ABS_CAP
    ceiling = int(_env_number(CREATE_MAX_CALLS_ENV, hard_max, CREATE_CALLS_FLOOR, hard_max))
    need = estimate_create_calls(plan)
    if need > DEFAULT_CREATE_MAX_CALLS:
        return ceiling, (f"This campaign needs about {need} Meta calls, more than one run allows "
                         "— split it. Nothing was created.")
    # Не ниже прежних 150: малые планы не теряют запас на медленное видео. Нижний потолок в окружении -
    # решение оператора: бюджет зажимается им (остановка на полпути, как всегда).
    # ACL-1768 x ACL-1771: чтения таргетинга по всем наборам идут из того же бюджета. Запас сверх записей
    # зажат тем же потолком; если после чтений записей не хватает, `_prepare` откажет до первой записи.
    return min(max(need + estimate_targeting_reads(plan), CREATE_BUDGET_FLOOR), ceiling), ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Точки подмены в тестах.
def _resolve_entry(account_label: str):
    from config.paths import get_ad_account_map_path
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry

    return resolve_account_entry(load_ad_account_map(get_ad_account_map_path()), account_label)


def _open_profile(profile_id: str, folder_id: str, instance: str):
    from src.fb_api import profile as _fb_profile

    return _fb_profile.open_profile(profile_id, folder_id, instance)


def _open_tab(cdp_url: str):
    from src.fb_api.cdp import PageTab

    return PageTab.open(cdp_url)


def _make_graph(tab, act: str, *, writes: bool = True):
    from src.fb_api.token_capture import InPageGraph

    if not writes:
        # Пробный запуск: обычные записи закрыты. Отдельный account-bound
        # validate-only creative может подтвердить Page, не создавая объектов.
        return InPageGraph(tab)
    return InPageGraph(tab, allow_writes=True, write_act=act)


def _stop_profile(entry, account_label: str) -> None:
    from src.fb_ads_rpa import browser as rpa_browser

    rpa_browser.stop_profile_on_request(entry, account_label)


def _keep_profiles_open() -> bool:
    try:
        from src.fb_ads_rpa.rpa_publisher import keep_profiles_open

        return keep_profiles_open()
    except Exception:
        return True


def _cooldown(account_label: str) -> tuple[float, str]:
    """Общее с чтениями охлаждение профиля после лимита/checkpoint (ACL-1760)."""
    try:
        from src.analysis_agent import account_spend as _as
        from src.analysis_agent import account_spend_api as _api

        return _api._cooldown_state(_as._profile_key(account_label))
    except Exception:
        return 0.0, ""


def _start_cooldown(account_label: str, stop: GraphStop) -> None:
    try:
        from src.analysis_agent import account_spend as _as
        from src.analysis_agent import account_spend_api as _api

        _api._start_cooldown(_as._profile_key(account_label), stop)
    except Exception:
        pass


def _record(step: str, ok: bool | None, campaign: str = "", diag: dict | None = None,
            duration_s: float | None = None) -> None:
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        _rt.record(step, ok, campaign=campaign, duration_s=duration_s,
                   diag={"source": "api", **_tm.scrub_value(diag or {})})
    except Exception:
        pass


def build_drafts(slots: list[dict[str, Any]], *, account_label: str, country: str) -> list[Any]:
    """Тот же черновик, что у RPA (`RpaPublisher.build_drafts` без браузера)."""
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts

    drafts = slots_to_campaign_drafts(slots, account_label=account_label, country=country, activate=False)
    return enrich_campaign_drafts(drafts, account_label=account_label, preview_mode=False)


class ApiPublishError(RuntimeError):
    """Отказ всего задания (профиль, страница, кабинет) с текстом человеку."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(_tm.scrub(message))
        self.code = code


def _published_as(outcome: Outcome) -> str:
    """ACL-1902: имя в Meta, только если оно не совпадает с именем из билдера (иначе пусто)."""
    chosen = str(outcome.published_as or "")
    return chosen if chosen and chosen != outcome.campaign_name else ""


def dry_run_result(outcome: Outcome) -> dict[str, Any]:
    """Итог пробного запуска: план и предпроверка, ничего не создано (ревью M3)."""
    ok = not outcome.error
    return {
        "campaign_name": outcome.campaign_name, "published_as": _published_as(outcome),
        "backend": "api", "dry_run": True, "status": "",
        "success": ok, "publishable": False,
        "warnings": list(outcome.warnings) + (["Dry run: nothing was created"] if ok else []),
        "blocking_failures": [], "problems": list(outcome.problems), "run_id": None, "campaign_id": None,
        "adset_id": None, "creative_id": None, "ad_id": None, "ad_ids": [],
        "error": outcome.error or None, "steps_completed": list(outcome.steps), "screenshots": [],
        "readback": {}, "plan": outcome.plan, "created": {}, "reused": {}, "ts": _now_iso(),
        # ACL-1819: вердикты серверных правил pre-flight audit (пусто, если аудит не шёл).
        "audit": list(outcome.audit),
        # ACL-1771: термины таргетинга (найдены / ждут подтверждения) и кэш подтверждённых.
        "targeting": list(outcome.targeting),
    }


def result_dict(outcome: Outcome, *, error: str = "") -> dict[str, Any]:
    """Итог кампании в виде, который рисует панель задания (как `to_job_dict`)."""
    created = bool(outcome.campaign_id)
    error = error or outcome.error
    mismatches = outcome.mismatches
    blocking = [f"readback mismatch: {m['object']} {m['field']} — expected {m['expected']!r}, "
                f"got {'Not read' if m.get('not_read') else repr(m['actual'])}" for m in mismatches]
    if created and error:
        blocking.append("not finished: " + error)
    readback_ok = bool(outcome.readback) and outcome.readback.get("ok") is True
    success = created and not error
    return {
        "campaign_name": outcome.campaign_name,
        "published_as": _published_as(outcome),
        "backend": "api",
        "status": "PAUSED" if created else "",
        "success": success,
        "publishable": success and readback_ok and not blocking and not outcome.manual_required,
        "manual_required": list(outcome.manual_required),
        "warnings": list(outcome.warnings),
        "blocking_failures": blocking,
        # ACL-1805: отказы Meta и правил - списком ACL-1437 (причина и что делать).
        "problems": list(outcome.problems),
        "run_id": None,
        "campaign_id": outcome.campaign_id or None,
        "adset_id": outcome.adset_ids[0] if outcome.adset_ids else None,
        # ACL-1768: все наборы кампании (adset_id - первый, как всегда).
        "adset_ids": list(outcome.adset_ids),
        "creative_id": None,
        "ad_id": outcome.ad_ids[0] if outcome.ad_ids else None,
        "ad_ids": list(outcome.ad_ids),
        "error": error or None,
        "steps_completed": list(outcome.steps),
        "screenshots": [],
        "readback": outcome.readback,
        "created": {k: (len(v) if isinstance(v, list) else 1) for k, v in outcome.created.items()},
        "reused": {k: (len(v) if isinstance(v, list) else 1) for k, v in outcome.reused.items()},
        "ts": _now_iso(),
    }


class ApiPublisher:
    """Создатель кампаний задания: профиль → своя вкладка → Graph с записью."""

    def __init__(self, *, progress: Callable[[str], None] = lambda _s: None, dry_run: bool = False):
        #: Пробный запуск: клиент и помощник без права записи, только план.
        self.dry_run = bool(dry_run)
        #: Результат прошлой записи неизвестен и не сверен — дальше не пишем (M4).
        self.blocked = ""
        #: Точка подмены в тестах (паузы опроса видео и сверки).
        self.sleep: Callable[[float], None] = time.sleep
        self.cancel_event = threading.Event()
        self.progress = progress
        #: ACL-1992: счёт созданных объектов кампании для процента сборки;
        #: задание подставляет свой приёмник после создания.
        self.units: Callable[[str, dict], None] = lambda _n, _u: None
        self._tab = None
        #: ACL-1812: аренда тёплой вкладки профиля (вместо своей вкладки).
        self._lease: Any = None
        self._client: GraphClient | None = None
        self._facts: AccountFacts | None = None
        self._resolver: TargetingResolver | None = None
        self._entry = None
        self._session_profile_id = ""
        self._opened = None
        #: ACL-2113: окно открыто прошлым заданием и оставлено (мы его взяли без перезапуска) —
        #: для решений «закрыть/держать» оно наше, как поднятое этим заданием.
        self.inherited_profile = False
        self.account_label = ""
        self.act = ""
        self.stop: GraphStop | None = None

    # -- совместимость с `publish_jobs` (как у RpaPublisher)
    def cancel(self) -> None:
        self.cancel_event.set()

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def started_profile(self) -> bool:
        """Профиль поднят самим заданием (не пользователем) — ACL-1784."""
        return bool(getattr(self._opened, "started_now", False)) or bool(self.inherited_profile)

    @property
    def writes(self) -> int:
        """Сколько записей ушло в Meta за задание, включая с неизвестным итогом (ACL-1784)."""
        return int(getattr(self._client, "writes", 0) or 0) if self._client is not None else 0

    def build_drafts(self, slots, *, account_label: str, country: str, **_ignored) -> list[Any]:
        return build_drafts(slots, account_label=account_label, country=country)

    # -- открыть
    def open(self, account_label: str) -> None:
        """Профиль, вкладка, токен, предпроверка кабинета. Ни одной записи."""
        self.account_label = account_label
        entry = _resolve_entry(account_label)
        if entry is None:
            raise ApiPublishError(f"{account_label} is not in the account map")
        try:
            self.act = normalize_act(str(getattr(entry, "ad_account_id", "") or ""))
        except ValueError as exc:
            raise ApiPublishError(f"{account_label}: {exc}") from exc
        profile_id = (getattr(entry, "profile_id", "") or "").strip()
        if not profile_id:
            raise ApiPublishError(f"{account_label} has no profile_id in the account map")
        left, why = _cooldown(account_label)
        if left > 0 and why == "page_unresponsive":
            raise ApiPublishError(
                f"{account_label}: the Ads Manager tab stopped answering on the last run; waiting about "
                f"{int(left // 60) + 1} min before opening it again. Nothing was created.",
                code="page_unresponsive")
        if left > 0:
            raise ApiPublishError(
                f"{account_label}: Meta stopped the last API call ({why}); waiting about "
                f"{int(left // 60) + 1} min before calling again. Nothing was created.")
        self._entry = entry
        self._session_profile_id = profile_id
        from src.fb_api import profile as _fb_profile

        if self._open_warm(account_label, profile_id):
            return
        from src.fb_api import mlx_session as _mlx

        try:
            from src.fb_api import stop_log as _stop_log

            with _mlx.scope(account=account_label), _stop_log.cancel_scope(self.cancelled):
                self._opened = _open_profile(profile_id, (getattr(entry, "folder_id", "") or "").strip(),
                                             (getattr(entry, "multilogin_instance", "") or "A").strip() or "A")
        except _fb_profile.ProfileError as exc:
            raise ApiPublishError(str(exc), code=getattr(exc, "code", "")) from exc
        except Exception as exc:  # noqa: BLE001
            raise ApiPublishError(f"{account_label}: the profile could not be opened with automation: "
                                  f"{_tm.scrub(str(exc))}") from exc
        self.progress("profile attached")
        self._tab = _open_tab(self._opened.cdp_url)
        graph = _make_graph(self._tab, self.act, writes=not self.dry_run)
        from src.fb_api.graph import Checkpoint, LoggedOut

        try:
            graph.install()
            graph.navigate(self.act, timeout=60.0)
            state = graph.wait_ready(timeout=READY_TIMEOUT_S)
        except GraphStop as exc:
            # Вкладка не ответила, браузер сказал почему (логин/checkpoint/молчит): стоп, без повторов.
            self._halt(exc)

        try:
            from src.fb_api import launch as _launch

            _launch.note_graph_session(profile_id, bool(state.get("has_token")), account=account_label)
        except Exception:  # noqa: BLE001 — телеметрия не роняет публикацию
            pass
        if state.get("kind") == "checkpoint":
            self._halt(Checkpoint("the profile is on a Facebook checkpoint"))
        if state.get("kind") == "login":
            self._halt(LoggedOut("the profile is logged out of Facebook"))
        if not state.get("has_token"):
            raise ApiPublishError(f"{account_label}: Ads Manager did not finish loading "
                                  "(no Graph session in the page); nothing was created")
        self._finish_open(account_label, graph)

    def _finish_open(self, account_label: str, graph: Any) -> None:
        """Клиент Graph на готовой вкладке и предпроверка кабинета (общее для обоих путей)."""
        self._client = GraphClient(
            graph, allow_writes=not self.dry_run,
            min_interval_s=_env_number(MIN_INTERVAL_ENV, 1.0, 0.5, 10.0),
            max_calls=PREFLIGHT_MAX_CALLS)
        self.progress("Ads Manager session ready")
        try:
            self._facts = preflight_account(self._client, self.act)
        except PlanError as exc:
            raise ApiPublishError(f"{account_label}: {exc}") from exc
        except GraphStop as exc:
            self._halt(exc)
        except GraphError as exc:
            raise ApiPublishError(f"{account_label}: Graph API error in the account check: {exc}") from exc
        self._resolver = TargetingResolver(self._client, search_cache=PLAN_SEARCH_CACHE, cache_scope=account_label)
        self.progress("ad account verified")

    def _open_warm(self, account_label: str, profile_id: str) -> bool:
        """ACL-1812: профиль «на сегодня» — берём его вкладку робота, без перезагрузки.

        True — вкладка взята, клиент и предпроверка кабинета сделаны. Профиль не
        поднимаем и не останавливаем; вкладку по концу задания отпускаем, не закрывая.
        """
        from src.fb_api import warm as _warm

        reg = _warm.registry()
        if not reg.is_warm(profile_id):
            return False
        act, dry = self.act, self.dry_run
        mode = "read" if dry else f"create:{act}"

        def make(tab):
            return _make_graph(tab, act, writes=not dry)

        try:
            self._lease = reg.checkout(profile_id, account=account_label, act=act, mode=mode,
                                       make_graph=make, ready_timeout=READY_TIMEOUT_S)
        except _warm.WarmError as exc:
            raise ApiPublishError(str(exc), code=exc.code) from exc
        except GraphStop as exc:
            self._halt(exc)
        self.progress("profile attached (warm)")
        self._finish_open(account_label, self._lease.graph)
        return True

    def _halt(self, stop: GraphStop) -> None:
        self.stop = stop
        _start_cooldown(self.account_label, stop)
        _tm.report_stop(stop.kind, str(stop), {"source": "publish", "code": stop.code})
        raise ApiPublishError(f"{self.account_label}: Graph API stopped ({stop.kind}): {stop}",
                              code=stop.kind)

    # -- одна кампания
    def publish_draft(self, draft: Any) -> dict[str, Any]:
        began = time.monotonic()
        name = str(getattr(draft, "name", "") or "")
        if self.stop is not None:
            return result_dict(Outcome(campaign_name=name),
                               error=f"Not started: Graph API stopped earlier ({self.stop.kind})")
        if self.blocked or (self._client is not None and self._client.unreconciled is not None):
            # Ревью M4: пока прошлая неизвестная запись не сверена, новых нет.
            return result_dict(Outcome(campaign_name=name), error=(
                "Not started: an earlier write in this run has an unknown result "
                f"({self.blocked or 'not reconciled'}). Check Ads Manager, then publish again "
                "(a re-run does not duplicate)."))
        try:
            plan = plan_from_draft(draft)
        except PlanError as exc:
            return result_dict(Outcome(campaign_name=name), error=f"{exc}. Nothing was created.")
        if plan.act != self.act:
            return result_dict(Outcome(campaign_name=name),
                               error="The draft's ad account differs from the verified one. Nothing was created.")
        assert self._client is not None and self._facts is not None and self._resolver is not None
        budget, too_big = create_call_budget(plan)
        if too_big:
            return result_dict(Outcome(campaign_name=name), error=too_big)
        early_notes: list[str] = []
        if not self.dry_run and any(ad.form_template for s_ in plan.ad_sets for ad in s_.ads):
            # ACL-1776: Страница плана закрепляется на сессию до первой записи формы.
            try:
                # ACL-2192: черновик без Страницы («Automatic») - выбрать её ДО закрепления:
                # pin_page принимает только числовой id, а creator выбирает Страницу позже
                # (повторный resolve_page в _prepare идемпотентен: page_id уже задан и на кабинете).
                if not plan.page_id:
                    early_notes = list(resolve_page(plan, self._facts))
                pinned = self._client.transport.pin_page(plan.page_id)
            except PlanError as exc:
                return result_dict(Outcome(campaign_name=name), error=f"{exc}. Nothing was created.")
            except GraphStop as exc:
                # Чтение Страниц профиля до закрепления тоже может упереться в стоп: как в creator.run.
                if not isinstance(exc, CallBudget):
                    self.stop = exc
                    _start_cooldown(self.account_label, exc)
                    _tm.report_stop(exc.kind, str(exc), {"source": "publish", "code": exc.code})
                return result_dict(Outcome(campaign_name=name), error=(
                    f"Graph API stopped ({exc.kind}) while choosing the Page: {exc}. Nothing was created."))
            except ValueError as exc:
                return result_dict(Outcome(campaign_name=name),
                                   error=f"The Page of this draft cannot be used in this run ({exc}). "
                                         "Nothing was created.")
            if isinstance(pinned, dict) and not pinned.get("has_token"):
                return result_dict(Outcome(campaign_name=name), error=(
                    "Ads Manager did not give the session back after pinning the Page "
                    f"({pinned.get('kind') or 'no token'}). Nothing was created; run again."))
        self._client.reset_budget(budget)

        # Пробный запуск ничего не создаёт — процента сборки у него нет.
        counter = None if self.dry_run else self._counter(plan)

        def step(text: str) -> None:
            self.progress(f"{name}: {text}")
            _record("api_create_step", True, campaign=name, diag={"step": text})
            if counter is not None:   # и строка без новой единицы — «last» на странице свежий
                self._report_units(name, counter, text)

        if counter is not None:
            self._report_units(name, counter)

        creator = CampaignCreator(self._client, self._facts, self._resolver,
                                  cancelled=self.cancelled, progress=step, sleep=self.sleep,
                                  live_progress=lambda text: self.progress(f"{name}: {text}"))
        if self.dry_run:
            outcome = creator.plan_only(plan)
            if self._client.writes:
                raise AssertionError("dry run must not write")  # структурно невозможно
            res = dry_run_result(outcome)
            _record("api_create_dry_run", not outcome.error, campaign=name,
                    duration_s=round(time.monotonic() - began, 1),
                    diag={"calls": self._client.calls, "stop": outcome.stop_kind})
            return res
        try:
            outcome = creator.run(plan)
        except Exception as exc:
            # F2: строка пилота пишется и когда создатель упал исключением.
            kind = exc.kind if isinstance(exc, GraphStop) else (
                "cdp_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__)
            _record("api_create_campaign", False, campaign=name,
                    duration_s=round(time.monotonic() - began, 1),
                    diag={"calls": self._client.calls, "writes": self._client.writes, "stop": kind})
            _tm.record({"event": "fb_api_create", "source": "api", "ok": False,
                        "took_s": round(time.monotonic() - began, 1), "calls": self._client.calls,
                        "writes": self._client.writes, "stop": kind, "raised": type(exc).__name__})
            if isinstance(exc, GraphStop):
                self.stop = exc
                _start_cooldown(self.account_label, exc)
            raise
        if outcome.stop_kind == "unknown" or self._client.unreconciled is not None:
            self.blocked = outcome.campaign_name
        if self._client.stopped is not None and not isinstance(self._client.stopped, CallBudget):
            self.stop = self._client.stopped
            _start_cooldown(self.account_label, self.stop)
            _tm.report_stop(self.stop.kind, str(self.stop), {"source": "publish", "code": self.stop.code})
        res = result_dict(outcome)
        # ACL-2192: заметка «Automatic Facebook Page» выбрана раньше creator - в итог она переносится здесь.
        res["warnings"] = [n for n in early_notes if n not in res["warnings"]] + res["warnings"]
        _record("api_create_campaign", res["publishable"], campaign=name,
                duration_s=round(time.monotonic() - began, 1),
                diag={"calls": self._client.calls, "writes": self._client.writes,
                      "created": res["created"], "reused": res["reused"],
                      "mismatches": len(outcome.mismatches), "stop": outcome.stop_kind})
        _tm.record({"event": "fb_api_create", "source": "api", "ok": res["publishable"],
                    "took_s": round(time.monotonic() - began, 1), "calls": self._client.calls,
                    "created": res["created"], "reused": res["reused"],
                    "mismatches": len(outcome.mismatches), "stop": outcome.stop_kind})
        return res

    @staticmethod
    def _counter(plan: Any) -> BuildCounter | None:
        try:
            return BuildCounter(plan)
        except Exception:  # noqa: BLE001 — процент не должен ронять публикацию
            return None

    def _report_units(self, name: str, counter: BuildCounter, text: str = "") -> None:
        try:
            if text:
                counter.feed(text)
            self.units(name, counter.view())
        except Exception:  # noqa: BLE001 — процент не должен ронять публикацию
            pass

    # -- конец
    def refresh_structure(self) -> None:
        """Read the account list before releasing this already-open session.

        The job calls this only after confirmed publishing. Keep its original
        call budget and stops; a failed refresh never repeats a publish.
        """
        if (self.dry_run or self.blocked or self.stop or self.cancelled()
                or self._client is None):
            return
        from types import SimpleNamespace
        from src.analysis_agent import account_refresh, campaign_inventory

        snap = campaign_inventory.read_in_session(
            self.account_label, SimpleNamespace(client=self._client, act=self.act, profile_id=self._session_profile_id), trigger="action")
        account_refresh.queue().record_result(snap, trigger="action")

    def close(self, *, force: bool = False) -> None:
        """Закрыть свою вкладку. Профиль не трогаем."""
        _ = force
        lease, self._lease = self._lease, None
        if lease is not None:
            # Тёплая вкладка остаётся открытой для следующего действия.
            stop = self.stop if isinstance(self.stop, GraphStop) else None
            try:
                lease.release(stop)
            except Exception:
                log.exception("Could not release the warm tab for %s", self.account_label)
        tab, self._tab = self._tab, None
        if tab is not None:
            try:
                tab.close()
            except Exception:
                log.exception("Could not close our tab for %s", self.account_label)

    def finish_job(self, account_label: str, *, clean: bool, other_job_waits: bool = False) -> str:
        """Вкладку закрыть; профиль — только если его подняли мы и всё чисто."""
        self.close()
        if self.dry_run:
            clean = False   # после пробного запуска окно остаётся: настоящий запуск следом
        opened = self._opened
        ours = (opened is not None and getattr(opened, "started_now", False)) or (
            opened is not None and self.inherited_profile)
        if not ours:
            _record("profile_closed_after_job", None, diag={"note": "not_started_by_job"})
            return ""
        if clean and not other_job_waits and not self.cancelled() and _keep_profiles_open():
            # ACL-2113: чистая публикация — окно остаётся для байера (его закроет простой
            # при нехватке места/памяти, либо кнопка Close profile).
            _record("profile_closed_after_job", None, diag={"note": "kept_convenience"})
            return "kept"
        if not clean or other_job_waits or self.cancelled():
            _record("profile_closed_after_job", None, diag={"note": "kept"})
            return ""
        from src.fb_api import stop_log as _stop_log

        try:
            _stop_profile(self._entry, account_label)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: could not close the profile the job started (%s)", account_label, exc)
            _record("profile_closed_after_job", False, diag={"error": type(exc).__name__})
            return ""
        finally:
            # ACL-1812 (ревью #813, B2): остановка записана при любой попытке.
            _stop_log.record_stop((getattr(self._entry, "profile_id", "") or "").strip())
        _record("profile_closed_after_job", True)
        return "closed"


# ───────────────────────── «Check plan (no writes)» в билдере ─────────────────────────
#
# Кнопка рядом с «Publish selected» (только при ACL_FB_BACKEND=api): те же
# черновики, предпроверка и план — НИ ОДНОЙ записи. Идёт не через задание
# публикации, а путём чтения ACL-1760 (`account_spend_api._session`): тот же
# замок профиля для чтений, место по памяти только если профиль закрыт,
# закрывается только поднятое этим чтением, вкладка и клиент — без права
# записи (помощник с WRITE=null, `GraphClient(allow_writes=False)`). Page профиля
# дополнительно требует один отдельный validate-only creative, без создания объектов.

#: Потолок вызовов на предпроверку кабинета внутри «Check plan».
# ACL-1771: поиск детального таргетинга ходит за списками Meta. Потолок задания в резолвере растёт с числом
# терминов, но не выше `JOB_READ_ABS_CAP`; здесь он лежит выше него с запасом на предпроверку кабинета.
PLAN_PREFLIGHT_MAX_CALLS = 30 + _tr_cap.JOB_READ_ABS_CAP
#: Результаты поиска переживают Check plan на короткое время: повтор после «unknown» идёт дальше.
PLAN_SEARCH_CACHE = _tr_cap.SearchCache()

#: ACL-2117: сколько секунд Check plan держит открытым профиль, который сам поднял
#: (0 — закрывать как раньше, через `CLOSE_GRACE_S`).
PLAN_KEEP_OPEN_ENV = "ACL_PLAN_KEEP_OPEN_S"
DEFAULT_PLAN_KEEP_OPEN_S = 600.0


def plan_keep_open_s() -> float:
    try:
        value = float(str(os.environ.get(PLAN_KEEP_OPEN_ENV, "") or DEFAULT_PLAN_KEEP_OPEN_S).replace(",", "."))
    except ValueError:
        value = DEFAULT_PLAN_KEEP_OPEN_S
    return max(0.0, min(3600.0, value))


def _plan_in_read_session(account_label: str, drafts: list[Any]) -> list[dict[str, Any]]:
    from src.analysis_agent import account_spend_api as _api

    results: list[dict[str, Any]] = []
    with _api._session(account_label) as sess:
        client = sess.client
        if client.allow_writes:
            raise AssertionError("the read session must not be able to write")
        client.reset_budget(PLAN_PREFLIGHT_MAX_CALLS)
        pub = ApiPublisher(dry_run=True)
        pub.act, pub.account_label, pub._client = sess.act, account_label, client
        try:
            pub._facts = preflight_account(client, sess.act)
        except PlanError as exc:
            raise ApiPublishError(f"{account_label}: {exc}", code="account") from exc
        pub._resolver = TargetingResolver(client, search_cache=PLAN_SEARCH_CACHE, cache_scope=account_label)
        for draft in drafts:
            if draft.errors:
                out = Outcome(campaign_name=str(draft.name), warnings=list(draft.warnings or []))
                out.error, out.stop_kind = "; ".join(draft.errors), "plan"
                results.append(dry_run_result(out))
                continue
            results.append(pub.publish_draft(draft))
        if client.writes:
            raise AssertionError("Check plan must not write")
        # ACL-2117: проверенный план обычно сразу публикуют. Окно, которое подняла проверка,
        # держим `plan_keep_open_s()` — Publish подхватит его без второго старта (и без
        # остановки → переоткрытия во время сохранения профиля MLX). Не взяли — закроется по сроку
        # (keep-open выключен) или уступит место по простою (keep-open включён).
        # Только после чистого прохода: остановка Meta/логаут/потолок вызовов окно не держат.
        if (client.stopped is None and any(not d.errors for d in drafts)
                and plan_keep_open_s() > 0):
            sess.keep_next, sess.keep_grace_s = True, plan_keep_open_s()
            # Keep-open включён (по умолчанию после ACL-2113): окно — «удобное» kept-окно, им
            # управляет вытеснение по простою; таймер `plan_keep_open_s()` — только при выключенном.
            sess.keep_convenience = _keep_profiles_open()
    return results


def check_plan(slots: list[dict[str, Any]], *, account_label: str, country: str) -> dict[str, Any]:
    """Черновики билдера → план каждой кампании. Ничего не создаёт."""
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    started = time.monotonic()
    try:
        drafts = build_drafts(slots, account_label=account_label, country=country)
    except Exception as exc:  # noqa: BLE001
        return {"account": account_label, "results": [], "dry_run": True, "error_kind": "plan",
                "error": f"Failed to build campaign drafts: {_tm.scrub(str(exc))}"}
    if not drafts:
        return {"account": account_label, "results": [], "dry_run": True, "error_kind": "plan",
                "error": "No campaigns to check."}
    run = _as.serialized_browser(key=_as._profile_key, reading=True)(_plan_in_read_session)
    account_audit: dict[str, Any] | None = None
    error_kind = ""
    except_kind = ""
    try:
        results = run(account_label, drafts)
        error = ""
    except Exception as exc:  # noqa: BLE001 — отказ профиля/страницы: текстом
        results = []
        error = str(exc) if isinstance(exc, ApiPublishError) else _api._failure(account_label, exc)[0]
        except_kind = type(exc).__name__
        if isinstance(exc, ApiPublishError) and exc.code == "account":
            # ACL-1819: кабинет недоступен/не активен — это вердикт правила account_active.
            account_audit = {"rule": "account_active", "status": "error", "detail": error, "ads": []}
            error_kind = "account"
        else:
            # ACL-1990: сессия чтения упала раньше, чем проверили хоть одну кампанию
            # (профиль занят, сброс соединения, вход в прокси, CdpTimeout, лимит Meta).
            # Это «не удалось проверить», а не ошибка кампании: Publish не выключаем.
            error_kind = "not_checked"
    _record("api_plan_check", not error,
            diag={"campaigns": len(drafts), "results": len(results),
                  **({"error_kind": error_kind, "raised": except_kind} if error_kind else {})},
            duration_s=round(time.monotonic() - started, 1))
    return {"account": account_label, "results": results, "dry_run": True, "error": error,
            "error_kind": error_kind, "account_audit": account_audit}
