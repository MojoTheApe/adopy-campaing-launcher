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
    with _AUTO_LOCK:
        auto = {l: dict(v) for l, v in _AUTO.items()}
    for label, info_one in list(got.items()):
        mark = auto.get(label)
        if mark:
            got[label] = {**info_one, "auto": mark["status"], "auto_code": mark.get("code", ""),
                          "auto_reason": REFUSAL_TEXT.get(mark.get("code", ""), "")}
    return {**info, "accounts": got}


# ───────────── ACL-2268: the profile is brought up when the person starts working with an account

#: One automatic attempt per physical profile per this time. Every outcome, a refusal included, uses it.
AUTO_RETRY_S = 30 * 60
#: Only when the profile lease was busy (no start was attempted): one more look after this time.
AUTO_REQUEUE_S = 60.0
AUTO_TRIGGERS = ("add", "duplicate", "edit")

#: Shown in the account chip tooltip only (never as a red line or a button).
REFUSAL_TEXT = {
    "busy": "another action is using this profile",
    "cooldown": "Meta paused this account",
    "open_by_hand": "it is open in a window started by hand",
    "closing": "it is still closing",
    "ceiling": "the profile limit is reached",
    "memory": "the computer is short of memory",
    "machine_busy": "the computer is busy",
    "jobs_waiting": "other runs are waiting",
    "start_failed": "Multilogin did not start it",
    "not_in_map": "the account is not in the account map",
}

_AUTO: dict[str, dict] = {}          # label -> {status: queued|opening|done|refused, code, retried}
_AUTO_QUEUE: list[str] = []
_AUTO_LOCK = threading.Lock()
_AUTO_WORKER: threading.Thread | None = None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _timer(delay: float, fn) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def auto_enabled() -> bool:
    try:
        from src.fb_api.connection import settings

        data = settings()
        return bool(data.get("auto_warm")) and bool(data.get("managed_sessions"))
    except Exception:  # noqa: BLE001 - unreadable settings: nothing opens by itself
        return False


def _publish_busy(label: str) -> bool:
    """A publish is running on this profile: it is left alone."""
    try:
        from src.analysis_agent import account_spend
        from src.web import publish_jobs

        return bool(publish_jobs.profile_in_use(account_spend._profile_key(label)))  # noqa: SLF001
    except Exception:  # noqa: BLE001 - not sure -> do not open
        log.exception("Could not tell whether %s is publishing", label)
        return True


def _auto_set(label: str, status: str, code: str = "", **extra: Any) -> None:
    with _AUTO_LOCK:
        prev = _AUTO.get(label, {})
        _AUTO[label] = {**prev, "status": status, "code": code, **extra}


def ensure_open(accounts: Any, trigger: Any = "", campaigns: Any = 0) -> tuple[dict, int]:
    """Queue the automatic warm-up of ONE account. Waits for nothing; refusals are quiet (200)."""
    refused = _guard()
    if refused:
        return refused
    labels = _clean_labels(accounts)
    try:
        count = int(campaigns)
    except (TypeError, ValueError):
        count = 0
    if len(labels) != 1 or trigger not in AUTO_TRIGGERS or count < 1:
        return {"error": "Automatic start needs one account, a trigger (add, duplicate or edit) "
                         "and an existing campaign."}, 400
    label = labels[0]
    if not auto_enabled():
        return {"accounts": {label: "off"}}, 200
    with _AUTO_LOCK:
        prev = _AUTO.get(label)
        if prev and prev["status"] in ("queued", "opening"):
            return {"accounts": {label: prev["status"]}}, 200
    try:
        entry = _entry(label)
    except Exception:  # noqa: BLE001
        entry = None
    pid = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
    if not pid:
        return {"accounts": {label: "not_in_map"}}, 200
    from src.web import auto_warm_store

    if not auto_warm_store.claim(pid, AUTO_RETRY_S):
        return {"accounts": {label: "recent"}}, 200
    with _AUTO_LOCK:
        _AUTO[label] = {"status": "queued", "code": "", "trigger": trigger, "campaigns": count}
        _AUTO_QUEUE.append(label)
        _start_worker_locked()
    return {"accounts": {label: "queued"}}, 202


