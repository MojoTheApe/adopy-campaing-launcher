"""Эндпоинты тёплых сессий: состояние строк, «Open profiles for today», «Close profiles» (ACL-1812).

Только при `ACL_FB_BACKEND=api`; при `rpa` — чёткий отказ, а состояние отвечает
`available: false`. Логика — в `src/fb_api/warm.py`; здесь проводка, коды ответов
и фоновое задание запуска (запуски идут по одному, с паузой 5–10 с).

Состояние строк (`state`) берётся только из реестра и процессов на машине — ни
одного вызова к Meta, MLX или CDP, поэтому его безопасно просить при открытии
страницы и списка. Запуск и закрытие меняют состояние машины и доступны только
когда дешборд слушает один адрес этого компьютера (как команды над кампаниями).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

log = logging.getLogger(__name__)

#: Сколько аккаунтов принимает одно действие (страна обычно — десяток).
MAX_ACCOUNTS = 40

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_ACTIVE_JOB = ""


def api_enabled() -> bool:
    from src.web import campaign_commands

    return campaign_commands.api_enabled()


def _guard() -> tuple[dict, int] | None:
    from src.web import campaign_commands

    return campaign_commands._guard()  # noqa: SLF001 — тот же закрытый доступ, что у команд


def _close_via_mlx(entry: Any, label: str) -> Any:
    """Мягкая остановка ТОЛЬКО этого профиля через MLX API его пространства, без kill."""
    from src.fb_ads_rpa import browser as rpa_browser

    return rpa_browser.stop_profile_on_request(entry, label)


def _registry():
    from src.fb_api import warm

    reg = warm.registry()
    if not reg.has_closer():
        reg.set_closer(_close_via_mlx)
    return reg


def _clean_labels(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    seen: list[str] = []
    for item in raw or []:
        label = str(item or "").strip()
        if label and label not in seen:
            seen.append(label)
    return seen


def _entry(label: str):
    from src.analysis_agent import account_spend as _as

    return _as._entry_for(label)  # noqa: SLF001


def _profile_id(label: str) -> str:
    try:
        entry = _entry(label)
    except Exception:
        entry = None
    return (getattr(entry, "profile_id", "") or "").strip() if entry else ""


def availability() -> dict:
    if not api_enabled():
        return {"available": False, "code": "rpa_backend", "reason": "Needs ACL_FB_BACKEND=api"}
    from src.web import campaign_commands

    if not campaign_commands.bound_to_loopback():
        return {"available": True, "actions": False,
                "reason": "Opening and closing profiles needs the dashboard to listen on 127.0.0.1 only"}
    from src.fb_api import warm

    return {"available": True, "actions": True, "max_heavy": warm.max_heavy(),
            "restart_cooldown_s": int(warm.restart_cooldown_s())}


def state(labels: list[str]) -> dict:
    """Что показать в строках. Локально: реестр + процессы. Ни одного вызова к Meta."""
    info = availability()
    if not info.get("available"):
        return {**info, "accounts": {}}
    labels = _clean_labels(labels)[:200]
    try:
        got = _registry().states([(label, _profile_id(label)) for label in labels])
    except Exception:  # noqa: BLE001 — экран не должен падать из-за подсказки
        log.exception("Could not work out the warm session states")
        got = {}
    # ACL-2268: статус автопрогрева рядом с чипом реестра (копия, реестр не трогаем).
    with _AUTO_LOCK:
        auto = {l: dict(v) for l, v in _AUTO.items()}
    for label, info_one in list(got.items()):
        mark = auto.get(label)
        if not mark:
            continue
        extra = {"auto": mark["status"]}
        if mark["status"] == "refused" and mark.get("code") in AUTO_REASONS:
            extra["auto_code"] = mark["code"]
            extra["auto_note"] = ("Not opened automatically: " + AUTO_REASONS[mark["code"]]
                                  + ". It opens when you run Check plan or Publish.")
        got[label] = {**info_one, **extra}
    return {**info, "accounts": got}


# ───────────── ACL-2268: автопрогрев из билдера — профиль поднимается, пока человек заполняет кампанию
#
# Тот же путь, что у Publish и Check plan (`prepare_existing`), но «тихий»: ничего не ждёт, никого не
# вытесняет, окно, открытое вручную, не трогает. Неудачный запуск не повторяется; единственный повтор —
# один взгляд через 60 с, если замок профиля был занят (запуска не было вовсе).

AUTO_TRIGGERS = ("add", "duplicate", "edit")
AUTO_REQUEUE_S = 60.0

#: Причины тихого отказа — текст для подсказки на чипе аккаунта.
AUTO_REASONS = {
    "busy": "another action is using this profile",
    "cooldown": "Meta paused this account",
    "open_by_hand": "it is open in a window started by hand",
    "closing": "it is still closing",
    "ceiling": "the profile limit is reached",
    "memory": "the computer is short of memory",
    "machine_busy": "the computer is busy",
    "jobs_waiting": "other runs are waiting",
    "start_failed": "Multilogin did not start it",
}

_AUTO: dict[str, dict] = {}     # label -> {status: queued|opening|done|refused, code, trigger, at}
_AUTO_QUEUE: list[tuple[str, bool]] = []     # (label, это повторный взгляд)
_AUTO_LOCK = threading.Lock()
_AUTO_WORKER: threading.Thread | None = None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _timer(delay: float, fn) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def auto_enabled() -> bool:
    """Настройка `auto_warm` (по умолчанию выключена) при включённых управляемых сессиях."""
    try:
        from src.fb_api.connection import settings

        cfg = settings()
        return bool(cfg.get("auto_warm")) and bool(cfg.get("managed_sessions"))
    except Exception:  # noqa: BLE001 — настройки нечитаемы -> ничего не открываем
        return False


def _publish_busy(label: str) -> bool:
    """Идёт ли публикация в этом профиле: тогда его не трогаем."""
    try:
        from src.analysis_agent import account_spend
        from src.web import publish_jobs

        return bool(publish_jobs.profile_in_use(account_spend._profile_key(label)))  # noqa: SLF001
    except Exception:  # noqa: BLE001 — не уверены -> не открываем
        log.exception("Could not tell whether %s is publishing", label)
        return True


def _auto_set(label: str, status: str, code: str = "", trigger: str = "") -> None:
    with _AUTO_LOCK:
        prev = _AUTO.get(label, {})
        _AUTO[label] = {"status": status, "code": code, "trigger": trigger or prev.get("trigger", ""),
                        "at": prev.get("at", time.time())}


def ensure_open(accounts: Any, trigger: Any = "", campaigns: Any = 0) -> tuple[dict, int]:
    """Поставить профиль ОДНОГО аккаунта в очередь автопрогрева. Ничего не ждёт."""
    refused = _guard()
    if refused:
        return refused
    labels = _clean_labels(accounts)
    if len(labels) != 1:
        return {"error": "Warm exactly one account at a time."}, 400
    if trigger not in AUTO_TRIGGERS:
        return {"error": "Unknown trigger."}, 400
    if isinstance(campaigns, bool) or not isinstance(campaigns, int) or campaigns < 1:
        return {"error": "There is no campaign card to warm for."}, 400
    label = labels[0]
    if not auto_enabled():
        return {"accounts": {label: "off"}}, 200
    try:
        entry = _entry(label)
    except Exception:  # noqa: BLE001
        entry = None
    if entry is None:
        return {"accounts": {label: "not_in_map"}}, 200
    pid = (getattr(entry, "profile_id", "") or "").strip()
    if not pid:
        return {"accounts": {label: "no_profile"}}, 200
    with _AUTO_LOCK:
        prev = _AUTO.get(label)
        if prev and prev["status"] in ("queued", "opening"):
            return {"accounts": {label: prev["status"]}}, 200
    from src.web import auto_warm_store

    # Любой исход (и отказ) тратит попытку на 30 минут; нечитаемый файл — попытки нет.
    if not auto_warm_store.take_attempt(pid):
        return {"accounts": {label: "skipped"}}, 200
    with _AUTO_LOCK:
        _AUTO[label] = {"status": "queued", "code": "", "trigger": trigger, "at": time.time()}
        _AUTO_QUEUE.append((label, False))
        _start_worker_locked()
    return {"accounts": {label: "queued"}}, 202


def _start_worker_locked() -> None:
    """Запустить рабочую нить, если очередь не пуста и нити нет. Вызывать под `_AUTO_LOCK`."""
    global _AUTO_WORKER
    if _AUTO_QUEUE and (_AUTO_WORKER is None or not _AUTO_WORKER.is_alive()):
        _AUTO_WORKER = threading.Thread(target=_auto_worker, daemon=True, name="acl-warm-auto")
        _AUTO_WORKER.start()


def _manual_job_running() -> bool:
    with _JOBS_LOCK:
        return bool(_ACTIVE_JOB and _JOBS.get(_ACTIVE_JOB, {}).get("state") == "running")


def _auto_worker() -> None:
    """Открывает по одному; ручное «Open profiles» не прерываем — ждём, пока оно закончится."""
    global _AUTO_WORKER
    while True:
        with _AUTO_LOCK:
            if not _AUTO_QUEUE:
                if _AUTO_WORKER is threading.current_thread():
                    _AUTO_WORKER = None
                return
            label, retried = _AUTO_QUEUE.pop(0)
        started = False
        try:
            while _manual_job_running():
                _sleep(1.0)
            started = _auto_one(label, retried)
        except Exception as exc:  # noqa: BLE001 — очередь не должна умереть на одном аккаунте
            log.exception("Automatic warm-up of %s failed", label)
            _auto_finish(label, "start_failed", False, 0.0, {"error": type(exc).__name__})
        if started:
            try:
                _sleep(_registry()._stagger())      # пауза 5–10 с после настоящего запуска
            except Exception:  # noqa: BLE001
                pass


def _requeue_later(label: str) -> None:
    def again() -> None:
        with _AUTO_LOCK:
            if _AUTO.get(label, {}).get("status") != "queued" or any(l == label for l, _r in _AUTO_QUEUE):
                return
            _AUTO_QUEUE.append((label, True))
            _start_worker_locked()

    _timer(AUTO_REQUEUE_S, again)


def _auto_finish(label: str, code: str, started: bool, duration_s: float, extra: dict | None = None) -> None:
    """Итог попытки: статус для экрана, строка телеметрии и событие. Только числа и коды."""
    ok_codes = ("started", "already_open")
    _auto_set(label, "done" if code in ok_codes else "refused", code)
    with _AUTO_LOCK:
        trigger = _AUTO.get(label, {}).get("trigger", "")
    payload = {"account": label, "trigger": trigger, "outcome": code, "started": bool(started),
               **(extra or {})}
    try:
        from src.fb_ads_rpa import run_telemetry

        run_telemetry.record("job_phase", code in ok_codes, note="auto_warm",
                             duration_s=round(duration_s, 1), diag=payload)
    except Exception:  # noqa: BLE001 — телеметрия не ломает прогрев
        log.exception("auto warm telemetry failed")
    try:
        from src.acl_event_log.writer import emit

        emit("profile_auto_warm", payload, account=label)
    except Exception:  # noqa: BLE001
        log.exception("auto warm event failed")


def _auto_one(label: str, retried: bool) -> bool:
    """Одна попытка. True — профиль реально запущен (после него нужна пауза)."""
    from src.fb_ads_rpa.operations import profile_lock
    from src.fb_ads_rpa.profile_lease import ProfileLeaseError

    began = time.monotonic()
    try:
        entry = _entry(label)
    except Exception:  # noqa: BLE001
        entry = None
    pid = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
    if not pid:
        _auto_finish(label, "start_failed", False, 0.0)
        return False
    _auto_set(label, "opening")
    try:
        with profile_lock(pid).holding(timeout=0):
            code, started, facts = _auto_locked(label, entry, pid)
    except ProfileLeaseError:
        if not retried:
            _auto_set(label, "queued")      # на экране ничего не меняется; один повторный взгляд
            _requeue_later(label)
            return False
        code, started, facts = "busy", False, {}
    _auto_finish(label, code, started, time.monotonic() - began, {**facts, "retried": retried})
    return started


def _admission(reg: Any, pid: str) -> tuple[str, dict]:
    """Строже, чем для действия человека. Только локальные данные. ("", facts) — можно."""
    facts: dict = {}
    queue = None
    try:
        queue = reg._slot_queue()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        queue = None
    snap = queue.snapshot() if queue is not None else {}
    if snap.get("waiting"):
        return "jobs_waiting", facts
    running = {r.get("profile") for r in snap.get("running", [])}
    open_now = (set(reg.open_profile_ids()) | {p for p in running if p}) - {pid}
    ceiling = int((snap.get("settings") or {}).get("ceiling") or 0)
    if not ceiling:
        from src.web import profile_slots

        ceiling = int(profile_slots.load_settings().ceiling)
    facts.update(open_profiles=len(open_now), ceiling=ceiling)
    if len(open_now) >= ceiling:
        return "ceiling", facts
    if snap and not (snap.get("next") or {}).get("allow", True):
        return "memory", facts
    if reg.warming_others(pid):
        return "machine_busy", facts
    load = reg.load_snapshot(pid)
    facts["used_pct"] = load.get("used_pct")
    if load.get("memory_high"):
        return "memory", facts
    if load.get("busy"):
        return "machine_busy", facts
    return "", facts


def _auto_locked(label: str, entry: Any, pid: str) -> tuple[str, bool, dict]:
    """Под замком профиля. Возвращает (код исхода, запущен ли, факты)."""
    from src.fb_api import mlx_session as _mlx
    from src.fb_api import stop_log, warm
    from src.fb_api.connection import opening_closed_profile, prepare_existing

    reg = _registry()
    kind, _port = reg._scan([pid]).get(pid, ("unknown", None))  # noqa: SLF001
    if kind == "port" and reg.is_warm(pid):
        return "already_open", False, {}
    if kind == "no_port":
        return "open_by_hand", False, {}
    if kind == "closing":
        return "closing", False, {}
    if _publish_busy(label):
        return "busy", False, {}
    try:
        from src.web import publish_api

        if publish_api._cooldown(label)[0] > 0:  # noqa: SLF001
            return "cooldown", False, {}
    except Exception:  # noqa: BLE001
        log.exception("Could not read the Meta stop of %s", label)
        return "cooldown", False, {}
    if stop_log.default().restart_wait_s(pid) > 0:
        return "cooldown", False, {}
    why, facts = _admission(reg, pid)
    if why:
        return why, False, facts
    was_warm = reg.is_warm(pid)
    batch = f"auto-{uuid.uuid4().hex[:6]}"
    _mlx.begin_batch(batch)
    try:
        with _mlx.scope(account=label, batch=batch), warm.quiet_start(), opening_closed_profile():
            ok = prepare_existing(reg, entry, label)
    except warm.WarmError as exc:
        code = {"memory_low": "memory", "profile_closing": "closing", "open_by_hand": "open_by_hand",
                "busy": "busy", "restart_cooldown": "cooldown"}.get(getattr(exc, "code", ""), "start_failed")
        return code, False, facts
    if not ok:
        return "start_failed", False, facts
    sess = reg.session(pid)
    started = bool(sess is not None and sess.dashboard_started and not was_warm)
    return ("started" if started else "already_open"), started, facts


def _job_view(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def start_open(labels: Any, confirm_reopen: bool = False) -> tuple[dict, int]:
    """Запустить профили выбранных аккаунтов «на сегодня» фоновым заданием."""
    refused = _guard()
    if refused:
        return refused
    global _ACTIVE_JOB
    labels = _clean_labels(labels)
    if not labels:
        return {"error": "Choose at least one account."}, 400
    if len(labels) > MAX_ACCOUNTS:
        return {"error": f"Open at most {MAX_ACCOUNTS} accounts at a time."}, 400
    with _JOBS_LOCK:
        if _ACTIVE_JOB and _JOBS.get(_ACTIVE_JOB, {}).get("state") == "running":
            return {"error": "Profiles are already being opened; wait for it to finish.",
                    "job_id": _ACTIVE_JOB}, 409
        job_id = uuid.uuid4().hex[:10]
        job = {"job_id": job_id, "state": "running", "kind": "open", "started": time.time(),
               "total": len(labels), "results": {l: {"state": "waiting"} for l in labels}}
        _JOBS[job_id] = job
        _ACTIVE_JOB = job_id
        for old in sorted(_JOBS, key=lambda j: _JOBS[j]["started"])[:-10]:
            _JOBS.pop(old, None)
    threading.Thread(target=_run_open, args=(job_id, labels, confirm_reopen), daemon=True, name="acl-warm-open").start()
    return _job_view(job), 202


def _run_open(job_id: str, labels: list[str], confirm_reopen: bool = False) -> None:
    global _ACTIVE_JOB
    reg = _registry()
    items: list[tuple[str, Any]] = []
    for label in labels:
        try:
            entry = _entry(label)
        except Exception:
            entry = None
        if entry is None:
            _set_result(job_id, label, {"ok": False, "code": "not_in_map",
                                        "message": "This account is not in the account map."})
        else:
            items.append((label, entry))
    try:
        from src.fb_api.connection import confirmed_reopen
        with confirmed_reopen(confirm_reopen):
            reg.open_many(items, job_id=job_id, on_result=lambda l, r: _set_result(job_id, l, r))
    except Exception:  # noqa: BLE001 — задание не должно умереть молча
        log.exception("Opening profiles failed")
    finally:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is not None:
                job["state"] = "done"
                job["finished"] = time.time()
            if _ACTIVE_JOB == job_id:
                _ACTIVE_JOB = ""


def _set_result(job_id: str, label: str, result: dict) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["results"][label] = {"state": "ok" if result.get("ok") else "failed", **result}


def job_status(job_id: str) -> tuple[dict, int]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return {"error": "Unknown job."}, 404
        return _job_view(job), 200


def close_profiles(labels: Any, confirm: bool) -> tuple[dict, int]:
    """Мягко закрыть профили, которые поднял дешборд. Только с подтверждением."""
    refused = _guard()
    if refused:
        return refused
    if not confirm:
        return {"error": "Closing profiles needs a confirmation.", "code": "confirm"}, 400
    labels = _clean_labels(labels)
    if not labels:
        return {"error": "Choose at least one account."}, 400
    if len(labels) > MAX_ACCOUNTS:
        return {"error": f"Close at most {MAX_ACCOUNTS} accounts at a time."}, 400
    reg = _registry()
    results: dict[str, dict] = {}
    for label in labels:
        try:
            entry = _entry(label)
        except Exception:
            entry = None
        if entry is None:
            results[label] = {"ok": False, "code": "not_in_map",
                              "message": "This account is not in the account map."}
            continue
        try:
            results[label] = reg.close_one(label, entry)
        except Exception as exc:  # noqa: BLE001
            log.exception("Closing the profile of %s failed", label)
            results[label] = {"ok": False, "code": "error", "message": f"{type(exc).__name__}"}
    return {"results": results}, 200


def prepare_connection(label: str, confirm_reopen: bool = False) -> tuple[dict,int]:
    """Prepare only the physical profile; the original read/write action has not started."""
    refused = _guard()
    if refused:
        return refused
    from src.fb_api.connection import prepare_existing,confirmed_reopen,may_reopen_manual,opening_closed_profile
    from src.fb_api.warm import defer_memory_wait,memory_deferred
    from src.fb_ads_rpa.operations import profile_lock
    entry = _entry(label)
    if entry is None:
        return {"ok":False,"account":label,"code":"not_in_map","error":"Unknown account"},400
    try:
        # ACL-2278: this request must not wait for memory (the page gives up after 45 s and the job would
        # fail for it). A shortage is not an error here: the job queues and opens the profile itself.
        with profile_lock(entry.profile_id).holding(timeout=0), confirmed_reopen(confirm_reopen), \
                defer_memory_wait():
            reg = _registry()
            kind,_ = reg._scan([entry.profile_id]).get(entry.profile_id,("unknown",None))
            if kind == "no_port":
                if not may_reopen_manual():
                    return {"ok":False,"account":label,"code":"open_by_hand"},409
                result = reg.open_one(label,entry)
                if not result.get("ok") and memory_deferred(result.get("code") or ""):
                    return {"ok":True,"account":label,"code":"memory_deferred","skipped":True},200
                return {"account":label,**result},200 if result.get("ok") else 409
            with opening_closed_profile():     # ACL-2262: /connection/prepare comes before Publish only
                prepare_existing(reg,entry,label)
        return {"ok":True,"account":label},200
    except Exception as exc:
        from src.fb_ads_rpa.profile_lease import ProfileLeaseError
        if memory_deferred(getattr(exc,"code","") or ""):
            return {"ok":True,"account":label,"code":"memory_deferred","skipped":True},200
        if isinstance(exc,ProfileLeaseError):
            # Held by a read or by the account's own job: the action queues or dedupes itself.
            return {"ok":True,"account":label,"code":"busy","skipped":True},200
        from src.fb_api.telemetry import scrub
        return {"ok":False,"account":label,"error":scrub(str(exc)),"code":getattr(exc,"code","") or "connection"},409
