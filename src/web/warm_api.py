"""Эндпоинты тёплых сессий: состояние строк, «Open profiles for today», «Close profiles» (ACL-1812).

Только при `ACL_FB_BACKEND=api`; при `rpa` — чёткий отказ, а состояние отвечает
`available: false`. Логика — в `src/fb_api/warm.py`; здесь проводка, коды ответов
и фоновое задание запуска (запуски идут по одному, без паузы между ними — ACL-2668 #14).

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
from datetime import datetime
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

    return {"available": True, "actions": True,
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
_AUTO_RUNNING = [0]  # automatic warm-ups being opened right now (each in its own thread)
# ACL-2668 №39: профили, которые автоподъём уже пропустил через допуск и сейчас поднимает.
# Допуск считает их занятыми местами, чтобы параллельные подъёмы не перешли потолок профилей.
_AUTO_ADMITTED: set[str] = set()
_ADMIT_LOCK = threading.Lock()


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
    """ACL-2668 №39: каждая заявка — свой поток, без очереди «по одному на машину».

    Сколько профилей поднимается одновременно, решают только потолок профилей и порог
    памяти в `_admission`.
    """
    while _AUTO_QUEUE:
        label = _AUTO_QUEUE.pop(0)
        _AUTO_RUNNING[0] += 1
        threading.Thread(target=_auto_worker, args=(label,), daemon=True, name="acl-warm-auto").start()


def running_job_count() -> int:
    """Warm-session launch work that is not finished: manual jobs plus automatic warm-ups.

    Automatic warm-ups count while queued and while they are being opened.
    """
    with _JOBS_LOCK:
        n = sum(1 for j in _JOBS.values() if j.get("state") in ("running", "waiting"))
    with _AUTO_LOCK:
        n += len(_AUTO_QUEUE) + _AUTO_RUNNING[0]
    return n


def _manual_job_running() -> bool:
    with _JOBS_LOCK:
        return any(j.get("state") == "running" and j.get("kind") == "open" for j in _JOBS.values())


def _auto_worker(label: str) -> None:
    """One account; a person's «Open profiles for today» is never interrupted - wait for it."""
    try:
        waited = 0
        while _manual_job_running() and waited < 600:
            _sleep(1.0)
            waited += 1
        _auto_one(label)
    except Exception as exc:  # noqa: BLE001 - one account must not break the others
        log.exception("Automatic warm-up of %s failed", label)
        _finish(label, "start_failed", started=False, error=type(exc).__name__)
    finally:
        with _AUTO_LOCK:
            _AUTO_RUNNING[0] = max(0, _AUTO_RUNNING[0] - 1)


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
    if code in _OK_CODES:
        # ACL-2668 №39: как после ручного Open profile (ACL-2419) — одна заявка на чтение статуса,
        # чтобы к Check plan/Publish статус аккаунта был свежим. Своих таймеров здесь нет.
        diag["status_read"] = request_status_read(label)
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
    with _AUTO_LOCK:
        open_ids |= _AUTO_ADMITTED - {pid}      # параллельные автоподъёмы уже заняли свои места
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
    # ACL-2668 №39: другие прогревающиеся профили больше не повод ждать — подъёмы идут параллельно.
    if "cpu_high" in (load.get("reasons") or ()):
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
        # Допуск и бронь места — одним шагом, иначе два параллельных подъёма увидят одно свободное место.
        with _ADMIT_LOCK:
            why, numbers = _admission(reg, pid)
            if why:
                return why, False, numbers
            with _AUTO_LOCK:
                _AUTO_ADMITTED.add(pid)
    try:
        return _start_quietly(label, entry, pid, reg, numbers)
    finally:
        with _AUTO_LOCK:
            _AUTO_ADMITTED.discard(pid)


