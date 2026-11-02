"""Кэш вердиктов «Check plan» на сервере (ACL-2266): неизменённая кампания читается в Meta один раз.

Единственный кэш вердиктов: страница только показывает то, что вернул сервер. Один вход на кампанию.

Ключ: (метка аккаунта, страна, id кабинета из карты аккаунтов, дайджест ОБОГАЩЁННОГО черновика
вместе с отпечатками файлов креативов, сегодняшняя дата, SCHEMA). Не хэш слотов формы: тексты из
библиотеки, ссылка, пиксель, Page, интересы и файлы на диске попадают в черновик уже после слотов.

Кладём только вердикт с положительным признаком проверки (`_reject_reason`): упавший или пропущенный
аудит, ошибка кампании и термины, ждущие клика человека, вердиктом не считаются.

В памяти процесса, под замком, до `MAX_ENTRIES` записей (старые вытесняются первыми); живёт
`ACL_PLAN_CACHE_TTL_MIN` минут (по умолчанию 60, 0 — кэш выключен). Перезапуск дашборда кэш
очищает. Ни Meta, ни профиля, ни куль-дауна модуль не трогает: попадание — чтение из памяти.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

TTL_ENV = "ACL_PLAN_CACHE_TTL_MIN"
DEFAULT_TTL_MIN = 60.0
MAX_TTL_MIN = 24 * 60.0
#: Потолок записей: старые (по времени записи) вытесняются первыми.
MAX_ENTRIES = 300
#: Поднимать при любом изменении содержимого вердикта или правил проверки (защита горячей перезагрузки).
SCHEMA = 1
#: Шаг, без которого строка не считается пройденной предпроверкой (creator.py, пробный прогон).
PASSED_STEP = "dry run: preflight passed, nothing was written"
_PENDING_TERM_STATES = ("pending", "unresolved", "unknown")

_lock = threading.Lock()
#: ключ -> (monotonic-момент записи, iso-время проверки, строка результата)
_store: dict[tuple, tuple[float, str, dict[str, Any]]] = {}
_counters: dict[str, Any] = {"hits": 0, "misses": 0, "puts": 0, "rejected": {}, "invalidated": {}}


def ttl_s() -> float:
    try:
        minutes = float(str(os.environ.get(TTL_ENV, "") or DEFAULT_TTL_MIN).replace(",", "."))
    except ValueError:
        minutes = DEFAULT_TTL_MIN
    return max(0.0, min(MAX_TTL_MIN, minutes)) * 60.0


def ttl_min() -> float:
    return ttl_s() / 60.0


def enabled() -> bool:
    return ttl_s() > 0


def _acct(label: str) -> str:
    return str(label or "").strip().casefold()


def act_of(account: str) -> str:
    """Id рекламного кабинета метки по карте аккаунтов ('' — метки нет или карта не читается)."""
    try:
        from src.web import publish_api

        entry = publish_api._resolve_entry(account)  # noqa: SLF001
        return str(getattr(entry, "ad_account_id", "") or "").strip().lower() if entry else ""
    except Exception:  # noqa: BLE001 - кэш не должен ронять проверку
        return ""


def _file_fingerprint(path: Any) -> list[Any]:
    """(путь, размер, mtime_ns) без чтения содержимого; нет файла — отметка об этом."""
    text = str(path or "")
    if not text:
        return [""]
    try:
        st = os.stat(text)
    except OSError:
        return [text, "missing"]
    return [text, st.st_size, st.st_mtime_ns]


def draft_digest(draft: Any) -> str:
    """Дайджест обогащённого черновика кампании и отпечатков его файлов (тексты, ссылка, Page — всё внутри)."""
    try:
        body = dataclasses.asdict(draft) if dataclasses.is_dataclass(draft) else dict(draft)
    except Exception:  # noqa: BLE001
        body = {"repr": repr(draft)}
    files = []
    for ad_set in getattr(draft, "ad_sets", None) or []:
        for ad in getattr(ad_set, "ads", None) or []:
            files.append([_file_fingerprint(getattr(ad, "creative_path", None)),
                          _file_fingerprint(getattr(ad, "vertical_path", None))])
    raw = json.dumps({"draft": body, "files": files}, sort_keys=True, ensure_ascii=False,
                     default=repr, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def digest_all(drafts: list[Any]) -> list[str]:
    return [draft_digest(d) for d in drafts]


def unique_names(drafts: list[Any]) -> bool:
    names = [str(getattr(d, "name", "") or "") for d in drafts]
    return bool(names) and all(names) and len(set(names)) == len(names)


def key_for(account: str, country: str, act: str, digest: str, today: str | None = None) -> tuple:
    return (_acct(account), str(country or "").strip().upper(), str(act or "").strip().lower(), digest,
            today or date.today().isoformat(), SCHEMA)


def _count(bucket: str, name: str) -> None:
    d = _counters[bucket]
    d[name] = d.get(name, 0) + 1


def _reject_reason(row: Any, name: str) -> str:
    """'' — строку можно положить; иначе причина (по положительным признакам, см. модуль)."""
    if not isinstance(row, dict):
        return "error"
    if row.get("success") is not True or row.get("error"):
        return "error"
    if PASSED_STEP not in (row.get("steps_completed") or []):
        return "not_passed"
    if str(row.get("campaign_name") or "") != str(name or ""):
        return "name_mismatch"
    audit = row.get("audit")
    if not isinstance(audit, list) or not audit:
        return "audit_empty"
    if any(str((a or {}).get("status")) == "error" for a in audit):
        return "audit_error"
    if any(str((t or {}).get("state")) in _PENDING_TERM_STATES for t in (row.get("targeting") or [])):
        return "pending_terms"
    return ""


def get(account: str, country: str, act: str, digest: str) -> dict[str, Any] | None:
    """Копия закэшированной строки с `cached: true` и `checked_at`, либо None."""
    limit = ttl_s()
    if limit <= 0:
        return None
    key = key_for(account, country, act, digest)
    with _lock:
        hit = _store.get(key)
        if hit is not None and time.monotonic() - hit[0] > limit:
            _store.pop(key, None)
            hit = None
        if hit is None:
            _counters["misses"] += 1
            return None
        _counters["hits"] += 1
        out = copy.deepcopy(hit[2])
        out["cached"] = True
        out["checked_at"] = hit[1]
    return out


def peek(account: str, country: str, act: str, digest: str) -> dict[str, Any] | None:
    """Как `get`, но без счётчиков и без вытеснения (для этапов 2-4 и маршрута /publish-plan/cached)."""
    limit = ttl_s()
    if limit <= 0:
        return None
    with _lock:
        hit = _store.get(key_for(account, country, act, digest))
        if hit is None or time.monotonic() - hit[0] > limit:
            return None
        out = copy.deepcopy(hit[2])
    out["cached"] = True
    out["checked_at"] = hit[1]
    return out


def put(account: str, country: str, act: str, digest: str, name: str, row: dict[str, Any]) -> str:
    """Кладёт строку, если она — вердикт. Вернёт '' (положили) или причину отказа ('off' — кэш выключен)."""
    if ttl_s() <= 0:
        return "off"
    reason = _reject_reason(row, name)
    if reason:
        with _lock:
            _count("rejected", reason)
        return reason
    stamp = str(row.get("checked_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    stored = copy.deepcopy(row)
    stored.pop("cached", None)
    stored["checked_at"] = stamp
    with _lock:
        key = key_for(account, country, act, digest)
        if key not in _store and len(_store) >= MAX_ENTRIES:
            oldest = min(_store, key=lambda k: _store[k][0])
            _store.pop(oldest, None)
        _store[key] = (time.monotonic(), stamp, stored)
        _counters["puts"] += 1
    return ""


def _log_invalidation(trigger: str, n: int) -> None:
    with _lock:
        _count("invalidated", trigger)
        snap = {"hits": _counters["hits"], "misses": _counters["misses"], "puts": _counters["puts"]}
    log.info("Plan verdict cache: %d entries dropped (%s); hits=%d misses=%d puts=%d",
             n, trigger, snap["hits"], snap["misses"], snap["puts"])


def invalidate_account(account: str, trigger: str = "account") -> int:
    """Забыть вердикты кабинета (задание закончилось, кабинет/Page мог измениться; хук этапа 2)."""
    want = _acct(account)
    with _lock:
        gone = [k for k in _store if k[0] == want]
        for k in gone:
            _store.pop(k, None)
    _log_invalidation(trigger, len(gone))
    return len(gone)


def invalidate_all(trigger: str = "all") -> int:
    with _lock:
        n = len(_store)
        _store.clear()
    _log_invalidation(trigger, n)
    return n


def counters() -> dict[str, Any]:
    with _lock:
        return copy.deepcopy(_counters)


def reset_for_tests() -> None:
    with _lock:
        _store.clear()
        _counters.update({"hits": 0, "misses": 0, "puts": 0, "rejected": {}, "invalidated": {}})
