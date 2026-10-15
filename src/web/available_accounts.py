"""Which unplaced accounts the Available accounts list shows (ACL-1319).

Раньше список был «всё известное минус размещённое», а «известное» включало
каждую строку отчёта байера — в том числе строки с нулевым спендом. У
владельца так в список попадали 36 аккаунтов, которые месяц ничего не тратили.
Решение владельца 2026-09-26: нулевые строки аккаунт «известным» не делают.

Правило показа — одно на всю машину и не зависит от периода на экране:

* **active** — у аккаунта есть спенд > 0 за последние 30 дней книги
  статистики. Окно меряется от ПОСЛЕДНЕЙ даты книги, а не от сегодня:
  отставшая на три дня книга не должна гасить работающие аккаунты.
* **new** — аккаунт подключён на этой машине (есть строка в карте
  аккаунтов) и книга не видела у него спенда ни разу, ни в какой день.
  Это «только что подключили, ещё не запускали» — его надо видеть, чтобы
  разместить в стране. Отметки времени подключения в карте нет, поэтому
  «недавно» определено так, без дат: подключённый аккаунт, у которого спенд
  когда-то был, но не в последние 30 дней, — уже не новый, а неактивный.
* всё остальное — **inactive**: не показывается, пока не включён
  «Show inactive».
* убранные руками (`account_links.hidden_accounts`) не показываются ни там,
  ни там — у них свой список «Hidden accounts» с кнопкой Restore.

Нет книги или в ней нет нужных колонок — фильтра нет, список как раньше:
спрятать всё из-за непрочитанного файла хуже, чем показать лишнее.

Имя аккаунта с книгой сводится так же, как везде на странице: первый токен
подписи (`normalize_account_alias`), а для имени из Multilogin
(`KLD_BFL_X84`) — по хвосту, только при единственном совпадении.
"""
from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field

WINDOW_DAYS = 30


@dataclass
class Activity:
    """Спенд по аккаунтам книги: за окно и за всё время."""

    window_from: str
    window_to: str
    #: ключ аккаунта книги → спенд за окно (только > 0)
    recent: dict[str, float] = field(default_factory=dict)
    #: ключи аккаунтов, у которых спенд > 0 был хоть когда-то
    ever: set[str] = field(default_factory=set)
    #: все ключи книги — для сведения по хвосту
    names: list[str] = field(default_factory=list)
    #: те же ключи множеством — строится один раз, а не на каждый аккаунт
    #: списка (финальное ревью PR #314)
    name_set: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name_set and self.names:
            self.name_set = frozenset(self.names)


def alias_key(label: str) -> str:
    from src.analysis_agent.analyze_fb_stats import normalize_account_alias

    return normalize_account_alias(label).casefold()


#: Последний ответ `workbook_activity`: книга (слабая ссылка на тот же
#: DataFrame), её длина, окно — и ответ.
_ACTIVITY_CACHE: dict = {"ref": None, "rows": None, "days": None, "value": None}
_ACTIVITY_LOCK = threading.Lock()


def workbook_activity(df, days: int = WINDOW_DAYS) -> Activity | None:
    """Спенд за последние `days` дней книги, считая её последний день.

    None — книги нет или в ней нет колонок Account Alias / $ Spend / Date.
    Колонка CM не сужает: аккаунт, который тратит, активен, чьим бы ни был
    ряд, — а список и так собран только из аккаунтов этой машины и отчёта
    этого байера.

    Финальное ревью PR #314: считалось заново на каждой отрисовке Home
    (разбор дат и группировка всей книги). Теперь — один раз на ревизию
    книги: кэш отчёта (`web.cache`) держит один и тот же DataFrame, пока не
    сменится файл или не пересоберётся отчёт, и новый DataFrame — новая
    ревизия. Ключ — сам объект (слабая ссылка: кэш не держит старую книгу
    в памяти), его длина и окно.
    """
    if df is None:
        return None
    rows = len(df) if hasattr(df, "__len__") else None
    with _ACTIVITY_LOCK:
        ref = _ACTIVITY_CACHE["ref"]
        if (ref is not None and ref() is df and _ACTIVITY_CACHE["rows"] == rows
                and _ACTIVITY_CACHE["days"] == days):
            return _ACTIVITY_CACHE["value"]
    value = _workbook_activity(df, days)
    try:
        new_ref = weakref.ref(df)
    except TypeError:
        return value
    with _ACTIVITY_LOCK:
        _ACTIVITY_CACHE.update(ref=new_ref, rows=rows, days=days, value=value)
    return value