def _start_quietly(label: str, entry: Any, pid: str, reg: Any, numbers: dict) -> tuple[str, bool, dict]:
    from src.fb_api import connection, mlx_session, stop_log, warm

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
        # ACL-2870: задания разных аккаунтов идут параллельно; отказ — только если аккаунт уже открывается.
        for jid, other in _JOBS.items():
            if other.get("state") == "running" and set(labels) & set(other.get("results") or {}):
                return {"error": "Profiles are already being opened; wait for it to finish.",
                        "job_id": jid}, 409
        job_id = uuid.uuid4().hex[:10]
        job = {"job_id": job_id, "state": "running", "kind": "open", "started": time.time(),
               "total": len(labels), "results": {l: {"state": "waiting"} for l in labels}}
        _JOBS[job_id] = job
        _ACTIVE_JOB = job_id
        # ACL-2870: чистим только завершённые задания — бегущее нельзя терять, его опрашивает строка
        done = [j for j in sorted(_JOBS, key=lambda j: _JOBS[j]["started"]) if _JOBS[j].get("state") != "running"]
        for old in done[:max(0, len(_JOBS) - 10)]:
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


def _local_tabs(label: str, pid: str):
    """ACL-2847: `(address, title)` of the open tabs of an already running profile, from its local debugging port only.

    The registered session's port, else one local process look (no network). A profile that is not running: nothing."""
    from src.fb_api import warm

    reg = _registry()
    sess = reg.session(pid) if pid else None
    port = getattr(sess, "port", 0) if sess is not None else 0
    if not port and pid:
        try:
            kind, found = reg._scan([pid]).get(pid, ("unknown", None))  # noqa: SLF001
            port = int(found or 0) if kind == "port" else 0
        except Exception:  # noqa: BLE001 - no look is "not detected"
            port = 0
    return warm.local_page_targets(port) if port else []


def note_tab_lock(label: str, *, urls=None, action: str = "Open profile") -> bool:
    """ACL-2653/2847: a running profile whose tab is on facebook.com/checkpoint/... or the login page is held.

    Reads only the tab addresses and titles from the profile's local debugging port; no Graph call, no navigation.
    Writes a hold (person-level, no Graph code) and returns True. This also covers a profile the owner opened by hand
    or that fell to the login page while it stayed open. When the tabs are readable and none is a lock any more, a
    login-page hold ends (the person logged in): the next Publish goes on without a separate Retry."""
    try:
        from src.analysis_agent import account_spend
        from src.fb_api import account_blocks

        pid = _profile_id(label)
        if urls is None:
            urls = _local_tabs(label, pid)
        key = account_spend._profile_key(label)  # noqa: SLF001
        account_blocks.reset_lock_seen()     # ACL-2668: this look decides afresh
        # ACL-2841: a tab is an address or an `(address, title)` pair; the title is Meta's own text for the lock.
        pairs = [(u, "") if isinstance(u, str) else (str(u[0]), str(u[1])) for u in urls]
        found = any([account_blocks.note_tab_url(key, label, u, action, t) for u, t in pairs])
        if not found and pairs:
            held = account_blocks.hold_of(key)
            if held and held.get("kind") == "checkpoint" and held.get("page") == "login":
                account_blocks.clear(key, "relogin", kind="checkpoint")
        return found
    except Exception:  # noqa: BLE001 - the open already worked; a failed look is only "not detected"
        log.debug("The profile tab was not checked for a Facebook lock", exc_info=True)
        return False


#: label -> why the Open's status read was not queued (empty: it was). Read once by `_on_open_result`.
_SKIP_REASON: dict[str, str] = {}
#: label -> the sentence the row shows about this Open's status read (ACL-2649); read once by `_on_open_result`.
_OPEN_NOTE: dict[str, str] = {}

_SKIP_TEXT = {
    "removed": "The account is not in the account list any more.",
    "invalid_account_map": "The account map entry is not valid; check the account connection.",
    "fresh": "The status was read a moment ago.",
    "profile_held": "This profile is on hold; press Retry on this profile.",
}


