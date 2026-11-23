"""Лендинги аккаунта для ротации в одном кампейне (ACL-2852).

Выбор живёт на аккаунте, рядом с Tracking link, и хранится на машине в
`account_landings.json`. Он никогда не стирается сам: убирает только человек.
Проценты пока только хранятся (в трафик они пойдут после подключения Keitaro).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

MAX_LANDINGS = 3
_LOCK = threading.Lock()
#: Что из карточки JCloud запоминаем. Остальное (шаблон, язык) — для показа в списке.
KEEP = ("id", "title", "url", "domain", "path", "template", "language")


def even_split(n: int) -> list[int]:
    """100 / 50+50 / 33+33+34: остаток от деления достаётся последнему."""
    if n <= 0:
        return []
    base = 100 // n
    shares = [base] * n
    shares[-1] += 100 - base * n
    return shares


def _path() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / "account_landings.json"


def _load_all() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_all(data: dict) -> None:
    from config.paths import _atomic_write

    _atomic_write(_path(), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def get_landings(account: str) -> list[dict]:
    with _LOCK:
        rows = _load_all().get(account)
    return [r for r in rows if isinstance(r, dict) and r.get("id") is not None] if isinstance(rows, list) else []


def _clean(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        lid = int(raw.get("id"))
    except (TypeError, ValueError):
        return None
    out = {k: raw.get(k) for k in KEEP}
    out["id"] = lid
    return out


def _percent(value: Any) -> int | None:
    try:
        p = int(value)
    except (TypeError, ValueError):
        return None
    return p if 0 <= p <= 100 else None


def _commit(account: str, rows: list[dict]) -> list[dict]:
    with _LOCK:
        data = _load_all()
        data[account] = rows
        _write_all(data)
    return rows


def add_landing(account: str, landing: Any) -> dict:
    """Добавить лендинг; проценты пересчитываются поровну."""
    item = _clean(landing)
    if item is None:
        return {"ok": False, "message": "This landing cannot be added (no ID)."}
    rows = get_landings(account)
    if any(r["id"] == item["id"] for r in rows):
        return {"ok": False, "message": "This landing is already added.", "landings": rows}
    if len(rows) >= MAX_LANDINGS:
        return {"ok": False, "message": f"At most {MAX_LANDINGS} landings per account.", "landings": rows}
    rows = [*rows, item]
    for row, share in zip(rows, even_split(len(rows))):
        row["percent"] = share
    return {"ok": True, "landings": _commit(account, rows)}


def remove_landing(account: str, landing_id: Any) -> dict:
    rows = [r for r in get_landings(account) if str(r["id"]) != str(landing_id)]
    for row, share in zip(rows, even_split(len(rows))):
        row["percent"] = share
    return {"ok": True, "landings": _commit(account, rows)}


def set_percent(account: str, landing_id: Any, percent: Any) -> dict:
    """Ручная правка процента. Сумму не навязываем: UI показывает, если она не 100."""
    value = _percent(percent)
    rows = get_landings(account)
    if value is None:
        return {"ok": False, "message": "Percent must be a whole number from 0 to 100.", "landings": rows}
    hit = next((r for r in rows if str(r["id"]) == str(landing_id)), None)
    if hit is None:
        return {"ok": False, "message": "This landing is not on the account.", "landings": rows}
    hit["percent"] = value
    return {"ok": True, "landings": _commit(account, rows)}
