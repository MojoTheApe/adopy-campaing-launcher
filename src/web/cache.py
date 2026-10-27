"""File-version-keyed in-process cache for the morning report.

Single-user, single-uvicorn-worker tool. The cache holds:

  - enriched Excel DataFrame (keyed by path + mtime + size) — expensive to rebuild
  - full report dict (GSheet + Excel) — Refresh reuses the DF when Excel unchanged

  - GET /  : if report cache matches the file version, return instantly.
  - POST /refresh : force-rebuild report (reuse Excel DF if the version is unchanged).
  - GET /details : uses cached enriched_df; builds if empty.

Known race (per design 7C, accepted): if /refresh swaps the cache while a
GET / render is mid-flight, the in-flight render may see partially-stale
state. Single-user, sub-second renders, low blast radius.
"""
from __future__ import annotations

import os
import threading
from itertools import count
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from src.analysis_agent import bi_stats
from src.analysis_agent.morning_service import (
    _resolve_excel_path,
    _workbook_key,
    load_morning_report,
)


# Reports are cached PER campaign manager: the enriched DataFrame is the
# expensive part and is the same for everyone, while the report assembled on
# top of it is per-CM. Keying only by the Excel file meant switching buyer
# would have thrown away the parsed workbook and re-read ~71k rows each time.
_REVISIONS = count(1)
_CACHE: Dict[str, Any] = {
    "key": None,            # file-version/source key for full report — None means empty
    "reports": {},          # cm -> report dict
    "df": None,             # pd.DataFrame (same as excel_df when warm)
    "warnings": [],         # list[str]
    "built_at": None,       # ISO datetime string
    "excel_key": None,      # file-version/source key for reused DF
    "excel_df": None,       # pd.DataFrame — survives report-only refresh
    # (key, {cm: report}, df, warnings) — то, что видят читатели (ACL-1399)
    "entry": None,
}


def _excel_key(excel_path: str) -> Optional[Tuple[str, float, int]]:
    if not os.path.isfile(excel_path):
        return None
    return _workbook_key(excel_path)


def _current_key(excel_path: str):
    # ACL-103: при BI ключ — последняя известная ревизия BI (без сети) плюс
    # книга-фолбэк; ревизию обновляет get_cached_report из рабочего потока.
    if bi_stats.bi_enabled():
        return bi_stats.source_key(_excel_key(excel_path))
    return _excel_key(excel_path)


# ACL-1399: сборку ведёт один поток за раз. Пока отчёт собирался прямо в
# обработчике запроса, каждый следующий запрос, которому нужен отчёт, начинал
# СВОЮ холодную сборку — и страница, и фрагменты карты, и /api/buyers. Теперь
# второй ждёт первого и берёт готовое.
_BUILD_LOCK = threading.Lock()


def _cm_key(my_cm: Optional[str]) -> str:
    if my_cm is None:
        from src.web.buyer import get_selected_cm

        my_cm = get_selected_cm()
    return (my_cm or "").strip()


def _warm_hit(expected_key, cm_key: str):
    # Один снимок, прочитанный одной операцией (ревью PR #408, R6): ключ,
    # отчёты, книга и предупреждения всегда из одной сборки — чтение по
    # полям могло взять новый df к старому отчёту или словарь, который
    # сборка другой книги успела заменить между проверкой и индексом.
    entry = _CACHE["entry"]
    if entry is None or expected_key is None or entry[0] != expected_key:
        return None
    report = entry[1].get(cm_key)
    if report is None:
        return None
    return report, entry[2], entry[3]


def peek_cached_report(my_cm: Optional[str] = None):
    """Тёплый (report, df, warnings) или None. Никогда не собирает и не ждёт.

    Дёшево (stat файла) и без замка: вызывается из цикла событий, чтобы
    решить, отдавать страницу или экран «цифры загружаются».
    """
    try:
        expected_key = _current_key(_resolve_excel_path(None))
    except Exception:  # noqa: BLE001 — нет пути к книге: «не готово»
        return None
    return _warm_hit(expected_key, _cm_key(my_cm))


