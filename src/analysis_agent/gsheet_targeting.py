"""Detailed-targeting profiles from Google Sheets.

Source of truth requested by the user (2026-07-30): spreadsheet
1WlSGohkev4h_n0gw2vl0jlJEDFpTGl306fF7bIiRt9k, tab "Detailed targeting".
Replaces the "Interests" sheet of Upload FB.xlsx as the primary source;
the Excel path stays available as the caller's fallback.

Tab layout (verified live 2026-07-30):
    A: Targeting name   — profile key ("Day", "FX", "Luxury", ...)
    B: targeting 1      — comma-separated real Meta interest/behavior names,
                          already in the exact form the RPA types into the
                          Detailed targeting search box (unlike the Excel
                          source, which stores a JSON blob needing parsing)
    C: define further   — optional narrowing group (only Mix/Mix2 use it);
                          passed to the RPA as an additional AND group

Env overrides:
    GSHEET_TARGETING_SPREADSHEET_ID — spreadsheet id
    GSHEET_TARGETING_TAB            — tab name (default "Detailed targeting")
"""
from __future__ import annotations

import os
import re
import sys
import time
import threading
from functools import wraps
from dataclasses import dataclass, field

_DEFAULT_SPREADSHEET_ID = "1WlSGohkev4h_n0gw2vl0jlJEDFpTGl306fF7bIiRt9k"
_DEFAULT_TAB = "Detailed targeting"
_CACHE_TTL_S = 300.0

# Some cells carry a leading bucket label ("Interests: Adventure travel, ...").
# It is presentation, not an interest name — strip it.
_BUCKET_PREFIX_RE = re.compile(r"^\s*(interests?|behaviou?rs?)\s*:\s*", re.I)


@dataclass
class TargetingProfile:
    name: str
    interests: str  # comma-separated names, cleaned
    narrow: str = ""  # "define further" AND group


@dataclass
class _Cache:
    loaded_at: float = 0.0
    profiles: dict[str, TargetingProfile] = field(default_factory=dict)


_cache = _Cache()
_lock = threading.RLock()


def _serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return call

#: Почему последняя загрузка не удалась — пусто, если удалась.
#:
#: Раньше отказ и пустая вкладка выглядели одинаково: обе давали `{}`. На
#: экране это превращалось в «empty or the sheet is unavailable» — одну строку
#: про две разные беды, требующие разных действий. Кто читал её 8 сентября,
#: когда лист начал отвечать `403`, не мог узнать, что случилось.
last_error: str = ""


def _log(msg: str) -> None:
    print(f"[gsheet_targeting] {msg}", file=sys.stderr)


def _clean_names(raw: str) -> str:
    raw = _BUCKET_PREFIX_RE.sub("", (raw or "").strip())
    names = [n.strip() for n in raw.split(",")]
    return ", ".join(n for n in names if n)


def _sheets_services():
    """Ключи к таблице по порядку: сервисный аккаунт, затем токен сотрудника.

    8 сентября таргеты «исчезли» с дешборда. На деле лист отвечал сервисному
    аккаунту `403 The caller does not have permission` — политика Adopy, — и
    загрузчик молча возвращал пустой словарь. При этом в том же коде всё время
    жил рабочий путь: токен сотрудника, которым дешборд писал в эту же таблицу.
    Одна таблица, два ключа, и читали мы тем, который отобрали.

    Ключей именно два и пробуются оба **на самом запросе**, а не при создании
    клиента: сервисный клиент собирается без единой жалобы, и `403` прилетает
    только на первом обращении. Проверка «удалось ли создать» здесь ничего не
    значит и однажды уже ввела меня в заблуждение.
    """
    try:
        from src.analysis_agent.gsheet_accounts import _build_sheets_service

        yield "сервисный аккаунт", _build_sheets_service("config/google_credentials.json")
    except Exception as exc:
        _log(f"service account unavailable: {exc!r}")
    try:
        from src.analysis_agent.gsheet_write import get_write_service

        yield "токен сотрудника", get_write_service()
    except Exception as exc:
        _log(f"employee token unavailable: {exc!r}")