def _start_worker_locked() -> None:
    global _AUTO_WORKER
    if _AUTO_QUEUE and (_AUTO_WORKER is None or not _AUTO_WORKER.is_alive()):
        _AUTO_WORKER = threading.Thread(target=_auto_worker, daemon=True, name="acl-warm-auto")
        _AUTO_WORKER.start()


def _manual_job_running() -> bool:
    with _JOBS_LOCK:
        return bool(_ACTIVE_JOB and _JOBS.get(_ACTIVE_JOB, {}).get("state") == "running")


def _auto_worker() -> None:
    """One account at a time; a person's «Open profiles for today» is never interrupted - wait for it."""
    global _AUTO_WORKER
    while True:
        with _AUTO_LOCK:
            if not _AUTO_QUEUE:
                if _AUTO_WORKER is threading.current_thread():
                    _AUTO_WORKER = None
                return
            label = _AUTO_QUEUE.pop(0)
        try:
            waited = 0
            while _manual_job_running() and waited < 600:
                _sleep(1.0)
                waited += 1
            _auto_one(label)
        except Exception as exc:  # noqa: BLE001 - the queue must not die on one account
            log.exception("Automatic warm-up of %s failed", label)
            _finish(label, "start_failed", started=False, error=type(exc).__name__)


def _requeue_later(label: str) -> None:
    def again() -> None:
        with _AUTO_LOCK:
            if _AUTO.get(label, {}).get("status") != "queued" or label in _AUTO_QUEUE:
                return
            _AUTO_QUEUE.append(label)
            _start_worker_locked()

    _timer(AUTO_REQUEUE_S, again)


_OK_CODES = ("started", "already_open")


def _finish(label: str, code: str, *, started: bool, **diag: Any) -> None:
    """Record the end of one attempt: chip state, telemetry row, event. Numbers and codes only."""
    _auto_set(label, "done" if code in _OK_CODES else "refused", code)
    with _AUTO_LOCK:
        mark = dict(_AUTO.get(label, {}))
    body = {"trigger": mark.get("trigger", ""), "outcome": code, "started": bool(started),
            "retried": bool(mark.get("retried")), **{k: v for k, v in diag.items() if v is not None}}
    try:
        from src.fb_ads_rpa import run_telemetry

        run_telemetry.record("job_phase", code in _OK_CODES, note="auto_warm",
                             duration_s=diag.get("duration_s"), diag={"account": label, **body})
    except Exception:  # noqa: BLE001 - telemetry never breaks the warm-up
        log.debug("auto_warm telemetry failed", exc_info=True)
    try:
        from src.acl_event_log import emit

        emit("profile_auto_warm", body, account=label)
    except Exception:  # noqa: BLE001
        log.debug("auto_warm event failed", exc_info=True)


def _admission(reg: Any, pid: str) -> tuple[str, dict]:
    """Stricter than a person's action. Local readings only. Empty code - go ahead."""
    numbers: dict = {}
    try:
        from src.web import publish_jobs

        snap = publish_jobs.slots_snapshot()
    except Exception:  # noqa: BLE001 - cannot tell -> do not start
        return "machine_busy", numbers
    if snap.get("waiting"):
        return "jobs_waiting", numbers
    open_ids = set(reg.open_profile_ids()) | {r.get("profile") for r in snap.get("running") or []}
    open_ids.discard("")
    open_ids.discard(None)
    ceiling = int((snap.get("settings") or {}).get("ceiling") or 0)
    numbers["open_profiles"] = len(open_ids)
    numbers["ceiling"] = ceiling
    if pid not in open_ids and len(open_ids) >= ceiling:
        return "ceiling", numbers
    if not (snap.get("next") or {}).get("allow", True):
        return "memory", numbers
    try:
        load = reg.load_snapshot(pid)
    except Exception:  # noqa: BLE001
        return "machine_busy", numbers
    numbers["used_pct"] = load.get("used_pct")
    if load.get("busy") or reg.warming_others(pid):
        return "machine_busy", numbers
    return "", numbers


