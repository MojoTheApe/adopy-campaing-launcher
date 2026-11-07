"""Состояние аккаунта для Home («таблица как в Google»). Чистый модуль, без IO.

Состояние выводится только из уже сохранённого: статус кабинета (`status`),
спенд за период и признак «книга знает аккаунт». Живых чтений Meta здесь нет.
Все пороги и множества — константы ниже: владелец может переиграть определение
в одном месте.
"""
from __future__ import annotations

from datetime import date, datetime

#: Порядок показа состояний в блоке страны и в счётчиках.
STATE_ORDER = ("active", "issue", "dead", "new", "unknown")
STATE_LABELS = {
    "active": "Active", "issue": "Issue", "dead": "Dead", "new": "New", "unknown": "Unknown",
}
#: Статусы кабинета, которые считаются проблемой (остальное, кроме disabled/clear, — тоже).
ISSUE_STATUSES = frozenset({"restricted", "blocked", "review"})
DEAD_STATUSES = frozenset({"disabled"})
#: Чтение старше стольких суток приглушается и получает «· Nd».
STATE_STALE_DAYS = 3
#: Строк в блоке страны до «Show N more» (Expanded).
AOV_ROWS_COLLAPSED = 15


def _parse_day(value: str) -> date | None:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _short_date(value: str) -> str:
    d = _parse_day(value)
    return d.strftime("%d.%m") if d else ""


def short_reason(status: str, status_text: str) -> str:
    """Короткая причина для плитки: подстрока текста, затем код статуса."""
    low = (status_text or "").casefold()
    if "not in bm" in low:
        return "not in BM"
    if "video selfie" in low:
        return "video selfie"
    return {
        "restricted": "restricted", "review": "in review", "blocked": "blocked",
        "disabled": "disabled",
    }.get(status or "", "problem")


def account_state(row: dict, today: date | None = None) -> dict:
    """{key,label,reason,short_reason,read_at,read_short,age_days,stale} по строке карты."""
    today = today or date.today()
    st = row.get("status") or {}
    code = str(st.get("status") or "")
    text = str(st.get("status_text") or "")
    at = str(st.get("at") or "")
    spend = row.get("spend") or 0
    try:
        spend = float(spend)
    except (TypeError, ValueError):
        spend = 0.0

    if code in DEAD_STATUSES:
        key = "dead"
    elif code in ISSUE_STATUSES:
        key = "issue"
    elif row.get("has_data") and spend > 0:
        key = "active"
    elif row.get("in_workbook") is False:
        key = "new"
    else:
        key = "unknown"

    read_day = _parse_day(at)
    age_days = (today - read_day).days if read_day else None
    stale = bool(st.get("stale")) or (age_days is not None and age_days > STATE_STALE_DAYS)

    elsewhere = row.get("elsewhere") or {}
    if key in ("issue", "dead"):
        reason = text or short_reason(code, text)
    elif key == "unknown":
        tried = _short_date(st.get("tried_at") or "")
        err = str(st.get("tried_error") or "")
        if tried or err:
            reason = f"last read failed {tried}".strip()
        elif elsewhere:
            reason = "spend filed under " + ", ".join(str(k) for k in elsewhere)
        else:
            reason = "no saved reading"
    elif key == "new":
        reason = "not in the workbook yet"
    else:
        reason = ""
    if key in ("unknown", "new") and elsewhere and not reason.startswith("spend filed"):
        reason = f"{reason} · spend filed under " + ", ".join(str(k) for k in elsewhere)

    return {
        "key": key,
        "label": STATE_LABELS[key],
        "reason": reason,
        "short_reason": short_reason(code, text) if key in ("issue", "dead") else "",
        "read_at": at,
        "read_short": _short_date(at),
        "tried_short": _short_date(st.get("tried_at") or ""),
        "age_days": age_days,
        "stale": stale,
    }


def state_counts(states) -> dict:
    """Счётчики по ключам состояний, все пять ключей всегда присутствуют."""
    out = {k: 0 for k in STATE_ORDER}
    for s in states:
        out[s["key"]] += 1
    out["all"] = sum(out[k] for k in STATE_ORDER)
    return out


def sort_rows(rows: list[dict]) -> list[dict]:
    """Active → Issue → Dead → New → Unknown, внутри — по спенду убыв."""
    def key(r):
        try:
            sp = float(r.get("spend") or 0)
        except (TypeError, ValueError):
            sp = 0.0
        return (STATE_ORDER.index(r["state"]["key"]), -sp, str(r.get("account") or "").casefold())
    return sorted(rows, key=key)
