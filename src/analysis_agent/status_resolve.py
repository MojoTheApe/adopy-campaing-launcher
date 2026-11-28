"""ACL-2419: чистые правила статуса кабинета — без IO, без Meta, без хранилища.

Хранилище (`account_spend`) и экран Home (`web.account_state`) зовут одни и те же
функции, поэтому слова «активен / неизвестно / отключён» не расходятся между
картой Manage и таблицей Home. Здесь нет ни таймеров, ни чтений: только решение
по уже сохранённой записи.
"""
from __future__ import annotations

from datetime import datetime

#: Старше стольких часов `clear`/`unknown` перестаёт быть фактом.
STATUS_FRESH_HOURS = 24
#: ACL-2649: статус из текста страницы устаревает быстрее статуса из Graph API: текст страницы
#: читается грубее, и неверно прочитанное слово не должно висеть сутками.
PAGE_STATUS_FRESH_HOURS = 6
#: Два чтения не дальше друг от друга считаются одним циклом (API и текст страницы).
SAME_CYCLE_MINUTES = 10
#: ACL-2649: тяжёлый статус старше срока свежести тоже не факт (кабинет могли вылечить в Ads Manager).
HEAVY = frozenset({"disabled", "restricted", "review", "blocked"})
#: Чем меньше число, тем весомее статус (порядок спеки §2.4).
RANK = {"disabled": 0, "restricted": 1, "review": 2, "blocked": 3, "clear": 4, "unknown": 5}


def parse_stamp(value: object) -> datetime | None:
    """`2026-11-05 14:02:11` или ISO с `T` → naive-время; нечитаемое → None."""
    text = str(value or "").strip().replace("T", " ")[:19]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None


def read_label(value: object, now: datetime | None = None) -> str:
    """`05.11 14:02`; год дописывается только если чтение не из этого года."""
    stamp = parse_stamp(value)
    if stamp is None:
        return ""
    now = now or datetime.now()
    if len(str(value or "").strip()) <= 10:
        text = stamp.strftime("%d.%m")
    else:
        text = stamp.strftime("%d.%m %H:%M")
    return text if stamp.year == now.year else f"{text}.{stamp.year}"


def disable_reason_label(name: str) -> str:
    """`ADS_INTEGRITY_POLICY` → `ads integrity policy`."""
    return str(name or "").replace("_", " ").strip().lower()


def should_replace(old: dict | None, new: dict) -> bool:
    """Заменять ли старую запись новой (спека §2.4).

    В пределах одного цикла весомый статус не затирается лёгким; через время
    выигрывает новое чтение (сегодняшний API-`clear` важнее вчерашнего `disabled`
    со страницы).
    """
    if not old or not old.get("status"):
        return True
    new_at, old_at = parse_stamp(new.get("at")), parse_stamp(old.get("at"))
    if new_at is None or old_at is None:
        return True
    gap = abs((new_at - old_at).total_seconds()) / 60
    # Весомый статус держится только между РАЗНЫМИ источниками (API и страница); два чтения
    # одного источника — новое выигрывает: кабинет мог выздороветь, а повторное чтение это видит.
    new_src, old_src = (new.get("source") or "page"), (old.get("source") or "page")
    if new_src == "api" and old_src == "page":
        # ACL-2649: любое более новое чтение из API заменяет статус, прочитанный из текста страницы.
        return new_at >= old_at
    if gap <= SAME_CYCLE_MINUTES and old_src != new_src:
        return RANK.get(str(new.get("status")), 5) <= RANK.get(str(old.get("status")), 5)
    return new_at >= old_at


def resolve_status(raw: dict, attempt_failed_at: str = "", now: datetime | None = None) -> dict:
    """Что показывать по записи `status|<account>` (спека §2.3, вариант A).

    `attempt_failed_at` — время неудачной попытки чтения позже записи (пусто,
    если такой не было). Ответ:
      shown     — слово для экрана: тяжёлый статус как есть, иначе `clear`/`unknown`;
      known     — статус можно показывать как факт (свежий и распознанный);
      fresh     — чтение не старше STATUS_FRESH_HOURS;
      stale     — после записи была неудачная попытка либо запись старше суток;
      age_hours — возраст записи или None, если время нечитаемо.
    """
    now = now or datetime.now()
    status = str((raw or {}).get("status") or "")
    stamp = parse_stamp((raw or {}).get("at"))
    age = (now - stamp).total_seconds() / 3600 if stamp else None
    limit = STATUS_FRESH_HOURS if str((raw or {}).get("source") or "page") == "api" else PAGE_STATUS_FRESH_HOURS
    fresh = age is not None and age <= limit
    failed_after = bool(attempt_failed_at)
    if status in HEAVY and fresh:
        # Свежий тяжёлый статус остаётся фактом; после неудачного чтения помечается «не обновлён».
        return {"shown": status, "known": True, "fresh": True, "stale": failed_after, "age_hours": age}
    if status in HEAVY:
        # ACL-2649: устаревший тяжёлый статус — не факт: «unknown», прежнее слово в `last`.
        return {"shown": "unknown", "known": False, "fresh": False, "stale": True, "age_hours": age,
                "last": status}
    if status == "clear" and fresh and not failed_after:
        return {"shown": "clear", "known": True, "fresh": True, "stale": False, "age_hours": age}
    # `clear` старше срока или после неудачного чтения, `unknown`, пусто: не факт.
    return {"shown": "unknown", "known": False, "fresh": fresh,
            "stale": bool(status) and (failed_after or not fresh), "age_hours": age}
