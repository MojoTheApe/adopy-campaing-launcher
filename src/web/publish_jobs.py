"""Background publish job store and runner.

Keeps an in-memory dict of running/completed publish jobs.
Each job runs in a daemon thread so the web server stays responsive.
Progress is streamed by appending to job["results"] as campaigns complete.

The campaign-manager dashboard is draft-only and uses RPA (Playwright + Ads
Manager UI).  The former Marketing API backend is intentionally not reachable
from this module: it could create live ACTIVE objects from client-controlled
form fields and violated the product's manual-final-publish boundary.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from config.paths import get_draft_status_path
from src.fb_ads_rpa.creative_compress import observe_preparation
from src.fb_ads_rpa.config import use_rpa_publish
from src.fb_ads_rpa.draft import RpaPublishResult
from src.fb_ads_rpa.rpa_publisher import RpaPublisher, profile_key_for
from src.fb_ads_rpa.operations import publish_lease
from src.web.keep_awake import keep_display_awake
from src.web.profile_slots import ProfileSlots

# ── In-memory job store ────────────────────────────────────────────────
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_update_reserved = False
# Serialises publishing PER PROFILE — see _run_job. Two accounts on two
# profiles run at once; two jobs on one profile still queue (acl-1210).
_publish_serial = publish_lease
_active_publishers: dict[str, RpaPublisher] = {}
# Сколько профилей публикуют разом — по свободной памяти машины (ACL-1321).
# Задания сверх решения ждут в «queued» в порядке отправки; см. profile_slots.
_slots = ProfileSlots()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_creative_notice(job_id: str, note: str) -> None:
    with _jobs_lock:
        notices = _jobs[job_id].setdefault("creative_notices", [])
        if note not in notices:
            notices.append(note)


def _job_result_from_rpa(result: RpaPublishResult) -> dict[str, Any]:
    d = result.to_job_dict()
    d["ts"] = _now_iso()
    return d


def create_job(
    account_label: str,
    country: str,
    slots: list[dict[str, Any]],
    *,
    via_multilogin: bool = True,
    activate: bool = False,
    dry_run: bool = False,
    hand_off: bool = False,
    total: int | None = None,
    campaign_names: list[str] | None = None,
) -> str:
    """Create a new publish job, store it, return its job_id."""
    if activate:
        raise ValueError("ACL is draft-only: live activation is disabled")
    if not use_rpa_publish():
        raise RuntimeError(
            "The legacy Meta API backend is disabled in the campaign-manager dashboard"
        )
    # Campaign-manager jobs always leave the verified draft open for a human.
    hand_off = True
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        if _update_reserved:
            raise RuntimeError("ACL is restarting for an update. Wait until it returns before starting a campaign.")
        _jobs[job_id] = {
            "job_id": job_id,
            "account_label": account_label,
            "country": country,
            "via_multilogin": via_multilogin,
            "activate": activate,
            "dry_run": dry_run,
            "hand_off": hand_off,
            "backend": "rpa" if use_rpa_publish() else "api",
            "status": "pending",
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "results": [],
            "creative_notices": [],
            "errors": [],
            "total": total if total is not None else len(slots),
            # Имена кампаний в порядке сборки: по ним ящик билдера находит
            # строку кампании и ставит на неё точку состояния (ACL-1315).
            "campaign_names": list(campaign_names or []),
            "done_count": 0,
            "cancel_requested": False,
        }
    return job_id


def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation of a running/pending job.

    Stops RPA and disconnects CDP; Multilogin window stays open for FB login check.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "Job not found"}
        if job["status"] in ("done", "cancelled"):
            return {"ok": False, "error": f"Job already {job['status']}"}
        job["cancel_requested"] = True
        # Surface immediate feedback in the poll UI before the worker finishes.
        if job["status"] in ("pending", "queued", "running"):
            job["status"] = "cancelling"
        publisher = _active_publishers.get(job_id)

    # Задание, ждущее места в очереди профилей, узнаёт об отмене сразу, а не
    # по таймеру. Вне замка заданий: очередь берёт его из-под своего.
    _slots.wake()

    if publisher is not None:
        _cancel_publisher(publisher)

    return {"ok": True, "job_id": job_id}


def _cancel_publisher(publisher: Any) -> None:
    """Отменить публикатор, не трогая поток с работающим циклом asyncio.

    `publisher.cancel()` закрывает sync Playwright задания, а закрытие
    (`BrowserConnection.close` → `_reset_asyncio_running_loop`) стирает метку
    «текущий цикл» того потока, где выполняется. Вызванная из цикла uvicorn,
    отмена оставляла сервер без цикла: 29.09 12:55 — «loop … is not the
    running loop», «There is no current event loop in thread 'MainThread'»,
    дешборд перестал отвечать (ACL-1380). Маршрут отмены уже зовёт нас в
    потоке; это страховка для любого другого вызова из async-кода.
    """
    def _cancel() -> None:
        try:
            publisher.cancel()
        except Exception:
            pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _cancel()  # обычный поток — отменяем здесь же
        return
    threading.Thread(target=_cancel, daemon=True, name="acl-job-cancel").start()


def slots_snapshot() -> dict[str, Any]:
    """Сколько профилей разом разрешено сейчас, что идёт и что ждёт (ACL-1321)."""
    return _slots.snapshot()


def active_job_count() -> int:
    """Jobs still working. An ACL update restarts the server, and a restart in
    the middle of a run leaves a half-built campaign in Ads Manager with nobody
    watching — so the update button asks this first."""
    with _jobs_lock:
        return sum(
            1 for job in _jobs.values()
            if job.get("status") in {"pending", "queued", "running", "cancelling"}
        )


def reserve_update() -> bool:
    """Atomically refuse active jobs and block new jobs until restart."""
    global _update_reserved
    with _jobs_lock:
        if _update_reserved or any(job.get("status") in {"pending", "queued", "running", "cancelling"}
                                   for job in _jobs.values()):
            return False
        _update_reserved = True
        return True


def release_update() -> None:
    global _update_reserved
    with _jobs_lock:
        _update_reserved = False


def _is_cancelled(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return bool(job and job.get("cancel_requested"))


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        # Deep-copy mutable lists so callers see a stable snapshot
        snap = dict(job)
        snap["results"] = list(job["results"])
        snap["errors"] = list(job["errors"])
        snap["creative_notices"] = list(job.get("creative_notices", []))
        snap["campaign_names"] = list(job.get("campaign_names", []))
        if isinstance(job.get("queue"), dict):
            snap["queue"] = dict(job["queue"])
        expected = int(snap.get("total") or 0)
        snap["publishable"] = (
            snap.get("status") == "done"
            and expected > 0
            and int(snap.get("done_count") or 0) == expected
            and len(snap["results"]) == expected
            and not snap["errors"]
            and all(_result_is_publishable(result) for result in snap["results"])
        )
        return snap


def _clear_inherited_asyncio_loop() -> None:
    """Clear asyncio running-loop marks before sync Playwright in a worker thread.

    Also stop any leaked sync Playwright driver from a prior failed CDP attach
    in this thread (that leaves get_running_loop() returning a running loop and
    triggers 'Playwright Sync API inside the asyncio loop' on the next start).
    """
    try:
        from src.fb_ads_rpa.browser import _reset_asyncio_running_loop, _stop_thread_playwright

        _stop_thread_playwright()
        _reset_asyncio_running_loop()
    except Exception:
        try:
            import asyncio
            import asyncio.events as _ae

            _rl = getattr(_ae, "_running_loop", None)
            if _rl is not None and hasattr(_rl, "set"):
                _rl.set(None)
            elif _rl is not None and hasattr(_rl, "loop_pid"):
                _rl.loop_pid = (None, None)
            setter = getattr(_ae, "_set_running_loop", None)
            if callable(setter):
                setter(None)
        except Exception:
            pass


@contextlib.contextmanager
def _watch_screen_lock(job_id: str):
    """Remember a local session lock observed while this job is working.

    Sample every fifteen seconds; unknown/unavailable probes are not evidence
    of a lock. No network or live browser operations are used here.
    """
    stop = threading.Event()

    def look() -> None:
        try:
            from src.fb_ads_rpa.run_telemetry import screen_locked
        except Exception:
            return
        while not stop.is_set():
            try:
                if screen_locked() is True:
                    with _jobs_lock:
                        job = _jobs.get(job_id)
                        if (not stop.is_set() and job is not None
                                and job.get("status") in {"running", "cancelling"}):
                            job["screen_was_locked"] = True
                    return          # узнали — больше смотреть незачем
            except Exception:
                return
            stop.wait(15.0)

    try:
        watcher = threading.Thread(target=look, daemon=True, name=f"screenlock-{job_id[:8]}")
        watcher.start()
    except Exception:
        # Monitoring is optional; inability to create its thread must not
        # strand a running job before RPA starts.
        stop.set()
    try:
        yield
    finally:
        # Serialize exit with the write above. A delayed OS probe must never
        # add a warning after the job or this monitoring scope has finished.
        with _jobs_lock:
            stop.set()


def _run_job(
    job_id: str,
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    via_multilogin: bool,
    activate: bool,
    dry_run: bool = False,
    hand_off: bool = False,
) -> None:
    """Runs in a daemon thread. Updates _jobs[job_id] as it progresses."""
    # Must be first: clear any asyncio event loop inherited from the parent
    # async handler (Python 3.14+ ContextVar propagation via threading.Thread).
    _clear_inherited_asyncio_loop()

    # One publish at a time PER PROFILE. Until 2026-09-16 this was one at a
    # time per process (2026-07-31: "parallel profiles are a later,
    # deliberate step"); the owner took that step in acl-1210. Two accounts
    # on two profiles run at once, each in its own thread with its own
    # Playwright; two jobs on the same profile still queue here in "queued"
    # until the first finishes — never two drivers on one browser. The lease
    # is held for the WHOLE run, not just the status flip.
    #
    # ACL-1321: и не больше профилей разом, чем тянет память машины. Место
    # даёт `_slots` (в порядке отправки; задание на занятый профиль ждёт свой
    # профиль, не держа места), замок профиля берётся уже с местом.
    with _jobs_lock:
        job = _jobs[job_id]
        if job.get("status") == "pending":
            job["status"] = "queued"
        job["queued_at"] = _now_iso()
        first_campaign = next(iter(job.get("campaign_names") or []), "")
    # Каждая строка телеметрии этого потока несёт номер задания — по нему
    # точка сборки берёт только свои строки (ACL-1315). С начала ожидания:
    # строка очереди тоже его.
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        _rt.set_current_job(job_id)
    except Exception:
        _rt = None

    def _show_wait(verdict: dict[str, Any]) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job["queue"] = {
                    # «profile» — ждёт свой профиль; иначе — свободное место.
                    "reason": "profile" if verdict.get("reason") == "profile" else "slot",
                    "ahead": int(verdict.get("ahead") or 0),
                    "limit": verdict.get("limit"),
                    "running": verdict.get("running"),
                    "available_mb": verdict.get("available_mb"),
                }

    profile = profile_key_for(account_label)
    grant = _slots.acquire(
        job_id,
        profile,
        cancelled=lambda: _is_cancelled(job_id),
        on_wait=_show_wait,
    )
    if grant is None:
        with _jobs_lock:
            _jobs[job_id].pop("queue", None)
        _finish(job_id, cancelled=True)
        return
    try:
        with _jobs_lock:
            _jobs[job_id].pop("queue", None)
            _jobs[job_id]["slot_wait_s"] = grant["waited_s"]
        # Сколько задание ждало места и почему его пустили — в телеметрию.
        if _rt is not None:
            try:
                _rt.record("job_phase", True, campaign=first_campaign, note="slot_queue",
                           duration_s=float(grant["waited_s"]),
                           diag={"account": account_label, "decision": grant["decision"],
                                 "first_hold": grant["first_hold"]})
            except Exception:
                pass
        with _publish_serial(profile):
            if _is_cancelled(job_id):
                _finish(job_id, cancelled=True)
                return
            with _jobs_lock:
                _jobs[job_id]["status"] = "running"
                _jobs[job_id]["started_at"] = _now_iso()

            if activate or not use_rpa_publish():
                _fatal(job_id, "ACL safety gate: only draft-only RPA jobs are allowed")
                return
            # Заблокированный экран останавливает сборку: система уходит на
            # защищённый рабочий стол, браузер перестаёт получать время, и
            # креатив, который грузится 74 секунды, грузился 42 минуты. Пока
            # работа идёт, просим не гасить экран; просьба снимается сама,
            # чем бы работа ни кончилась. Держится она вокруг ВСЕЙ работы, а
            # не вокруг шага: прерывать нельзя всю. Очередь в это время ждёт
            # снаружи — держать экран ради ожидания незачем.
            with keep_display_awake(account_label), _watch_screen_lock(job_id):
                _run_rpa(
                    job_id,
                    slots,
                    account_label=account_label,
                    country=country,
                    activate=False,
                    dry_run=dry_run,
                    hand_off=True,
                )
    finally:
        _slots.release(job_id)


def _append_result(job_id: str, res: dict[str, Any]) -> None:
    with _jobs_lock:
        _jobs[job_id]["results"].append(res)
        _jobs[job_id]["done_count"] += 1


def _fatal(job_id: str, msg: str) -> None:
    with _jobs_lock:
        _jobs[job_id]["errors"].append(msg)
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["finished_at"] = _now_iso()
        account_label = _jobs[job_id].get("account_label", "")
        results = list(_jobs[job_id].get("results", []))
        errors = list(_jobs[job_id].get("errors", []))
        expected_total = int(_jobs[job_id].get("total") or 0)
    _write_draft_status(account_label, results, errors, expected_total=expected_total)


def _result_is_publishable(result: dict[str, Any]) -> bool:
    """Fail closed when a producer omitted the explicit publishability verdict."""
    return (
        result.get("success") is True
        and result.get("publishable") is True
        and not (result.get("blocking_failures") or [])
    )


def _summarize_results(
    results: list[dict],
    errors: list[str] | None = None,
    *,
    expected_total: int | None = None,
) -> dict[str, Any]:
    """Build the one persistent verdict the dashboard may display after restart."""
    errors = [str(e) for e in (errors or []) if str(e).strip()]
    publishable = [r for r in results if _result_is_publishable(r)]
    blocked = [r for r in results if r.get("success") is True and not _result_is_publishable(r)]
    failed = [r for r in results if r.get("success") is not True]
    expected_matches = expected_total is None or (
        expected_total > 0 and len(results) == expected_total
    )
    ready = (
        bool(results)
        and expected_matches
        and len(publishable) == len(results)
        and not errors
    )
    blocking_failures = [
        str(failure)
        for result in blocked
        for failure in (result.get("blocking_failures") or [])
    ]
    return {
        "state": "ready" if ready else "blocked",
        "ts": _now_iso(),
        "count": len(results),
        "publishable_count": len(publishable),
        "blocked_count": len(blocked),
        "failed_count": len(failed),
        "expected_count": expected_total,
        "campaign_names": [str(r.get("campaign_name") or "?") for r in results],
        "blocking_failures": blocking_failures,
        "errors": errors,
    }


def _write_draft_status(
    account_label: str,
    results: list[dict],
    errors: list[str] | None = None,
    *,
    expected_total: int | None = None,
) -> None:
    """Persist a truthful verdict in durable machine-local state."""
    import json as _json
    from pathlib import Path as _Path

    if not account_label:
        return
    path = _Path(get_draft_status_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.is_file():
            try:
                with open(path, encoding="utf-8") as f:
                    existing = _json.load(f)
            except Exception:
                existing = {}
        existing[account_label] = _summarize_results(
            results,
            errors,
            expected_total=expected_total,
        )
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            _json.dump(existing, f, ensure_ascii=False, indent=2)
            f.flush()
            import os as _os

            _os.fsync(f.fileno())
        temp_path.replace(path)
    except Exception:
        pass  # Non-fatal


def _finish(job_id: str, *, cancelled: bool = False) -> None:
    with _jobs_lock:
        _jobs[job_id]["status"] = "cancelled" if cancelled else "done"
        _jobs[job_id]["finished_at"] = _now_iso()
        account_label = _jobs[job_id].get("account_label", "")
        results = list(_jobs[job_id].get("results", []))
        errors = list(_jobs[job_id].get("errors", []))
        expected_total = int(_jobs[job_id].get("total") or 0)
        _active_publishers.pop(job_id, None)
    if not cancelled:
        _write_draft_status(
            account_label,
            results,
            errors,
            expected_total=expected_total,
        )


def _run_rpa(
    job_id: str,
    slots: list[dict[str, Any]],
    *,
    account_label: str,
    country: str,
    activate: bool,
    dry_run: bool = False,
    hand_off: bool = False,
) -> None:
    publisher = RpaPublisher(dry_run=dry_run, hand_off=hand_off)
    with _jobs_lock:
        _active_publishers[job_id] = publisher
    try:
        drafts = publisher.build_drafts(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
            preview_mode=False,
        )
    except Exception as e:
        _fatal(job_id, f"Failed to build campaign drafts: {e}")
        # Финальное ревью PR #314 (d): исключение сборки черновиков кончает
        # задание так же, как исключение публикации, — и тоже едет в Sentry
        # со стеком (раньше — только строка в задании).
        try:
            from src import acl_sentry

            acl_sentry.report_job_exception(e, job=job_id, account=account_label, phase="build_drafts")
        except Exception:
            pass
        with _jobs_lock:
            _active_publishers.pop(job_id, None)
        return

    with _jobs_lock:
        _jobs[job_id]["total"] = len(drafts)
        _jobs[job_id]["campaign_names"] = [str(getattr(d, "name", "") or "") for d in drafts]

    if not drafts:
        _fatal(job_id, "No campaigns to publish.")
        with _jobs_lock:
            _active_publishers.pop(job_id, None)
        return

    ready: list[Any] = []
    for draft in drafts:
        if _is_cancelled(job_id):
            break
        if draft.errors:
            _append_result(
                job_id,
                _job_result_from_rpa(
                    RpaPublishResult(
                        campaign_name=draft.name,
                        success=False,
                        error="; ".join(draft.errors),
                    )
                ),
            )
        else:
            ready.append(draft)

    if _is_cancelled(job_id):
        with _jobs_lock:
            _jobs[job_id]["errors"].append("Остановлено пользователем")
        # Never stop Multilogin — user needs the window to verify FB login.
        publisher.close(force=False)
        _finish(job_id, cancelled=True)
        return

    if not ready:
        _finish(job_id)
        return

    cancelled = False
    try:
        for draft in ready:
            if _is_cancelled(job_id):
                cancelled = True
                break
            with observe_preparation(lambda note: _append_creative_notice(job_id, note)):
                result = publisher.publish_draft(draft)
            _append_result(job_id, _job_result_from_rpa(result))
    except Exception as e:
        if _is_cancelled(job_id):
            cancelled = True
            with _jobs_lock:
                _jobs[job_id]["errors"].append("Остановлено пользователем")
        else:
            with _jobs_lock:
                _jobs[job_id]["errors"].append(f"RPA error: {e}")
            # ACL-1318: исключение, которым кончилась кампания, — в Sentry, со
            # стеком и через тот же вычиститель; отправка работу не роняет.
            try:
                from src import acl_sentry

                acl_sentry.report_job_exception(e, job=job_id, account=account_label, phase="publish")
            except Exception:
                pass
    finally:
        # Disconnect CDP only; Multilogin window stays open for FB login check.
        publisher.close(force=False)
        _finish(job_id, cancelled=cancelled or _is_cancelled(job_id))


def start_job(
    account_label: str,
    country: str,
    slots: list[dict[str, Any]],
    *,
    via_multilogin: bool = True,
    activate: bool = False,
    dry_run: bool = False,
    hand_off: bool = False,
) -> str:
    """Create a job and launch it in a background daemon thread. Returns job_id."""
    if activate:
        raise ValueError("ACL is draft-only: live activation is disabled")
    if not use_rpa_publish():
        raise RuntimeError(
            "The legacy Meta API backend is disabled in the campaign-manager dashboard"
        )
    hand_off = True
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    drafts = slots_to_campaign_drafts(
        slots,
        account_label=account_label,
        country=country,
        activate=False,
    )
    job_id = create_job(
        account_label, country, slots,
        via_multilogin=via_multilogin,
        activate=activate,
        dry_run=dry_run,
        hand_off=hand_off,
        total=len(drafts),
        campaign_names=[str(getattr(d, "name", "") or "") for d in drafts],
    )
    t = threading.Thread(
        target=_run_job,
        args=(job_id, slots),
        kwargs={
            "account_label": account_label,
            "country": country,
            "via_multilogin": via_multilogin,
            "activate": activate,
            "dry_run": dry_run,
            "hand_off": hand_off,
        },
        daemon=True,
        name=f"publish-{job_id[:8]}",
    )
    t.start()
    return job_id
