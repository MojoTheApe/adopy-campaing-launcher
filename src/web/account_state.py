"""Состояние аккаунта для Home («таблица как в Google»). Чистый модуль, без IO.

Состояние выводится только из уже сохранённого: статус кабинета (`status`),
спенд за период и признак «книга знает аккаунт». Живых чтений Meta здесь нет.
Все пороги и множества — константы ниже: владелец может переиграть определение
в одном месте.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime

from src.analysis_agent.status_resolve import (
    STATUS_FRESH_HOURS,  # noqa: F401 — порог здесь же, чтобы владелец менял его в одном месте
    read_label,
    resolve_status,
)

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


def _heavy_word(code: str, text: str) -> str:
    """Слово тяжёлого статуса для строки состояния: `restricted · payment unsettled`."""
    low = (text or "").casefold()
    if code == "restricted":
        if "grace period" in low:
            return "restricted · payment grace period"
        if "unsettled" in low:
            return "restricted · payment unsettled"
    return short_reason(code, text)


def _status_line(code: str, text: str, res: dict, st: dict, label: str, tried_label: str,
                 has_binding: bool, key: str = "", by_spend: bool = False) -> str:
    """Строка под словом состояния (ACL-2419 §4): слово статуса, время чтения, причина.

    ACL-2556: строка не спорит со значком. Active по спенду/старому clear —
    «not refreshed»; под New и прочими не-Unknown никогда нет «unknown · …».
    """
    if by_spend:
        if code == "clear" and label:
            return f"status not refreshed · last read {label}"
        if tried_label:
            return f"status not refreshed · last read failed {tried_label}"
        return "active by spend · status not read yet"
    if key == "new":
        return ""
    if res["known"] and code == "clear":
        return ("active" if st.get("source") == "api" else "no warning") + (f" · {label}" if label else "")
    if res["known"]:
        line = _heavy_word(code, text) + (f" · {label}" if label else "")
        reason = str(st.get("disable_reason") or "")
        if code == "disabled" and reason:
            line += f" · {reason}"
        if res["stale"]:
            line += " · not refreshed"
        return line
    if tried_label:
        return f"unknown · read failed {tried_label}"
    if code and label:
        return f"unknown · last status {label}"
    return "unknown · never read" if has_binding else ""


def _status_tip(code: str, res: dict, st: dict, label: str, by_spend: bool = False,
                tried_label: str = "") -> str:
    """Подсказка к слову состояния (ACL-2419 §4)."""
    if by_spend:
        return ("Active by spend in the period; the account status could not be read"
                + (f" {tried_label}." if tried_label else " yet."))
    if res["known"] and code == "clear":
        if st.get("source") == "api":
            return f"Account status from Meta (account_status = 1), read {label}."
        return f"No warning on the account page, read {label}. Not the same as a verified active status."
    if res["known"] and res["stale"]:
        return "Last known status. The latest read did not refresh it."
    if not res["known"] and code:
        failure = str(st.get("tried_error") or "")
        base = ("The status was not refreshed in the last 24 hours. It is not shown as "
                '"no warning" or "active" until it is read again.')
        return f"{base} {failure}".strip()
    return ""


def stale_clear_too_old(at: str, today: date) -> bool:
    """Удачное чтение `clear` старше STATE_STALE_DAYS (или без даты) не считается фактом."""
    day = _parse_day(at)
    return day is None or (today - day).days > STATE_STALE_DAYS


def account_state(row: dict, today: date | None = None) -> dict:
    """{key,label,reason,short_reason,read_at,read_short,age_days,stale,...} по строке карты.

    ACL-2419: сначала статус кабинета, потом спенд. Отключённый — Dead и с
    деньгами; `clear` без свежего чтения (или после неудачного) — Unknown, а не
    «Active по спенду»: устаревшее «всё хорошо» фактом не считается.
    """
    today = today or date.today()
    now = datetime.combine(today, dtime(12, 0)) if today != date.today() else datetime.now()
    st = row.get("status") or {}
    code = str(st.get("status") or "")
    text = str(st.get("status_text") or "")
    at = str(st.get("at") or "")
    spend = row.get("spend") or 0
    try:
        spend = float(spend)
    except (TypeError, ValueError):
        spend = 0.0
    failed_at = str(st.get("tried_at") or "") or ("1" if st.get("stale") else "")
    res = resolve_status(st, failed_at, now)
    # Не распознанный API-код (`unknown` со словами Meta) — тоже незнание, не «нет статуса».
    has_status = bool(code)

    by_spend = False
    if code in DEAD_STATUSES:
        key = "dead"
    elif code in ISSUE_STATUSES:
        key = "issue"
    elif res["known"] and code == "clear":
        key = "active"
    elif row.get("has_data") and spend > 0:
        # ACL-2556: не прочитанный/просроченный/упавший статус не делает «Unknown»
        # аккаунт, который тратит; тяжёлые статусы выше уже выиграли.
        key = "active"
        by_spend = True
    elif code == "clear" and not stale_clear_too_old(at, today):
        # Последнее удачное чтение было active/clear не старше порога, новое упало.
        key = "active"
        by_spend = True
    elif has_status and not res["known"]:
        key = "unknown"
    elif row.get("in_workbook") is False:
        key = "new"
    else:
        key = "unknown"

    read_day = _parse_day(at)
    age_days = (today - read_day).days if read_day else None
    stale = bool(st.get("stale")) or (age_days is not None and age_days > STATE_STALE_DAYS)

    elsewhere = row.get("elsewhere") or {}
    label = read_label(at, now)
    tried_label = read_label(st.get("tried_at") or "", now) if (st.get("tried_at") or st.get("tried_error")) else ""
    if key in ("issue", "dead"):
        reason = text or short_reason(code, text)
    elif key == "unknown":
        tried = _short_date(st.get("tried_at") or "")
        err = str(st.get("tried_error") or "")
        if tried or err:
            reason = f"last read failed {tried}".strip()
        elif has_status and label:
            reason = f"last status {label}"
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
        # ACL-2419: слово статуса из Meta и время чтения для ячейки состояния.
        "status_word": res["shown"],
        "status_read": label,
        "status_line": _status_line(code, text, res, st, label, tried_label,
                                    bool(row.get("act_id") or row.get("profile_id")), key, by_spend),
        "status_tip": _status_tip(code, res, st, label, by_spend, tried_label),
        "by_spend": by_spend,
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
