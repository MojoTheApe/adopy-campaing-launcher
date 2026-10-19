"""Где сейчас каждая кампания задания публикации (ACL-1315).

Точка у имени кампании в ящике билдера спрашивает сервер: что с этой
кампанией прямо сейчас. Ответ складывается из двух источников:

* само задание (`publish_jobs`): статус и итог по кампании, когда он есть;
* телеметрия робота (`output/rpa_telemetry.jsonl`): последний шаг кампании
  и время, когда он записан, — по нему видно, что сборка встала.

Файл телеметрии общий и растёт без конца, поэтому читается только его хвост
(`TAIL_BYTES`), и только строки этого задания: с именем его кампании, не
старше самого задания и — когда строка его несёт — с номером задания. Разбора сверх `json.loads` строки здесь нет — опрос
идёт раз в несколько секунд, пока сборка работает.

Решение, какого цвета точка, принимает страница: сервер отдаёт факты
(итог, последний шаг, сколько секунд тихо, что роботу не по силам).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Сколько секунд без нового шага у идущей кампании — уже «встала».
#: Владелец назвал три минуты — до площадок так и есть.
STALL_AFTER_S = 180

#: С площадок и дальше (креатив, медиа, назначение, текст) законный шаг идёт
#: до ~6 минут: 25.09 — 370 с (ad_creative_reuse → ad_creative_upload),
#: 356, 329, 321 с. На этом этапе «встала» — только после семи минут.
SLOW_STALL_AFTER_S = 420

#: Шаги, после которых кампания на медленном этапе. Этап, а не сам шаг:
#: долго идёт шаг, который ещё не записан, а видна только строка перед ним
#: (ad_name → креатив, advantage_plus_audience → площадки). Одно место.
SLOW_STAGE_STEP = re.compile(
    r"^(advantage_plus_audience|placements|devices_ios|next_to_ad"
    r"|ad\d*_|library|creative|media|destination|copy)")


def stall_after_for(steps: list[str]) -> int:
    """Порог тишины для кампании по её шагам этого задания."""
    return SLOW_STALL_AFTER_S if any(SLOW_STAGE_STEP.match(_base_step(s)) for s in steps) \
        else STALL_AFTER_S

#: Хвост файла телеметрии, который читается за один опрос. Одна кампания
#: пишет сотню-другую строк по ~200 байт; полмегабайта покрывают несколько
#: кампаний параллельных заданий с запасом.
TAIL_BYTES = 512 * 1024

_ACTIVE = frozenset({"pending", "queued", "running", "cancelling"})

#: Остановки, которые робот сам не снимет: нужен человек в Meta.
_BLOCKERS = {
    "account_blocked": "Account restricted by Meta — complete the verification in Ads Manager",
    "lead_terms_not_accepted": "Lead Generation terms are not accepted on the Page — accept them in Meta",
    # ACL-1697: Ads Manager открыл не тот рекламный кабинет.
    "wrong_ad_account": "Ads Manager opened a different ad account than the draft's — check the ad account id on the Accounts page",
    # ACL-1320: новый редактор Meta «в один экран».
    "simple_create_layout": "Meta opened its new single-screen campaign editor for this account; "
                            "the robot does not support it yet",
}


def _profile_blockers() -> dict[str, str]:
    """ACL-1320: остановки из-за профиля (вход, сеть, перезапуск, открыт
    руками) — тем же текстом, что и в итоге кампании."""
    try:
        from src.fb_ads_rpa import profile_trouble
    except Exception:
        return {}
    return dict(profile_trouble.MESSAGES)


_BLOCKERS.update(_profile_blockers())


def _telemetry_path() -> Path:
    from src.fb_ads_rpa.run_telemetry import TELEMETRY_PATH

    return TELEMETRY_PATH


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def tail_rows(path: Path, max_bytes: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """Последние строки JSONL не дальше `max_bytes` от конца файла.

    Первая строка хвоста почти всегда обрезана — её выбрасываем. Битые
    строки (запись оборвалась на полуслове) пропускаются молча: телеметрия
    не должна ронять опрос.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    rows: list[dict[str, Any]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _base_step(step: Any) -> str:
    return str(step or "").split(":", 1)[0].strip()


def _result_view(result: dict[str, Any]) -> dict[str, Any]:
    from src.web.publish_jobs import _result_is_publishable

    view = {
        "success": result.get("success") is True,
        "publishable": _result_is_publishable(result),
        "error": str(result.get("error") or ""),
        "blocking_failures": [str(x) for x in (result.get("blocking_failures") or [])],
    }
    if result.get("published_as"):       # ACL-1902: только у переименованной API-кампании
        view["published_as"] = str(result["published_as"])
    return view


def _blocker_from_result(result: dict[str, Any]) -> str:
    for failure in result.get("blocking_failures") or []:
        why = _BLOCKERS.get(_base_step(failure))
        if why:
            return why
    for step in result.get("steps_completed") or []:
        text = str(step)
        if text.startswith("recorded_flow_retry_exhausted:"):
            failed = text.split(":")[1]
            return f"Gave up after all retries at step '{failed}' — needs a person"
    return ""


def job_progress(
    job: dict[str, Any],
    *,
    path: Path | None = None,
    now: datetime | None = None,
    max_bytes: int = TAIL_BYTES,
) -> dict[str, Any]:
    """Факты по каждой кампании задания для точки в ящике билдера."""
    from src.fb_ads_rpa.retry_policy import CAMPAIGN_RETRY_STEPS, MAX_CAMPAIGN_ATTEMPTS

    now = now or datetime.now(timezone.utc)
    status = str(job.get("status") or "")
    results = list(job.get("results") or [])

    names: list[str] = []
    for name in list(job.get("campaign_names") or []) + [r.get("campaign_name") for r in results]:
        name = str(name or "")
        if name and name not in names:
            names.append(name)
    by_result: dict[str, dict[str, Any]] = {}
    for r in results:
        by_result[str(r.get("campaign_name") or "")] = r

    # Строки старше задания принадлежат прошлым прогонам тех же кампаний.
    since = _parse_ts(job.get("created_at"))
    wanted = set(names)
    last: dict[str, dict[str, Any]] = {}
    fails: dict[str, dict[str, int]] = {}
    blocker: dict[str, str] = {}
    latest: datetime | None = None
    job_id = str(job.get("job_id") or "")
    steps_seen: dict[str, list[str]] = {}
    for row in tail_rows(path or _telemetry_path(), max_bytes) if wanted else []:
        name = str(row.get("campaign") or "")
        if name not in wanted:
            continue
        # Строка с номером другого задания — чужая, даже при том же имени
        # кампании (одноимённые кампании разных аккаунтов, ACL-1315).
        if row.get("job") and job_id and row.get("job") != job_id:
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or (since is not None and ts < since):
            continue
        # ACL-1614: сигнал «робот молчит» не шаг робота — его строка не должна
        # обнулять тишину, о которой он сам сообщает.
        if _base_step(row.get("step")) == "step_stuck":
            continue
        last[name] = row
        steps_seen.setdefault(name, []).append(str(row.get("step") or ""))
        if latest is None or ts > latest:
            latest = ts
        if row.get("ok") is False:
            step = _base_step(row.get("step"))
            if step in _BLOCKERS:
                blocker[name] = _BLOCKERS[step]
            elif step in CAMPAIGN_RETRY_STEPS:
                n = fails.setdefault(name, {}).get(step, 0) + 1
                fails[name][step] = n
                if n >= MAX_CAMPAIGN_ATTEMPTS and name not in blocker:
                    blocker[name] = f"Gave up after {n} attempts at step '{step}' — needs a person"

    campaigns: list[dict[str, Any]] = []
    current = ""
    for name in names:
        row = last.get(name) or {}
        result = by_result.get(name)
        why = blocker.get(name, "")
        if result is not None and not why:
            why = _blocker_from_result(result)
        if result is None and not current and status == "running":
            current = name   # кампании собираются по очереди, в порядке имён
        ts = _parse_ts(row.get("ts"))
        campaigns.append({
            "name": name,
            "result": _result_view(result) if result is not None else None,
            "step": str(row.get("step") or ""),
            "ok": row.get("ok"),
            "note": str(row.get("note") or "")[:200],
            "ts": row.get("ts") or "",
            "idle_s": round((now - ts).total_seconds(), 1) if ts else None,
            "stall_after_s": stall_after_for(steps_seen.get(name, [])),
            "blocker": why,
        })

    # Тишина задания: от последнего шага любой его кампании, а до первого
    # шага — от начала работы (ожидание профиля в очереди не считается:
    # там статус `queued`).
    idle_s = None
    if status == "running":
        started = _parse_ts(job.get("started_at"))
        mark = max([t for t in (latest, started) if t is not None], default=None)
        if mark is not None:
            idle_s = round((now - mark).total_seconds(), 1)

    return {
        "job_id": job.get("job_id") or "",
        "account_label": job.get("account_label") or "",
        "status": status,
        "active": status in _ACTIVE,
        "errors": [str(e) for e in (job.get("errors") or [])],
        # Порог — по этапу кампании, которая собирается сейчас.
        "stall_after_s": stall_after_for(steps_seen.get(current, [])),
        "idle_s": idle_s,
        "current": current,
        # ACL-1321: задание ждёт места или свой профиль — сколько впереди.
        "queue": dict(job["queue"]) if status == "queued" and isinstance(job.get("queue"), dict) else None,
        "campaigns": campaigns,
    }