def _workbook_activity(df, days: int) -> Activity | None:
    try:
        import pandas as pd

        if df is None or any(c not in getattr(df, "columns", []) for c in ("Account Alias", "$ Spend", "Date")):
            return None
        days_col = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
        last = days_col.max()
        if pd.isna(last):
            return None
        start = last - pd.Timedelta(days=days - 1)
        spend = pd.to_numeric(df["$ Spend"], errors="coerce").fillna(0.0)
        keys = df["Account Alias"].map(lambda a: alias_key("" if a is None else str(a)))
        frame = pd.DataFrame({"key": keys, "spend": spend, "day": days_col})
        frame = frame[frame["key"] != ""]
        total = frame.groupby("key")["spend"].sum()
        recent = frame[(frame["day"] >= start) & (frame["day"] <= last)].groupby("key")["spend"].sum()
        return Activity(
            window_from=start.date().isoformat(),
            window_to=last.date().isoformat(),
            recent={k: round(float(v), 2) for k, v in recent.items() if float(v) > 0},
            ever={k for k, v in total.items() if float(v) > 0},
            names=sorted(total.index),
        )
    except Exception:  # pragma: no cover - кривая книга не должна гасить список
        return None


def workbook_key(label: str, activity: Activity, *, by_tail: bool = False) -> str:
    """Ключ, под которым книга знает этот аккаунт, — или пусто.

    `by_tail` — только для подключённых на машине: их имя дал Multilogin
    (`KLD_BFL_X84`), и с книжным (`FacebookMKTOBFLX84`) оно сходится лишь
    хвостом. Подписи из отчёта и так книжные — угадывать их по хвосту значило
    бы приписать аккаунту чужой спенд.
    """
    key = alias_key(label)
    if not key:
        return ""
    if key in activity.ever or key in activity.name_set:
        return key
    if not by_tail:
        return ""
    # Догадка — поэтому только единственное совпадение, как и в карте.
    from src.web.app import _alias_by_tail

    return _alias_by_tail(label, activity.names).casefold()


def split_available(
    candidates: list[str],
    *,
    connected: list[str],
    placed: list[str],
    activity: Activity | None,
    hidden: set[str],
) -> dict[str, list[dict]]:
    """Разложить неразмещённых по трём спискам: shown / inactive / hidden.

    `candidates` — то, что раньше целиком шло в Available accounts.
    `placed` — аккаунты, стоящие в какой-либо стране: тот же аккаунт под
    другой подписью (`FacebookDoc19 SA` из таблицы при размещённом
    `FacebookDoc19 - Eros`) тоже считается размещённым — это один аккаунт.
    `hidden` — ключи из `account_links.hidden_keys()`.
    """
    from src.web.account_links import hidden_key

    placed_keys = {alias_key(p) for p in placed if alias_key(p)}
    connected_keys = {hidden_key(c) for c in connected}
    shown: list[dict] = []
    inactive: list[dict] = []
    hidden_rows: list[dict] = []
    for label in candidates:
        if alias_key(label) in placed_keys:
            continue
        if hidden_key(label) in hidden:
            hidden_rows.append({"account": label})
            continue
        if activity is None:
            shown.append({"account": label, "status": "", "spend": None})
            continue
        is_connected = hidden_key(label) in connected_keys
        key = workbook_key(label, activity, by_tail=is_connected)
        spend = activity.recent.get(key) if key else None
        if spend:
            shown.append({"account": label, "status": "active", "spend": spend})
        elif is_connected and (not key or key not in activity.ever):
            shown.append({"account": label, "status": "new", "spend": None})
        else:
            inactive.append({"account": label, "status": "inactive", "spend": None})
    return {"shown": shown, "inactive": inactive, "hidden": hidden_rows}
