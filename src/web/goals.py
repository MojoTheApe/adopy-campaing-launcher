"""Per-campaign-manager goals: how many deposits, at what price, in which country.

Goals used to be a JavaScript literal inside report.html. Adding a country meant
editing HTML, and every machine carried the same numbers whether or not they were
that person's. Editing them from the dashboard is impossible while they live in
the markup, so they move here first — the Edit button has to have something to
edit.

Each campaign manager has their own goals. A team lead is expected to hand them
out and watch them, so the store is keyed by CM name rather than by machine, and
`ACL_TEAM_LEAD=1` unlocks editing somebody else's.

That flag is an agreement, not a mechanism: the dashboard has no sign-in — the
Buyer picker in the header chooses a view, not an identity — so anyone who can
reach the file can change any of it. Enforcing it would need a server that checks
who is asking, and there is none. Written down here so nobody mistakes the flag
for a lock.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from config.paths import get_local_config_dir

#: Цель по FTD, с которой человек начинает, пока не задал свою.
#:
#: Раньше стартом были цели владельца из старого шаблона — семь стран с его
#: числами. 30.09 тестовая установка «от имени Дана» показала их как цели Дана
#: (ACL-1452). Старт теперь — страны самого человека, по 10 FTD, остальное пусто.
DEFAULT_FTD = 10

#: Код и рынок для известных стран — справочник, а не чьи-то цели.
COUNTRY_INFO: dict[str, tuple[str, str]] = {
    "kuwait": ("KW", "GCC"),
    "united arab emirates": ("UAE", "GCC"),
    "saudi arabia": ("SA", "GCC"),
    "qatar": ("QA", "GCC"),
    "bahrain": ("BH", "GCC"),
    "oman": ("OM", "GCC"),
    "chile": ("CL", "Latam"),
    "colombia": ("CO", "Latam"),
    "ecuador": ("EC", "Latam"),
    "mexico": ("MX", "Latam"),
    "peru": ("PE", "Latam"),
    "panama": ("PA", "Latam"),
    "guatemala": ("GT", "Latam"),
    "costa rica": ("CR", "Latam"),
    "el salvador": ("SV", "Latam"),
    "argentina": ("AR", "Latam"),
}


def starting_goals(countries) -> list[dict]:
    """Цели человека, который своих ещё не задавал: его страны по DEFAULT_FTD."""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in countries or []:
        name = (str(raw or "")).strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        code, market = COUNTRY_INFO.get(name.casefold(), ("", "Other"))
        out.append({"country": name, "code": code, "market": market, "ftd": DEFAULT_FTD,
                    "cpa": None, "cr": None, "leads_per_day": None})
    out.sort(key=lambda g: (g["market"], g["country"]))
    return out

_FIELDS = ("country", "code", "market", "ftd", "cpa", "cr", "leads_per_day")


def goals_path() -> Path:
    return Path(get_local_config_dir()) / "goals.json"


def is_team_lead() -> bool:
    return (os.environ.get("ACL_TEAM_LEAD") or "").strip().lower() in {"1", "true", "yes"}


def _read() -> dict:
    path = goals_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    path = goals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _num_of(raw, key: str):
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"assets {key} must be a number, got {raw!r}")
    if value < 0:
        raise ValueError(f"assets {key} cannot be negative")
    return value


def _clean(goal: dict) -> dict:
    country = (str(goal.get("country") or "")).strip()
    if not country:
        raise ValueError("A country is required")

    def _num(key: str, cast, minimum=0):
        raw = goal.get(key)
        if raw in (None, ""):
            return None
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number, got {raw!r}")
        if value < minimum:
            raise ValueError(f"{key} cannot be negative")
        return value

    out = {
        "country": country,
        "code": (str(goal.get("code") or "")).strip().upper()[:4],
        "market": (str(goal.get("market") or "")).strip() or "Other",
        "ftd": _num("ftd", int) or 0,
        "cpa": _num("cpa", float),
        "cr": _num("cr", float),
        "leads_per_day": _num("leads_per_day", int),
    }
    # Необязательные цели по Ad Asset (Rules): {"<asset>": {"cpa": .., "cr": ..}}
    assets = goal.get("assets")
    if isinstance(assets, dict) and assets:
        clean: dict = {}
        for name, val in assets.items():
            name = str(name or "").strip()
            if not name or not isinstance(val, dict):
                continue
            entry = {k: _num_of(val.get(k), k) for k in ("cpa", "cr")}
            if any(v is not None for v in entry.values()):
                clean[name] = entry
        if clean:
            out["assets"] = clean
    return out


def for_cm(cm: str, countries=None) -> list[dict]:
    """This person's goals; without stored ones — their countries at DEFAULT_FTD.

    `countries` are the person's own countries (the caller knows them from the
    report and the account map). Seeding on read rather than on write keeps a
    machine that never edits anything from acquiring a file it does not need.
    """
    cm = (cm or "").strip()
    stored = _read().get(cm)
    if stored is None:
        return starting_goals(countries)
    return [dict(g) for g in stored if isinstance(g, dict)]


def has_stored(cm: str) -> bool:
    """Задал ли человек свои цели (тогда стартовый список не нужен)."""
    return _read().get((cm or "").strip()) is not None


def save(cm: str, goals: list[dict]) -> list[dict]:
    cm = (cm or "").strip()
    if not cm:
        raise ValueError("Whose goals? A campaign manager name is required")
    cleaned = [_clean(g) for g in goals]
    seen: set[str] = set()
    for g in cleaned:
        key = g["country"].casefold()
        if key in seen:
            raise ValueError(f"{g['country']} is listed twice")
        seen.add(key)
    data = _read()
    data[cm] = cleaned
    _write(data)
    return cleaned


def upsert(cm: str, goal: dict, countries=None) -> list[dict]:
    """Add a country or change one. Country name is the identity.

    `countries` — те же страны, что видел человек: первая правка сохраняет
    стартовые цели вместе с ней, а не одну правленую страну.
    """
    cleaned = _clean(goal)
    current = for_cm(cm, countries)
    # Правка страны из формы My Goals не знает про цели по Ad Asset: без ключа `assets`
    # (или с `None`) сохраняем прежние, иначе первая же правка CPA стирала бы цели Rules.
    # Пустой словарь `{}` - явная просьба очистить.
    if goal.get("assets") is None and "assets" not in cleaned:
        old = next((g for g in current if (g.get("country") or "").casefold() == cleaned["country"].casefold()), None)
        if old and isinstance(old.get("assets"), dict) and old["assets"]:
            cleaned["assets"] = old["assets"]
    goals = [g for g in current if (g.get("country") or "").casefold() != cleaned["country"].casefold()]
    goals.append(cleaned)
    goals.sort(key=lambda g: (g.get("market") or "", g.get("country") or ""))
    return save(cm, goals)


def delete(cm: str, country: str, countries=None) -> list[dict]:
    """Stop tracking a country. Nothing else is touched — the country keeps its
    accounts, its history and its section; it simply stops being a target."""
    country = (country or "").strip().casefold()
    goals = [g for g in for_cm(cm, countries) if (g.get("country") or "").casefold() != country]
    return save(cm, goals)


def known_managers() -> list[str]:
    return sorted(_read(), key=str.casefold)


def overview() -> list[dict]:
    """Read-only list of every manager's stored goals, for the team lead.

    Only people who have saved goals appear: a manager who never edited has just
    a derived starting list, and showing those would pass off a guess as a target.
    The caller must check `is_team_lead()` first; this function only reads.
    """
    out: list[dict] = []
    for cm in known_managers():
        goals = [dict(g) for g in (_read().get(cm) or []) if isinstance(g, dict)]
        out.append({
            "cm": cm,
            "goals": goals,
            "countries": len(goals),
            "ftd_total": sum(int(g.get("ftd") or 0) for g in goals),
        })
    return out