def report_ready(my_cm: Optional[str] = None) -> bool:
    """Отчёт этого менеджера уже в памяти и книга с тех пор не менялась."""
    return peek_cached_report(my_cm) is not None


def get_cached_report(
    *, force_rebuild: bool = False, my_cm: Optional[str] = None,
    progress: Callable[[str], None] | None = None,
) -> Tuple[Dict[str, Any], pd.DataFrame, List[str]]:
    """Return the latest (report, df, warnings).

    Rebuilds when the Excel file's version differs from the cached entry, or
    when force_rebuild is set. On force_rebuild, reuses the enriched DataFrame
    if the Excel version is unchanged (only GSheet + report assembly re-run).

    Blocking: a cold build takes a minute. Never call it on the event loop —
    handlers go through a worker thread (see src/web/app.py, ACL-1399).
    """
    excel_path = _resolve_excel_path(None)
    if bi_stats.bi_enabled():
        # Сеть — только когда ревизии ещё нет или нажат Refresh. Иначе её
        # обновляет фоновый поток: запрос страницы не ждёт BI (ACL-1399).
        if force_rebuild or bi_stats.known_revision() is None:
            bi_stats.refresh_revision(force=True)
        bi_stats.ensure_revision_watcher()
    expected_key = _current_key(excel_path)
    cm_key = _cm_key(my_cm)

    if not force_rebuild:
        hit = _warm_hit(expected_key, cm_key)
        if hit is not None:
            return hit

    requested = _BUILDS_STARTED[0]
    with _BUILD_LOCK:
        # Пока ждали замок, отчёт мог собрать другой поток. Для обычного
        # запроса это готовый ответ; для force_rebuild — только если та сборка
        # НАЧАЛАСЬ после нашего запроса (сборки идут строго по одной), иначе
        # её данные старше нажатия Refresh.
        expected_key = _current_key(excel_path)
        if not force_rebuild or _BUILDS_STARTED[0] > requested:
            hit = _warm_hit(expected_key, cm_key)
            if hit is not None:
                return hit
        _BUILDS_STARTED[0] += 1
        return _build_locked(expected_key, cm_key, progress=progress)


# Сколько сборок начато — см. force_rebuild выше.
_BUILDS_STARTED = [0]


def _build_locked(expected_key, cm_key: str, *, progress=None):
    reused_df = None
    if (
        expected_key is not None
        and _CACHE.get("excel_key") == expected_key
        and _CACHE.get("excel_df") is not None
    ):
        reused_df = _CACHE["excel_df"]

    options = {"progress": progress} if progress is not None else {}
    report, df, warnings = load_morning_report(
        enriched_df=reused_df, my_cm=cm_key or None, **options
    )
    # Refresh can change values or account rows without changing the DF length.
    # Derived period reports must belong to this exact rebuild.
    report["_cache_revision"] = next(_REVISIONS)

    # A new workbook invalidates every buyer's report, not just this one.
    # ACL-1399: читатели берут тёплый отчёт без замка (из цикла и из пула) —
    # только через снимок `entry` ниже, который ставится последним.
    new_book = _CACHE["key"] != expected_key
    reports = {} if new_book else dict(_CACHE["reports"])
    reports[cm_key] = report
    _CACHE["df"] = df
    _CACHE["warnings"] = warnings
    _CACHE["excel_key"] = expected_key
    # ACL-103: книга, прочитанная вместо упавшего BI, не переиспользуется —
    # следующий Refresh снова пробует BI, а не держит фолбэк до новой загрузки.
    bi_fallback = bi_stats.bi_enabled() and df.attrs.get("stats_source") != "bi"
    _CACHE["excel_df"] = None if bi_fallback else df
    _CACHE["reports"] = reports
    from datetime import datetime
    _CACHE["built_at"] = datetime.now().isoformat(timespec="seconds")
    _CACHE["key"] = expected_key
    # Читатели без замка видят только этот кортеж — он заменяется целиком.
    _CACHE["entry"] = (expected_key, reports, df, warnings)
    return report, df, warnings


