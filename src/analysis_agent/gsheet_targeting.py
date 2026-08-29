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
                          loaded but NOT applied yet — the RPA has no
                          narrow-audience step today

Env overrides:
    GSHEET_TARGETING_SPREADSHEET_ID — spreadsheet id
    GSHEET_TARGETING_TAB            — tab name (default "Detailed targeting")
"""
from __future__ import annotations

import os
import re
import sys
import time
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
    narrow: str = ""  # "define further" column, not applied by the RPA yet


@dataclass
class _Cache:
    loaded_at: float = 0.0
    profiles: dict[str, TargetingProfile] = field(default_factory=dict)


_cache = _Cache()


def _log(msg: str) -> None:
    print(f"[gsheet_targeting] {msg}", file=sys.stderr)


def _clean_names(raw: str) -> str:
    raw = _BUCKET_PREFIX_RE.sub("", (raw or "").strip())
    names = [n.strip() for n in raw.split(",")]
    return ", ".join(n for n in names if n)


def load_targeting_profiles(*, force: bool = False) -> dict[str, TargetingProfile]:
    """Load all profiles, cached for a few minutes. {} on any failure —
    callers treat that as "fall back to the Excel source"."""
    now = time.time()
    if not force and _cache.profiles and now - _cache.loaded_at < _CACHE_TTL_S:
        return _cache.profiles

    try:
        from src.analysis_agent.gsheet_accounts import _build_sheets_service

        svc = _build_sheets_service("config/google_credentials.json")
        sheet_id = os.environ.get("GSHEET_TARGETING_SPREADSHEET_ID") or _DEFAULT_SPREADSHEET_ID
        tab = os.environ.get("GSHEET_TARGETING_TAB") or _DEFAULT_TAB

        # Tolerate tab renames that only change case/whitespace.
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        actual = next(
            (t for t in titles if t.strip().lower() == tab.strip().lower()), None
        )
        if actual is None:
            _log(f"tab {tab!r} not found (have: {titles}) — falling back")
            return {}

        rows = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"'{actual}'!A2:C500")
            .execute()
            .get("values", [])
        )
    except Exception as exc:
        _log(f"load failed ({exc!r}) — falling back")
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

    _cache.profiles = profiles
    _cache.loaded_at = now
    _log(f"loaded {len(profiles)} targeting profiles from tab {actual!r}")
    return profiles


def lookup_targeting_interests(targeting_name: str) -> str:
    """Comma-separated interest names for a profile; "" when unknown.

    Matching mirrors the Excel path's lookup_interest(): exact first, then
    substring either way — dashboards sometimes carry decorated labels.
    """
    t = (targeting_name or "").strip()
    if not t:
        return ""
    profiles = load_targeting_profiles()
    if not profiles:
        return ""
    if t in profiles:
        return profiles[t].interests
    for name, prof in profiles.items():
        if t in name or name in t:
            return prof.interests
    return ""


class TargetingWriteError(RuntimeError):
    """Adding a profile failed for a reason the operator can act on."""


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
    clash = next((k for k in existing if k.strip().lower() == clean_name.lower()), None)
    if clash:
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
            range=f"'{actual}'!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[clean_name, clean_interests, clean_narrow]]},
        ).execute()
    except TargetingWriteError:
        raise
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator as text
        raise TargetingWriteError(f"Google Sheets отказал в записи: {exc}") from exc

    # Drop the cache so the new profile is visible without waiting out the TTL.
    load_targeting_profiles(force=True)
    _log(f"added targeting profile {clean_name!r}")
    return TargetingProfile(name=clean_name, interests=clean_interests, narrow=clean_narrow)