def _auto_one(label: str) -> None:
    """The one opening path: the lease, a local scan, the refusals, then `prepare_existing` in quiet mode."""
    began = time.monotonic()
    try:
        entry = _entry(label)
    except Exception:  # noqa: BLE001
        entry = None
    pid = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
    if not pid:
        _finish(label, "not_in_map", started=False)
        return
    from src.fb_ads_rpa.operations import profile_lock
    from src.fb_ads_rpa.profile_lease import ProfileLeaseError

    _auto_set(label, "opening")
    reg = _registry()
    try:
        with profile_lock(pid).holding(timeout=0):
            code, started, numbers = _under_lease(label, entry, pid, reg)
    except ProfileLeaseError:
        with _AUTO_LOCK:
            retried = bool(_AUTO.get(label, {}).get("retried"))
            if not retried:
                _AUTO[label] = {**_AUTO.get(label, {}), "status": "queued", "code": "", "retried": True}
        if retried:
            _finish(label, "busy", started=False)
        else:
            _requeue_later(label)
        return
    numbers["duration_s"] = round(time.monotonic() - began, 1)
    _finish(label, code, started=started, **numbers)
    if started:
        _sleep(reg._stagger())  # noqa: SLF001 - the same 5-10 s gap as «Open profiles for today»


_WARM_CODES = {"memory_low": "memory", "profile_closing": "closing", "busy": "busy", "profile_lease": "busy",
               "restart_cooldown": "cooldown", "open_by_hand": "open_by_hand"}


def _under_lease(label: str, entry: Any, pid: str, reg: Any) -> tuple[str, bool, dict]:
    from src.fb_api import connection, mlx_session, stop_log, warm

    if reg.is_warm(pid):
        return "already_open", False, {}
    kind, _port = reg._scan([pid]).get(pid, ("unknown", None))  # noqa: SLF001
    if kind == "no_port":
        return "open_by_hand", False, {}
    if kind == "closing":
        return "closing", False, {}
    if _publish_busy(label):
        return "busy", False, {}
    try:
        from src.web import publish_api

        if publish_api._cooldown(label)[0] > 0 or stop_log.default().restart_wait_s(pid) > 0:  # noqa: SLF001
            return "cooldown", False, {}
    except Exception:  # noqa: BLE001 - cannot tell -> do not open
        return "cooldown", False, {}
    numbers: dict = {}
    if kind != "port":      # a profile that is already running is only adopted: no admission needed
        why, numbers = _admission(reg, pid)
        if why:
            return why, False, numbers
    batch = f"auto-{uuid.uuid4().hex[:6]}"
    mlx_session.begin_batch(batch)
    try:
        with mlx_session.scope(account=label, batch=batch), warm.quiet_start(), \
                connection.opening_closed_profile():
            ok = connection.prepare_existing(reg, entry, label)
    except warm.WarmError as exc:
        return _WARM_CODES.get(getattr(exc, "code", "") or "", "start_failed"), False, numbers
    if not ok:
        return ("cooldown" if stop_log.default().restart_wait_s(pid) > 0 else "start_failed"), False, numbers
    sess = reg.session(pid)
    started = bool(sess is not None and getattr(sess, "dashboard_started", False))
    return ("started" if started else "already_open"), started, numbers


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
            reg.open_many(items, job_id=job_id, on_result=lambda l, r: _on_open_result(job_id, l, r))
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


def request_status_read(label: str) -> str:
    """ACL-2419: после удачного Open — одна заявка в существующую очередь обновления.

    Возвращает `queued` | `skipped`. Очередь сама проверяет замок профиля, паузу,
    лимит вызовов и недавний отказ; здесь нет ни таймера, ни собственного Graph-вызова.
    """
    try:
        from src.analysis_agent import account_refresh

        got = account_refresh.queue().request(label, "open", force=True)
        return "queued" if got.get("queued") else "skipped"
    except Exception:  # noqa: BLE001 — открытие профиля уже удалось, статус вторичен
        log.exception("Could not queue the status read after opening %s", label)
        return "skipped"


def _on_open_result(job_id: str, label: str, result: dict) -> None:
    result = dict(result)
    # Только удачное «окно поднято»: открытие руками, закрытие и отказ ничего не ставят в очередь.
    if result.get("ok"):
        result["status_read"] = request_status_read(label)
    else:
        result["status_read"] = "n/a"
    _set_result(job_id, label, result)


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