# ── Фоновая сборка (ACL-1399) ──────────────────────────────────────────
#
# После перезапуска первая страница ждала холодную сборку (~минуту), и всё
# это время сервер не отвечал ни на что — даже на /health. Теперь страница
# сразу отдаёт «цифры загружаются», а сборка идёт здесь, в своём потоке.
_WARM_LOCK = threading.Lock()
_WARM: Dict[str, Any] = {"thread": None, "error": None, "token": None, "stage": None}


def background_progress_callback() -> Callable[[str], None]:
    """Bind progress to the owning background job, including its pool workers.

    A callback from a foreground build or an earlier finished job cannot update
    the active loading screen. No file reads or report work happen under this lock.
    """
    owner = threading.current_thread()

    def update(stage: str) -> None:
        if stage not in ("reading", "processing", "preparing"):
            return
        with _WARM_LOCK:
            if _WARM["thread"] is owner:
                _WARM["stage"] = stage

    return update


def start_background_build(job: Callable[[], Any], *, token: Any = None) -> bool:
    """Запустить `job` в фоновом потоке, если такой ещё не идёт.

    `token` — что именно собирается (период страницы): статус отвечает «идёт»
    только для той же страницы. Ошибка сборки запоминается, а не теряется:
    страница покажет её обычным путём.
    """
    with _WARM_LOCK:
        running = _WARM["thread"]
        if running is not None and running.is_alive():
            return False
        _WARM["error"] = None
        _WARM["token"] = token
        _WARM["stage"] = None

        def run() -> None:
            try:
                job()
            except Exception as exc:  # noqa: BLE001 — покажем на странице
                with _WARM_LOCK:
                    _WARM["error"] = str(exc) or type(exc).__name__
            finally:
                with _WARM_LOCK:
                    if _WARM["thread"] is threading.current_thread():
                        _WARM["thread"] = None

        thread = threading.Thread(target=run, name="acl-report-build", daemon=True)
        _WARM["thread"] = thread
        thread.start()
        return True


def background_build_state() -> Dict[str, Any]:
    """{"building": bool, "error": str|None, "token": ...} — без ожидания."""
    with _WARM_LOCK:
        thread = _WARM["thread"]
        return {
            "building": thread is not None and thread.is_alive(),
            "error": _WARM["error"],
            "token": _WARM["token"],
            "stage": _WARM["stage"],
        }


def forget_background_error() -> Optional[str]:
    with _WARM_LOCK:
        error, _WARM["error"] = _WARM["error"], None
        return error


def entry_source_key():
    """Source key of the report now in memory (what the last build was made from)."""
    entry = _CACHE["entry"]
    return entry[0] if entry is not None else None


def verified_source_key(timeout: float = 4.0):
    """Current source key, asking BI for its revision if it is not known yet.

    ACL-2207: after a restart the revision is unknown, and a stored page can
    only be called current once it has been compared with it. The question goes
    to BI in a helper thread so an unreachable server costs `timeout` seconds,
    not the connect timeout; if it stays unknown the key is None, which the
    caller must treat as "not verified". Never call on the event loop.
    """
    try:
        if bi_stats.bi_enabled() and bi_stats.known_revision() is None:
            worker = threading.Thread(
                target=bi_stats.refresh_revision, name="acl-bi-revision-once", daemon=True
            )
            worker.start()
            worker.join(timeout)
        key = _current_key(_resolve_excel_path(None))
    except Exception:  # noqa: BLE001 — no workbook path / BI settings: not verified
        return None
    # BI answered "unavailable": the revision is a placeholder, not a version.
    if bi_stats.bi_enabled() and bi_stats.known_revision() in (None, "unavailable"):
        return None
    return key


def cache_built_at() -> Optional[str]:
    return _CACHE["built_at"]


def cache_clear(*, keep_excel_df: bool = False) -> None:
    """Evict report cache. Optionally keep enriched Excel DF for a fast rebuild."""
    excel_key = _CACHE.get("excel_key") if keep_excel_df else None
    excel_df = _CACHE.get("excel_df") if keep_excel_df else None
    _CACHE["entry"] = None
    _CACHE["key"] = None
    _CACHE["reports"] = {}
    _CACHE["df"] = None
    _CACHE["warnings"] = []
    _CACHE["built_at"] = None
    _CACHE["excel_key"] = excel_key
    _CACHE["excel_df"] = excel_df
