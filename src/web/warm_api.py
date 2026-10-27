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
    return {**info, "accounts": got}


def _job_view(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def start_open(labels: Any) -> tuple[dict, int]:
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
    threading.Thread(target=_run_open, args=(job_id, labels), daemon=True, name="acl-warm-open").start()
    return _job_view(job), 202


def _run_open(job_id: str, labels: list[str]) -> None:
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
