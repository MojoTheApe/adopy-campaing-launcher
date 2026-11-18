"""ACL-2666: часы расписания из Израиля в пояс кабинета - по реальным правилам поясов на даты показа."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import team_time as tt  # noqa: E402
from tests.test_acl1443_lifetime_budget import ZoneMeta, _creator, _life, _plan  # noqa: E402

ISRAEL = "Asia/Jerusalem"
MON_FRI, EVERY_DAY = [1, 2, 3, 4, 5], [0, 1, 2, 3, 4, 5, 6]
#: Сетка байера: Пн-Пт 00-02 и Пн-Вс 15-24 по Израилю.
BUYER_GRID = [{"days": MON_FRI, "start_minute": 0, "end_minute": 120},
              {"days": EVERY_DAY, "start_minute": 900, "end_minute": 1440}]


@pytest.fixture(autouse=True)
def _israel_team(monkeypatch):
    monkeypatch.delenv("ACL_TEAM_TIMEZONE", raising=False)


def _w(days, a, b):
    return {"days": days, "start_minute": a * 60, "end_minute": b * 60, "timezone_type": "ADVERTISER"}


def _payload(tmp_path, zone, start, end, grid=BUYER_GRID):
    creator = _creator(ZoneMeta(tz=zone))
    plan = _plan(_life(tmp_path, start=start, end=end, schedule=grid))
    return creator, plan, creator.adset_params(plan, plan.ad_sets[0], "1", {"geo_locations": {}})


def _by_day(windows):
    out = {}
    for w in windows:
        for d in w["days"]:
            out.setdefault(d, []).append((w["start_minute"] // 60, w["end_minute"] // 60))
    return {d: sorted(v) for d, v in out.items()}


def test_buyers_example_new_york_gives_mon_thu_8_19_fri_sat_8_17_sun_8_19(tmp_path):
    _, _, params = _payload(tmp_path, "America/New_York", "2026-11-17T00:00", "2026-11-30T23:59")
    got = params["adset_schedule"]
    # Пн-Чт 08-19, Пт-Сб 08-17, Вс 08-19 (Израиль UTC+2, Нью-Йорк UTC-5: разница 7 часов, не 8).
    assert got == [_w([5, 6], 8, 17), _w([0, 1, 2, 3, 4], 8, 19)]
    assert _by_day(got) == {0: [(8, 19)], 1: [(8, 19)], 2: [(8, 19)], 3: [(8, 19)], 4: [(8, 19)],
                            5: [(8, 17)], 6: [(8, 17)]}
    assert all(w["timezone_type"] == "ADVERTISER" for w in got)


def test_the_check_plan_names_the_account_zone_and_the_difference(tmp_path):
    creator, plan, _ = _payload(tmp_path, "America/New_York", "2026-11-17T00:00", "2026-11-30T23:59")
    note = creator.schedule_zone_note(plan.ad_sets[0])
    assert "America/New_York" in note and "UTC-5" in note and "-7 h" in note and "2026-11-17" in note


def _reference(grid, zone, on):
    """Независимый расчёт: каждая клетка Израиля - реальный момент на дате `on`, её час и день недели в поясе кабинета."""
    cells = {}
    base = datetime(on.year, on.month, on.day, tzinfo=ZoneInfo(ISRAEL))
    # Понедельник недели `on` (0 = воскресенье в Meta, 1 = понедельник).
    monday = base - timedelta(days=base.weekday())
    for w in grid:
        for day in w["days"]:
            for hour in range(w["start_minute"] // 60, w["end_minute"] // 60):
                il = (monday + timedelta(days=(day - 1) % 7, hours=hour)).replace(tzinfo=None)
                il = il.replace(tzinfo=ZoneInfo(ISRAEL))
                acc = il.astimezone(ZoneInfo(zone))
                cells.setdefault((acc.weekday() + 1) % 7, set()).add(acc.hour)
    out = {}
    for d, hours in cells.items():
        runs, h = [], sorted(hours)
        start = prev = h[0]
        for x in h[1:] + [None]:
            if x is None or x != prev + 1:
                runs.append((start, prev + 1))
                start = x
            prev = x if x is not None else prev
        out[d] = runs
    return out


def test_account_zone_ahead_of_israel_moves_hours_later(tmp_path):
    # Токио UTC+9, Израиль UTC+2 (ноябрь): разница +7 часов.
    _, _, params = _payload(tmp_path, "Asia/Tokyo", "2026-11-17T00:00", "2026-11-30T23:59")
    got = _by_day(params["adset_schedule"])
    assert got == _reference(BUYER_GRID, "Asia/Tokyo", datetime(2026, 11, 17))
    assert got[1] == [(0, 9), (22, 24)]          # Пн: хвост Вс 15-24 (->22-07) и Пн 00-02 (->07-09), плюс Пн 15-24 -> 22-24
    assert tt.whole_hour_shift(ISRAEL, "Asia/Tokyo", datetime(2026, 11, 17, tzinfo=ZoneInfo(ISRAEL))) == 7


def test_account_zone_behind_israel_moves_hours_earlier(tmp_path):
    # Лос-Анджелес UTC-8, Израиль UTC+2: разница -10 часов.
    _, _, params = _payload(tmp_path, "America/Los_Angeles", "2026-11-17T00:00", "2026-11-30T23:59")
    got = _by_day(params["adset_schedule"])
    assert got == _reference(BUYER_GRID, "America/Los_Angeles", datetime(2026, 11, 17))
    assert tt.whole_hour_shift(ISRAEL, "America/Los_Angeles", datetime(2026, 11, 17, tzinfo=ZoneInfo(ISRAEL))) == -10


def test_a_run_across_the_dst_change_uses_the_start_date_and_warns(tmp_path):
    # 26.10-10.11: Израиль перевёл часы 25.10, Нью-Йорк переводит 01.11. Старт 26.10 (Израиль +2, Нью-Йорк -4): разница -6.
    creator, plan, params = _payload(tmp_path, "America/New_York", "2026-10-26T09:00", "2026-11-10T09:00")
    got = _by_day(params["adset_schedule"])
    # Разница -6 на старте: Пн-Чт 09-20, Пт-Сб 09-18 (на час позже, чем при -7); Вс 09-20.
    assert got == {0: [(9, 20)], 1: [(9, 20)], 2: [(9, 20)], 3: [(9, 20)], 4: [(9, 20)], 5: [(9, 18)], 6: [(9, 18)]}
    warns = creator.schedule_shift_warnings(plan)
    assert len(warns) == 1 and "changes on 2026-11-01" in warns[0] and "1 hour later than planned" in warns[0]
    # Тот же срок со стартом после 01.11 даёт сдвиг -7 и без предупреждения.
    creator2, plan2, later = _payload(tmp_path, "America/New_York", "2026-11-17T00:00", "2026-11-30T23:59")
    assert later["adset_schedule"] != params["adset_schedule"]
    assert creator2.schedule_shift_warnings(plan2) == []


def test_prepare_puts_the_zone_note_into_the_check_plan_warnings(tmp_path):
    from src.fb_api import creator as cr
    creator, plan, _ = _payload(tmp_path, "America/New_York", "2026-11-17T00:00", "2026-11-30T23:59")
    out = cr.Outcome(campaign_name="x")
    try:
        creator._prepare(plan, out, writing=False)
    except Exception:
        pass  # later checks (page, files) may refuse on the stub; the zone note is added before them
    assert any("America/New_York" in w and "-7 h" in w for w in out.warnings)
