"""ACL-1448: Start/End и сетка расписания вводятся по времени команды (Израиль) и переводятся в пояс кабинета.

Ни Meta, ни Multilogin не трогаются: Graph - `FakeMeta` в памяти. Перевод сетки - чистая функция
(`team_time.team_grid_to_account_windows`), поэтому проверяется и отдельно, и через создателя кампании.
Daily остаётся как был: ключа `time_zone` нет ни в слоте, ни в черновике, ни в плане.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import team_time as tt  # noqa: E402
from tests.test_acl1443_lifetime_budget import ZoneMeta, _creator, _life, _plan, _run  # noqa: E402
from tests.test_acl1761_api_create import _draft  # noqa: E402

ISRAEL = "Asia/Jerusalem"
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]
#: «Не крутить 01-05 по Израилю»: все часы, кроме 01:00-05:00, во все 7 дней.
NO_01_05 = [{"days": EVERY_DAY, "start_minute": 0, "end_minute": 60},
            {"days": EVERY_DAY, "start_minute": 300, "end_minute": 1440}]


def _at(y, m, d, hh=9, mm=0, zone=ISRAEL):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(zone))


def _w(days, a, b):
    return {"days": days, "start_minute": a * 60, "end_minute": b * 60, "timezone_type": "ADVERTISER"}


# ───────────────────────── пояс команды ─────────────────────────

def test_team_timezone_default_override_and_invalid_name(monkeypatch, caplog):
    monkeypatch.delenv("ACL_TEAM_TIMEZONE", raising=False)
    assert tt.team_timezone() == "Asia/Jerusalem"
    monkeypatch.setenv("ACL_TEAM_TIMEZONE", "Europe/Bucharest")
    assert tt.team_timezone() == "Europe/Bucharest"
    monkeypatch.setenv("ACL_TEAM_TIMEZONE", "Mars/Olympus")
    with caplog.at_level("WARNING"):
        assert tt.team_timezone() == "Asia/Jerusalem"
    assert "Mars/Olympus" in caplog.text


# ───────────────────────── перевод сетки ─────────────────────────

def test_new_york_is_seven_hours_behind_and_the_cut_hours_move_to_the_previous_day():
    # Ноябрь 2026: Израиль UTC+2, Нью-Йорк UTC-5. Израиль 01-05 = Нью-Йорк 18-22 ПРЕДЫДУЩЕГО дня.
    got = tt.team_grid_to_account_windows(NO_01_05, ISRAEL, "America/New_York", _at(2026, 11, 10))
    assert got == [_w(EVERY_DAY, 0, 18), _w(EVERY_DAY, 22, 24)]


def test_one_israeli_window_lands_on_the_previous_weekday_in_new_york():
    monday_night = [{"days": [1], "start_minute": 60, "end_minute": 300}]       # Mon 01-05 Israel
    got = tt.team_grid_to_account_windows(monday_night, ISRAEL, "America/New_York", _at(2026, 11, 10))
    assert got == [_w([0], 18, 22)]                                              # Sun 18-22 New York


def test_bucharest_has_no_shift_and_the_grid_is_only_regrouped():
    got = tt.team_grid_to_account_windows(NO_01_05, ISRAEL, "Europe/Bucharest", _at(2026, 11, 10))
    assert got == [_w(EVERY_DAY, 0, 1), _w(EVERY_DAY, 5, 24)]


def test_sunday_goes_back_to_saturday_and_saturday_forward_to_sunday():
    sunday = [{"days": [0], "start_minute": 0, "end_minute": 120}]               # Sun 00-02 Israel
    assert tt.team_grid_to_account_windows(sunday, ISRAEL, "America/New_York", _at(2026, 11, 10)) == [_w([6], 17, 19)]
    saturday = [{"days": [6], "start_minute": 22 * 60, "end_minute": 1440}]      # Sat 22-24 Israel
    # Токио UTC+9: Израиль +7 часов.
    assert tt.team_grid_to_account_windows(saturday, ISRAEL, "Asia/Tokyo", _at(2026, 11, 10)) == [_w([0], 5, 7)]


def test_a_window_crossing_midnight_is_split_between_two_days():
    late = [{"days": [2], "start_minute": 16 * 60, "end_minute": 1440}]          # Tue 16-24 Israel
    got = tt.team_grid_to_account_windows(late, ISRAEL, "Asia/Tokyo", _at(2026, 11, 10))   # +7 часов
    assert got == [_w([3], 0, 7), _w([2], 23, 24)]                                # Tue 23-24 + Wed 00-07


def test_days_with_the_same_window_share_one_record_in_day_order():
    mon_wed = [{"days": [3, 1], "start_minute": 540, "end_minute": 600}]
    got = tt.team_grid_to_account_windows(mon_wed, ISRAEL, "Europe/Bucharest", _at(2026, 11, 10))
    assert got == [_w([1, 3], 9, 10)]


def test_the_conversion_is_deterministic():
    runs = [tt.team_grid_to_account_windows(NO_01_05, ISRAEL, "America/New_York", _at(2026, 11, 10)) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_a_half_hour_difference_is_refused():
    # Индия UTC+5:30, Израиль в ноябре +2 -> 3.5 часа.
    with pytest.raises(tt.ShiftError, match="not a whole number of hours"):
        tt.team_grid_to_account_windows(NO_01_05, ISRAEL, "Asia/Kolkata", _at(2026, 11, 10))


# ───────────────────────── смена разницы поясов между стартом и концом ─────────────────────────

def test_a_change_of_the_difference_gives_a_warning_with_the_date():
    # Израиль переводит часы 25.10.2026, Нью-Йорк - 01.11.2026: между ними разница 6 часов вместо 7.
    note = tt.shift_change_warning(ISRAEL, "America/New_York", _at(2026, 10, 20), _at(2026, 10, 30))
    assert "changes on 2026-10-25" in note and "1 hour earlier than planned" in note
    back = tt.shift_change_warning(ISRAEL, "America/New_York", _at(2026, 10, 30), _at(2026, 11, 5))
    assert "changes on 2026-11-01" in back and "1 hour later than planned" in back


def test_no_warning_when_the_difference_stays():
    assert tt.shift_change_warning(ISRAEL, "America/New_York", _at(2026, 11, 3), _at(2026, 11, 9)) == ""


# ───────────────────────── создатель кампании ─────────────────────────

def _israel(tmp_path, **over):
    d = _life(tmp_path, start="2026-11-10T09:00", end="2026-11-15T09:00", schedule=NO_01_05, **over)
    d.ad_sets[0].time_zone = ""            # как с формы без явного пояса: берётся пояс команды
    return d


def test_start_end_and_schedule_are_converted_from_israel_to_the_account(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_TEAM_TIMEZONE", raising=False)
    creator = _creator(ZoneMeta())
    plan = _plan(_israel(tmp_path))
    s = plan.ad_sets[0]
    assert s.time_zone == ISRAEL and s.schedule == NO_01_05          # план хранит сетку как введена
    params = creator.adset_params(plan, s, "1", {"geo_locations": {}})
    assert params["start_time"] == "2026-11-10T09:00:00+0200" and params["end_time"] == "2026-11-15T09:00:00+0200"
    assert params["adset_schedule"] == [_w(EVERY_DAY, 0, 18), _w(EVERY_DAY, 22, 24)]


def test_the_plan_summary_shows_converted_windows_and_the_zone(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_TEAM_TIMEZONE", raising=False)
    out = _creator(ZoneMeta()).plan_only(_plan(_israel(tmp_path)))
    assert out.error == ""
    row = out.plan["summary"]["ad_sets"][0]
    assert row["time_zone"] == ISRAEL
    assert "Mon 00:00-18:00" in row["schedule"] and "Mon 22:00-24:00" in row["schedule"] and len(row["schedule"]) == 14
    assert not any("changes on" in w for w in out.warnings)


def test_the_change_of_the_difference_is_a_warning_not_an_error_in_check_plan(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_TEAM_TIMEZONE", raising=False)
    d = _life(tmp_path, start="2026-10-20T09:00", end="2026-10-30T09:00", schedule=NO_01_05)
    d.ad_sets[0].time_zone = ""
    out = _creator(ZoneMeta()).plan_only(_plan(d))
    assert out.error == ""
    assert any("changes on 2026-10-25" in w and "earlier than planned" in w for w in out.warnings)


def test_a_non_whole_hour_difference_stops_the_plan(tmp_path):
    out = _creator(ZoneMeta(tz="Asia/Kolkata")).plan_only(_plan(_israel(tmp_path)))
    assert "not a whole number of hours" in out.error


def test_an_unknown_account_zone_stops_the_schedule_but_not_the_start(tmp_path):
    creator = _creator(ZoneMeta(tz=None))
    plan = _plan(_israel(tmp_path))
    assert creator.start_iso(plan.ad_sets[0]) == "2026-11-10T09:00:00+0200"       # Израиль зону кабинета не ждёт
    with pytest.raises(cr.PlanError, match="cannot be converted"):
        creator.schedule_windows(plan.ad_sets[0])


def test_a_time_in_the_israeli_dst_gap_is_refused_naming_israel(tmp_path):
    creator = _creator(ZoneMeta())
    d = _life(tmp_path, start="2027-03-26T02:30", end="2027-04-02T09:00")
    d.ad_sets[0].time_zone = ""
    plan = _plan(d)
    with pytest.raises(cr.PlanError, match="does not exist in Israel time"):
        creator.start_iso(plan.ad_sets[0])


def test_start_in_the_past_is_judged_in_absolute_time_of_israel(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_TEAM_TIMEZONE", raising=False)
    # NOW = 12.10.2026 12:00 UTC = 15:00 в Израиле: 14:00 уже прошло, 16:00 - нет.
    past = _life(tmp_path, start="2026-10-12T14:00", end="2026-10-20T09:00")
    past.ad_sets[0].time_zone = ""
    assert "Israel time" in _creator(ZoneMeta()).plan_only(_plan(past)).error
    ok = _life(tmp_path, start="2026-10-12T16:00", end="2026-10-20T09:00")
    ok.ad_sets[0].time_zone = ""
    assert _creator(ZoneMeta()).plan_only(_plan(ok)).error == ""


def test_the_env_override_sets_the_zone_of_the_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_TEAM_TIMEZONE", "Europe/Bucharest")
    assert _plan(_israel(tmp_path)).ad_sets[0].time_zone == "Europe/Bucharest"


def test_a_lifetime_campaign_is_written_and_verified_against_the_converted_windows(tmp_path, monkeypatch):
    monkeypatch.delenv("ACL_TEAM_TIMEZONE", raising=False)
    meta = ZoneMeta()
    out = _run(meta, _israel(tmp_path))
    assert out.error == "" and out.readback["ok"], out.mismatches
    aset = next(p for path, p in meta.posts if path.endswith("/adsets"))
    assert aset["adset_schedule"] == [_w(EVERY_DAY, 0, 18), _w(EVERY_DAY, 22, 24)]
    # Meta вернула те же часы по дням в другом порядке - это то же множество.
    meta2 = ZoneMeta()
    meta2.mutate_on_read[("adset", "adset_schedule")] = (
        [_w([d], 22, 24) for d in EVERY_DAY] + [_w([d], 0, 18) for d in EVERY_DAY])
    assert _run(meta2, _israel(tmp_path)).readback["ok"]
    # А непереведённая сетка (как введена по Израилю) - расхождение.
    meta3 = ZoneMeta()
    meta3.mutate_on_read[("adset", "adset_schedule")] = [{**w, "timezone_type": "ADVERTISER"} for w in NO_01_05]
    assert "adset_schedule" in [m["field"] for m in _run(meta3, _israel(tmp_path)).mismatches]


def test_daily_plan_has_no_time_zone(tmp_path):
    plan = _plan(_draft(tmp_path, ads=1))
    assert plan.ad_sets[0].time_zone == ""
    out = _creator(ZoneMeta()).plan_only(plan)
    assert "time_zone" not in out.plan["summary"]["ad_sets"][0]


# ───────────────────────── форма -> слот -> черновик ─────────────────────────

def _fields(**over):
    f = {"country": "Mexico", "account_label": "Acc", "tracking_link": "https://t.invalid/x", "camp_count": "1",
         "camp_0_name": "MX_test", "camp_0_ad_count": "1", "camp_0_budget": "120.50",
         "camp_0_ad_0_creative_file": "a.png", "camp_0_ad_0_copy": "T1"}
    f.update(over)
    return f


def test_the_time_zone_is_stored_only_for_lifetime():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields(
        camp_0_budget_type="lifetime", camp_0_start_time="2026-11-10T09:00", camp_0_end_time="2026-11-15T09:00",
        camp_0_time_zone=ISRAEL, camp_0_schedule=json.dumps(NO_01_05)))
    assert slots[0]["time_zone"] == ISRAEL
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert draft.ad_sets[0].time_zone == ISRAEL and draft.ad_sets[0].to_preview_dict()["time_zone"] == ISRAEL
    # Daily: поле пришло, но в слот и черновик не попадает.
    _c, _l, daily = web._parse_build_upload_form(_fields(camp_0_budget_type="daily", camp_0_time_zone=ISRAEL))
    assert "time_zone" not in daily[0]
    [d2] = slots_to_campaign_drafts(daily, account_label=label, country=country)
    assert "time_zone" not in d2.ad_sets[0].to_preview_dict()


# ───────────────────────── страница: пресет, подписи и строка «что получит Meta» ─────────────────────────

REPORT = ROOT / "src" / "web" / "templates" / "report.html"


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    import os
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        except Exception as exc:  # noqa: BLE001 - нет браузера на машине
            pytest.skip(f"no browser: {exc}")
        yield b
        b.close()


def _module_source() -> str:
    src = REPORT.read_text(encoding="utf-8")
    start = src.index("window.cbLifetime = (function")
    end = src.index("window.builderAccountFacts = window.cbLifetime.facts;")
    return src[start:end] + "window.builderAccountFacts = window.cbLifetime.facts;"


@pytest.fixture
def lt(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.set_content('<div class="cb-acc-row" data-acc-label="NY"><div class="cb-slot" id="slot">'
                   '<input name="camp_0_budget" value="50"></div></div>')
    pg.add_script_tag(content="window.ACL_FB_BACKEND='api'; window.CB_TEAM_TIMEZONE='Asia/Jerusalem';"
                              "window.CB_ACCOUNT_FACTS={NY:{timezone_name:'America/New_York',currency:'USD'},"
                              "KO:{timezone_name:'Asia/Kolkata',currency:'USD'}};\n" + _module_source())
    pg.evaluate("""() => {
        const slot = document.getElementById('slot');
        slot.insertAdjacentHTML('beforeend', cbLifetime.segHtml(0) + cbLifetime.rowsHtml(0));
        slot.querySelector('[data-budget-type="lifetime"]').click();
        slot.querySelector('[name$="_start_time"]').value = '2026-11-10T09:00';
        slot.querySelector('[name$="_end_time"]').value = '2026-11-15T09:00';
        const on = slot.querySelector('.cb-sched-on'); on.checked = true; on.dispatchEvent(new Event('change'));
    }""")
    yield pg
    assert not errors, errors
    ctx.close()


def test_labels_and_aria_say_israel_time(lt):
    text = lt.evaluate("document.getElementById('slot').innerText")
    assert "Start (Israel time)" in text and "End (Israel time)" in text
    assert lt.get_attribute(".cb-sg", "aria-label") == "Hours when ads run, Israel time"
    assert lt.get_attribute("[name$='_start_time']", "aria-label") == "Start time, Israel time"
    assert "Times are Israel time (Asia/Jerusalem). Account: America/New_York (UTC-5)" in \
        lt.inner_text("[data-lt-tz]") or "Times are Israel time (Asia/Jerusalem). Account: unknown" in lt.inner_text("[data-lt-tz]")


def test_the_time_zone_field_follows_the_budget_type(lt):
    assert lt.evaluate("(() => { const f = document.querySelector('[name$=\"_time_zone\"]');"
                       " return [f.value, f.disabled]; })()") == ["Asia/Jerusalem", False]
    lt.click('[data-budget-type="daily"]')
    assert lt.evaluate("document.querySelector('[name$=\"_time_zone\"]').disabled") is True


def test_the_preset_clears_01_to_05_on_every_day_and_the_preview_shows_account_time(lt):
    lt.click(".cb-lt-preset")
    cells = lt.evaluate("[...document.querySelectorAll('.cb-sg-cell')].map(c => [+c.dataset.d, +c.dataset.h, c.classList.contains('is-on')])")
    assert len(cells) == 168
    assert all(on == (h not in (1, 2, 3, 4)) for _d, h, on in cells)
    assert json.loads(lt.input_value("[name$='_schedule']")) == NO_01_05
    # Формулы сервера: Нью-Йорк на 10 ноября = Израиль минус 7 часов.
    assert lt.inner_text("[data-lt-acc-preview]") == \
        "Meta gets account time (America/New_York): Every day: 00:00–18:00, 22:00–24:00"


def test_the_preview_asks_to_wait_for_check_plan_when_the_account_zone_is_unknown(lt):
    lt.evaluate("document.querySelector('.cb-acc-row').dataset.accLabel = 'unknown-account'")
    lt.click(".cb-lt-preset")
    assert lt.inner_text("[data-lt-acc-preview]") == "Account time zone is read on Check plan; hours are converted then."


def test_the_page_algorithm_matches_the_server_one(lt):
    got = lt.evaluate("cbLifetime.convertWindows([{days:[1],start_minute:60,end_minute:300}], -7)")
    assert got == [{"days": [0], "start_minute": 18 * 60, "end_minute": 22 * 60}]
    got = lt.evaluate("cbLifetime.convertWindows([{days:[0],start_minute:0,end_minute:120}], -7)")
    assert got == [{"days": [6], "start_minute": 17 * 60, "end_minute": 19 * 60}]
    got = lt.evaluate("cbLifetime.convertWindows([{days:[6],start_minute:1320,end_minute:1440}], 7)")
    assert got == [{"days": [0], "start_minute": 5 * 60, "end_minute": 7 * 60}]
    # Разные часы по дням: дни с одинаковым текстом сжаты в диапазон.
    lt.evaluate("""() => { const hid = document.querySelector('[name$="_schedule"]');
        hid.disabled = false;
        hid.value = JSON.stringify([{days:[1,2,3,4,5], start_minute:540, end_minute:1080}]);
        cbLifetime.sync(document.getElementById('slot')); }""")
    assert lt.inner_text("[data-lt-acc-preview]") == "Meta gets account time (America/New_York): Mon–Fri 02:00–11:00"


def test_a_half_hour_account_zone_is_flagged_in_the_preview(lt):
    lt.evaluate("document.querySelector('.cb-acc-row').dataset.accLabel = 'KO'")
    lt.click(".cb-lt-preset")
    assert "not a whole number of hours" in lt.inner_text("[data-lt-acc-preview]")


def test_start_in_the_past_is_compared_by_the_absolute_time_of_israel(lt):
    lt.evaluate("document.querySelector('[name$=\"_start_time\"]').value = '2020-01-01T10:00'")
    assert any("Israel time" in p["text"] for p in lt.evaluate("cbLifetime.problems(document.getElementById('slot'))"))
    lt.evaluate("document.querySelector('[name$=\"_start_time\"]').value = '2999-01-01T10:00'")
    assert not any(p["id"] == "lt_past" for p in lt.evaluate("cbLifetime.problems(document.getElementById('slot'))"))
