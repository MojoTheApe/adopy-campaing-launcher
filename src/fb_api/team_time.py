"""ACL-1448: время команды (Израиль) и перевод сетки расписания в часовой пояс кабинета.

Байер вводит Start/End и сетку часов по времени команды; Meta принимает только ADVERTISER/USER,
поэтому перевод наш. Всё здесь - чистые функции без сети: одни и те же входы дают один и тот же результат.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

DEFAULT_TEAM_TIMEZONE = "Asia/Jerusalem"
ENV_TEAM_TIMEZONE = "ACL_TEAM_TIMEZONE"
#: Как зону называют в сообщениях для байера.
TEAM_LABEL = "Israel"


class ShiftError(ValueError):
    """Разницу поясов нельзя выразить целыми часами: окна расписания не переводятся."""


def load_zone(name: str) -> ZoneInfo | None:
    """ZoneInfo по имени; пустое/незнакомое имя - None."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def team_timezone() -> str:
    """Имя пояса команды: `ACL_TEAM_TIMEZONE`, иначе Asia/Jerusalem. Неверное имя - дефолт и предупреждение."""
    raw = (os.environ.get(ENV_TEAM_TIMEZONE) or "").strip()
    if not raw:
        return DEFAULT_TEAM_TIMEZONE
    if load_zone(raw) is None:
        log.warning("%s=%r is not a valid time zone; using %s", ENV_TEAM_TIMEZONE, raw, DEFAULT_TEAM_TIMEZONE)
        return DEFAULT_TEAM_TIMEZONE
    return raw


def offset_difference_seconds(team_zone: str, account_zone: str, at: datetime) -> int:
    """Смещение кабинета минус смещение команды на момент `at` (секунды). `at` - с поясом."""
    if at.tzinfo is None:
        raise ValueError("`at` must be timezone-aware")
    team, acc = load_zone(team_zone), load_zone(account_zone)
    if team is None or acc is None:
        raise ValueError("unknown time zone")
    return int((at.astimezone(acc).utcoffset() - at.astimezone(team).utcoffset()).total_seconds())


def whole_hour_shift(team_zone: str, account_zone: str, at: datetime) -> int:
    """Разница поясов на момент `at` в целых часах; не целая - `ShiftError`."""
    diff = offset_difference_seconds(team_zone, account_zone, at)
    if diff % 3600:
        raise ShiftError(f"the time difference between {TEAM_LABEL} ({team_zone}) and the ad account "
                         f"({account_zone}) is {diff / 3600:g} hours, not a whole number of hours, "
                         "so the hours of the schedule cannot be converted")
    return diff // 3600


def team_grid_to_account_windows(windows: list[dict], team_zone: str, account_zone: str,
                                 at: datetime) -> list[dict]:
    """Окна сетки по времени команды -> окна ADVERTISER по времени кабинета.

    Разница поясов берётся на момент `at` (старт кампании). Каждая часовая клетка сдвигается; выход за
    полночь переносит её на соседний день недели (воскресенье 0 и суббота 6 замкнуты в кольцо); соседние
    клетки одного дня склеиваются в окно; дни с одинаковым окном собираются в одну запись, как отдаёт Meta.
    Порядок: по началу, концу, дням. Нецелая разница часов - `ShiftError`.
    """
    shift = whole_hour_shift(team_zone, account_zone, at)
    hours: dict[int, set[int]] = {d: set() for d in range(7)}
    for w in windows:
        for day in w["days"]:
            for hour in range(int(w["start_minute"]) // 60, int(w["end_minute"]) // 60):
                total = int(day) * 24 + hour + shift
                hours[(total // 24) % 7].add(total % 24)
    by_span: dict[tuple[int, int], list[int]] = {}
    for day in range(7):
        run_start = prev = None
        for hour in sorted(hours[day]) + [None]:
            if run_start is not None and (hour is None or hour != prev + 1):
                by_span.setdefault((run_start * 60, (prev + 1) * 60), []).append(day)
                run_start = None
            if hour is not None:
                if run_start is None:
                    run_start = hour
                prev = hour
    return [{"days": days, "start_minute": a, "end_minute": b, "timezone_type": "ADVERTISER"}
            for (a, b), days in sorted(by_span.items(), key=lambda kv: (kv[0], kv[1]))]


def shift_change_date(team_zone: str, account_zone: str, start: datetime, end: datetime):
    """Если разница поясов на `end` не та, что на `start`: (дата смены в поясе команды, прежняя разница с,
    новая разница с) - в секундах. Иначе None. Момент смены ищется делением пополам (до секунды)."""
    d0 = offset_difference_seconds(team_zone, account_zone, start)
    d1 = offset_difference_seconds(team_zone, account_zone, end)
    if d0 == d1:
        return None
    lo, hi = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    while (hi - lo).total_seconds() > 1:
        mid = lo + (hi - lo) / 2
        if offset_difference_seconds(team_zone, account_zone, mid) == d0:
            lo = mid
        else:
            hi = mid
    team = load_zone(team_zone)
    return hi.astimezone(team).date(), d0, d1


def shift_change_warning(team_zone: str, account_zone: str, start: datetime, end: datetime) -> str:
    """Предупреждение для Check plan, если разница между Израилем и кабинетом меняется внутри срока. Нет смены - пусто.

    Окна уходят в Meta по времени кабинета и дальше не пересчитываются; после смены часов они сдвигаются
    относительно Израиля на delta = новая_разница - старая: delta > 0 - показ раньше, delta < 0 - позже.
    """
    got = shift_change_date(team_zone, account_zone, start, end)
    if got is None:
        return ""
    when, d0, d1 = got
    delta = (d1 - d0) / 3600
    amount = f"{abs(delta):g} hour{'' if abs(delta) == 1 else 's'}"
    return (f"The time difference between {TEAM_LABEL} and the account changes on {when.isoformat()}; "
            f"from then on ads run {amount} {'earlier' if delta > 0 else 'later'} than planned.")


def localize(value: str, zone_name: str) -> datetime | None:
    """«YYYY-MM-DDTHH:MM» в поясе `zone_name`. Нет зоны -> ValueError; несуществующее время (дыра) -> None."""
    zone = load_zone(zone_name)
    if zone is None:
        raise ValueError(f"unknown time zone {zone_name!r}")
    naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    local = naive.replace(tzinfo=zone, fold=0)
    if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return local


__all__ = ["DEFAULT_TEAM_TIMEZONE", "ShiftError", "team_timezone", "team_grid_to_account_windows",
           "shift_change_warning", "shift_change_date", "whole_hour_shift", "offset_difference_seconds",
           "localize", "load_zone", "TEAM_LABEL"]