def open_read_note(got: dict) -> str:
    """ACL-2649: one English sentence for the row about the Open's status read, or "" when nothing needs saying.

    A read that is already running, a paused queue (with the time it may read again) and a skipped request are
    named instead of the generic "not read now". Pure text: nothing is read or timed here."""
    reason = str((got or {}).get("reason") or "")
    if reason == "running":
        return "A status read is already running for this account; the status updates when it ends."
    if reason == "halted":
        until = (got or {}).get("until")
        try:
            when = datetime.fromtimestamp(float(until)).strftime("%H:%M") if until else ""
        except (TypeError, ValueError, OverflowError, OSError):
            when = ""
        return (f"Reads are paused until {when}; this status read starts then." if when
                else "Reads are paused on this machine; this status read starts as soon as they resume.")
    if got and not got.get("queued"):
        return _SKIP_TEXT.get(reason, "")
    return ""
_SKIP_LOCK = threading.Lock()


def request_status_read(label: str) -> str:
    """ACL-2419: после удачного Open — одна заявка в существующую очередь обновления.

    Возвращает `queued` | `skipped`. Очередь сама проверяет замок профиля, паузу,
    лимит вызовов и недавний отказ; здесь нет ни таймера, ни собственного Graph-вызова.
    """
    try:
        from src.analysis_agent import account_refresh

        try:
            from src.analysis_agent import account_spend
            from src.fb_api import account_blocks

            key_ = account_spend._profile_key(label)  # noqa: SLF001 - ACL-2652
            # A reopened profile has a fresh tab: a "tab stopped answering" hold (never another kind) ends here.
            account_blocks.lift_frozen_hold(key_, "open")
            account_blocks.mark_person_open(key_)
        except Exception:  # noqa: BLE001
            log.debug("could not mark the person's open", exc_info=True)
        # ACL-2668/2653: Open profile is the person's own press: the `open` read passes a held profile like Retry.
        got = account_refresh.queue().request(label, "open", force=True)
        reason = str(got.get("reason") or "")
        if reason == "halted":
            try:
                got = {**got, "until": ((account_refresh.queue().state() or {}).get("halt") or {}).get("until")}
            except Exception:  # noqa: BLE001 - the time is only a nicety for the note
                log.debug("The queue pause time could not be read", exc_info=True)
        with _SKIP_LOCK:
            _SKIP_REASON[label] = "" if got.get("queued") else reason
            _OPEN_NOTE[label] = open_read_note(got)
        # A read that is already running for this account answers the Open as well.
        return "queued" if got.get("queued") or reason == "running" else "skipped"
    except Exception:  # noqa: BLE001 — открытие профиля уже удалось, статус вторичен
        log.exception("Could not queue the status read after opening %s", label)
        return "skipped"


def _on_open_result(job_id: str, label: str, result: dict) -> None:
    result = dict(result)
    # Только удачное «окно поднято»: открытие руками, закрытие и отказ ничего не ставят в очередь.
    if result.get("ok"):
        # ACL-2653: a person pressed Open: the account's status is read now, whatever the queue, a halt or a hold.
        # A tab on a Facebook lock is recorded from its address first; the read then shows Meta's refusal and its time.
        result["locked"] = bool(note_tab_lock(label))
        result["status_read"] = request_status_read(label)
        with _SKIP_LOCK:
            result["status_reason"] = _SKIP_REASON.pop(label, "")
            result["status_note"] = _OPEN_NOTE.pop(label, "")
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


def login_refusal(label: str) -> str:
    """ACL-2847: the refusal text when the profile of this account is held as logged out of Facebook, else an empty string."""
    try:
        from src.analysis_agent import account_spend
        from src.fb_api import account_blocks

        key = account_spend._profile_key(label)  # noqa: SLF001
        if not account_blocks.is_login_hold(account_blocks.hold_of(key)):
            return ""
        got = account_blocks.refusal(key, label, "publish")
        return got[0] if got else ""
    except Exception:  # noqa: BLE001 - an unreadable hold file never stops an action
        return ""


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
            # ACL-2847: a profile that is open but sits on the Facebook login page stops the action here, at once.
            note_tab_lock(label, action="Publish")
            stop = login_refusal(label)
            if stop:
                return {"ok":False,"account":label,"code":"account_blocked","error":stop},409
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