@_serialized
def load_targeting_profiles(*, force: bool = False) -> dict[str, TargetingProfile]:
    """Load all profiles, cached for a few minutes. {} on any failure —
    callers treat that as "fall back to the Excel source".

    Почему не удалось — в `last_error`: пустая вкладка и отказ в доступе дают
    одинаковый `{}`, а действий требуют разных.
    """
    global last_error
    now = time.time()
    if not force and _cache.loaded_at and now - _cache.loaded_at < _CACHE_TTL_S:
        return _cache.profiles

    _cache.loaded_at, _cache.profiles = now, {}
    sheet_id = os.environ.get("GSHEET_TARGETING_SPREADSHEET_ID") or _DEFAULT_SPREADSHEET_ID
    tab = os.environ.get("GSHEET_TARGETING_TAB") or _DEFAULT_TAB

    services = _sheets_services()

    rows = None
    actual = None
    troubles = []
    for label, svc in services:
        try:
            # Tolerate tab renames that only change case/whitespace.
            meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
            titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
            actual = next(
                (t for t in titles if t.strip().lower() == tab.strip().lower()), None
            )
            if actual is None:
                last_error = f"вкладки «{tab}» нет в таблице (есть: {', '.join(titles)})"
                _log(f"tab {tab!r} not found (have: {titles}) — falling back")
                return {}
            rows = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=f"'{actual.replace(chr(39), chr(39) * 2)}'!A2:C")
                .execute()
                .get("values", [])
            )
            break
        except Exception as exc:
            troubles.append(f"{label}: {exc}")
            _log(f"{label} refused ({exc!r})")

    if rows is None:
        last_error = "; ".join(troubles) or "нечем открыть таблицу: ни сервисного аккаунта, ни токена сотрудника"
        return {}

    profiles: dict[str, TargetingProfile] = {}
    for row in rows:
        name = (row[0] if row else "").strip()
        interests = _clean_names(row[1] if len(row) > 1 else "")
        if not name or not interests:
            continue
        if name in profiles:
            # Duplicate profile names exist in the tab (e.g. "SM" twice) —
            # first row wins, deterministically.
            continue
        profiles[name] = TargetingProfile(
            name=name,
            interests=interests,
            narrow=_clean_names(row[2] if len(row) > 2 else ""),
        )

    last_error = ""
    _cache.profiles = profiles
    _cache.loaded_at = now
    _log(f"loaded {len(profiles)} targeting profiles from tab {actual!r}")
    return profiles


def lookup_targeting_interests(targeting_name: str) -> str:
    """Exact profile lookup through the same local library used by the builder."""
    from src.analysis_agent.targeting_registry import resolve_interests
    return resolve_interests(targeting_name)


class TargetingWriteError(RuntimeError):
    """Adding a profile failed for a reason the operator can act on."""


@_serialized
def add_targeting_profile(name: str, interests: str, narrow: str = "") -> TargetingProfile:
    """Append one profile to the shared tab and return it.

    The tab is already shared by the whole team, so a profile added here is
    immediately visible to every buyer — that is the point. Appending (rather
    than rewriting a range) is what makes concurrent edits safe: two buyers
    adding at the same moment get two rows, never a lost one.

    Refuses duplicates by name: the loader silently keeps the first row when a
    name repeats, so a duplicate would look accepted and then be ignored.
    """
    from src.analysis_agent.gsheet_write import get_write_service, resolve_tab

    clean_name = (name or "").strip()
    clean_interests = _clean_names(interests or "")
    clean_narrow = _clean_names(narrow or "")

    if not clean_name:
        raise TargetingWriteError("Название таргета не может быть пустым.")
    if not clean_interests:
        raise TargetingWriteError("Укажите хотя бы один интерес.")

    existing = load_targeting_profiles(force=True)
    if last_error:
        raise TargetingWriteError("Не удалось проверить общую библиотеку перед записью: " + last_error)
    from src.analysis_agent.targeting_registry import fingerprint, normalise_one
    clash = next((k for k in existing if normalise_one(k) == normalise_one(clean_name)), None)
    if clash:
        previous = existing[clash]
        if fingerprint(previous.interests, previous.narrow) == fingerprint(clean_interests, clean_narrow):
            return previous  # successful append with a lost response can be retried
        raise TargetingWriteError(
            f"Таргет «{clash}» уже есть — выберите другое название или "
            f"отредактируйте существующий в таблице."
        )

    sheet_id = os.environ.get("GSHEET_TARGETING_SPREADSHEET_ID") or _DEFAULT_SPREADSHEET_ID
    tab = os.environ.get("GSHEET_TARGETING_TAB") or _DEFAULT_TAB

    try:
        svc = get_write_service()
        actual = resolve_tab(svc, sheet_id, tab)
        if actual is None:
            raise TargetingWriteError(f"Вкладка «{tab}» не найдена в таблице таргетов.")
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{actual.replace(chr(39), chr(39) * 2)}'!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[clean_name, clean_interests, clean_narrow]]},
        ).execute()
    except TargetingWriteError:
        raise
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator as text
        raise TargetingWriteError(f"Google Sheets отказал в записи: {exc}") from exc

    # Drop the cache so the new profile is visible without waiting out the TTL.
    _cache.loaded_at = 0
    _log(f"added targeting profile {clean_name!r}")
    return TargetingProfile(name=clean_name, interests=clean_interests, narrow=clean_narrow)
