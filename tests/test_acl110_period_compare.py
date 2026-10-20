"""ACL-110: сравнение выбранного периода с другим.

Решения владельца 2026-10-20: два режима (предыдущий период той же длины и свой
диапазон); изменение — «—», когда прошлое значение ноль или его нет; строки,
которые есть только в одном периоде, помечены new / gone.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.web import app as web_app  # noqa: E402
from src.web import period_compare as pc  # noqa: E402


# ── периоды ────────────────────────────────────────────────────────────────
def test_previous_period_has_the_same_length_and_touches_the_selected_one():
    assert pc.previous_period("2026-10-07", "2026-10-13") == ("2026-09-30", "2026-10-06")
    assert pc.previous_period("2026-10-01", "2026-10-01") == ("2026-09-30", "2026-09-30")
    # через границу месяца и високосный год
    assert pc.previous_period("2028-03-01", "2028-03-03") == ("2028-02-27", "2028-02-29")


@pytest.mark.parametrize("a,b", [("", ""), ("junk", "2026-10-01"), ("2026-10-05", "2026-10-01")])
def test_no_previous_period_without_a_valid_range(a, b):
    assert pc.previous_period(a, b) is None


def test_compare_off_by_default():
    assert pc.resolve_compare("", "", "", "2026-10-01", "2026-10-07") == (None, "")


def test_prev_mode_resolves():
    info, note = pc.resolve_compare("prev", "", "", "2026-10-07", "2026-10-13")
    assert note == ""
    assert (info["mode"], info["date_from"], info["date_to"]) == ("prev", "2026-09-30", "2026-10-06")


def test_custom_mode_uses_the_picked_range_and_defaults_the_end():
    info, _ = pc.resolve_compare("custom", "2026-08-01", "2026-08-07", "2026-10-07", "2026-10-13")
    assert (info["date_from"], info["date_to"]) == ("2026-08-01", "2026-08-07")
    info, _ = pc.resolve_compare("custom", "2026-08-01", "", "2026-10-07", "2026-10-13")
    assert (info["date_from"], info["date_to"]) == ("2026-08-01", "2026-08-01")


@pytest.mark.parametrize("args,why", [
    (("custom", "", "", "2026-10-07", "2026-10-13", ""), "valid start"),
    (("custom", "2026-08-09", "2026-08-01", "2026-10-07", "2026-10-13", ""), "before it starts"),
    (("prev", "", "", "", "", "all"), "Pick a date range"),
    (("prev", "", "", "", "", ""), "Pick a date range"),
    (("weekly", "", "", "2026-10-07", "2026-10-13", ""), "Unknown"),
])
def test_a_comparison_that_cannot_be_shown_says_why(args, why):
    info, note = pc.resolve_compare(*args)
    assert info is None
    assert why in note


# ── изменение ──────────────────────────────────────────────────────────────
def test_delta_is_a_percent_of_the_earlier_value():
    assert pc.delta_text(120, 100) == ("+20%", "up")
    assert pc.delta_text(75, 100) == ("−25%", "down")
    assert pc.delta_text(100, 100) == ("0%", "flat")


@pytest.mark.parametrize("cur,prev", [(5, 0), (5, None), (None, 5), (5, "x"), (float("nan"), 5), (5, float("inf"))])
def test_delta_is_a_dash_when_the_earlier_value_is_zero_or_missing(cur, prev):
    assert pc.delta_text(cur, prev) == ("—", "")


def test_metric_compare_shows_earlier_values_and_changes_for_all_six_metrics():
    cur = {"spend": 200, "leads": 20, "deposits": 4}
    prev = {"spend": 100, "leads": 10, "deposits": 2}
    cells = pc.metric_compare(cur, prev)
    assert [c["prev"] for c in cells] == ["$100", "10", "2", "$10", "$50", "20.0%"]
    assert [c["delta"] for c in cells] == ["+100%", "+100%", "+100%", "0%", "0%", "0%"]


def test_cost_metrics_are_coloured_the_other_way_round():
    cur = {"spend": 100, "leads": 5, "deposits": 1}      # CPL 20, CPA 100
    prev = {"spend": 100, "leads": 10, "deposits": 2}    # CPL 10, CPA 50
    c = pc.metric_compare(cur, prev)
    assert c[1]["tone"] == "bad"      # меньше лидов — хуже
    assert c[3]["tone"] == "bad"      # CPL вырос — хуже
    assert c[0]["tone"] == ""         # спенд не хорош и не плох


def test_ratio_without_a_divisor_has_no_delta_instead_of_a_fake_one():
    cur = {"spend": 50, "leads": 5, "deposits": 0}
    prev = {"spend": 50, "leads": 5, "deposits": 0}
    cells = pc.metric_compare(cur, prev)
    assert cells[4]["prev"] == "—" and cells[4]["delta"] == "—"   # CPA: FTD нет


def test_missing_counterpart_is_explicit():
    cells = pc.metric_compare({"spend": 1, "leads": 1, "deposits": 1}, None)
    assert all(c["prev"] == "—" and c["delta"] == "—" for c in cells)
    gone = pc.metric_compare(None, {"spend": 10, "leads": 2, "deposits": 1})
    assert gone[0]["prev"] == "$10" and gone[0]["delta"] == "—"


def test_row_states():
    assert pc.row_state(True, False) == "new"
    assert pc.row_state(False, True) == "gone"
    assert pc.row_state(True, True) == "both"
    assert pc.row_state(False, False) == ""
    assert pc.state_for({"spend": 5}, None) == "new"
    assert pc.state_for({"spend": 0, "leads": 0, "deposits": 0}, {"spend": 5}) == "gone"
    # строка без данных у аккаунта — это «нет строки», а не «ноль»
    assert pc.state_for({"has_data": False, "spend": 0}, {"spend": 5, "has_data": True}) == "gone"


# ── PeriodCompare: что было с этой строкой раньше ──────────────────────────
def _prev_report():
    return {"countries": [{
        "country": "Costa Rica",
        "cm_comparison": [
            {"cm": "Ann", "spend": 100, "leads": 10, "deposits": 2},
            {"cm": "Bob", "spend": 50, "leads": 5, "deposits": 1},
        ],
        "close_recs": {"CR_1": [
            {"adset_name": "AS A", "spend": 30, "leads": 3, "deposits": 0},
            {"adset_name": "AS B", "spend": 20, "leads": 2, "deposits": 1},
        ]},
        "open_recs": {"CR_1": [
            {"strategy_type": 1, "targeting": "Yoga"},
            {"strategy_type": 2, "targeting": "Chess"},
        ]},
    }]}


def _prev_amap():
    return {
        "countries": [{
            "country": "Costa Rica",
            "book": {"spend": 150, "leads": 15, "deposits": 3},
            "rows": [
                {"account": "CR_1", "has_data": True, "spend": 90, "leads": 9, "deposits": 2},
                {"account": "CR_2", "has_data": False, "spend": 0, "leads": 0, "deposits": 0},
            ],
        }],
        "totals": {"spend": 150, "leads": 15, "deposits": 3},
        "cm_section": [{"country": "Costa Rica", "spend": 150, "leads": 15, "deposits": 3,
                        "rows": [{"cm": "Ann"}, {"cm": "Bob"}]}],
    }


def _cmp():
    info = {"mode": "prev", "date_from": "2026-09-30", "date_to": "2026-10-06"}
    return pc.PeriodCompare(info, _prev_report(), _prev_amap())


def test_label_names_the_earlier_period():
    assert _cmp().label == "2026-09-30 → 2026-10-06"


def test_lookups_find_earlier_rows_by_normalised_name():
    c = _cmp()
    assert c.account("costa  rica", "cr_1")["spend"] == 90
    assert c.account("Costa Rica", "CR_2") is None          # аккаунт без данных тогда
    assert c.country_book("Costa Rica")["spend"] == 150
    assert c.cm("Costa Rica", "ann")["spend"] == 100
    assert c.cm("Costa Rica", "Zed") is None
    assert c.adset("Costa Rica", "CR_1", "as a")["spend"] == 30
    assert c.totals()["spend"] == 150
    assert c.cm_group("Costa Rica")["spend"] == 150


def test_rows_gone_since_the_earlier_period():
    c = _cmp()
    assert [r["cm"] for r in c.cm_gone("Costa Rica", ["Ann"])] == ["Bob"]
    assert c.cm_gone("Costa Rica", ["Ann", "Bob"]) == []
    assert [r["adset_name"] for r in c.adset_gone("Costa Rica", "CR_1", ["AS A"])] == ["AS B"]


def test_open_recommendations_are_compared_by_strategy_and_targeting_not_by_name():
    c = _cmp()
    now = [{"strategy_type": 1, "targeting": "yoga", "campaign_name": "other date"},
           {"strategy_type": 3, "targeting": "Salsa"}]
    d = c.open_recs_diff("Costa Rica", "CR_1", now)
    assert (d["now"], d["before"]) == (2, 2)
    assert d["new"] == ["Salsa"] and d["gone"] == ["Chess"]


def test_an_earlier_period_without_rows_is_reported_empty():
    empty = pc.PeriodCompare({"mode": "prev", "date_from": "a", "date_to": "a"},
                             {"countries": []},
                             {"countries": [], "totals": None, "cm_section": []})
    assert empty.empty
    assert not _cmp().empty


# ── шаблоны ────────────────────────────────────────────────────────────────
def _macros():
    return web_app.templates.env.get_template("_metrics.html").module


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def test_cells_without_compare_are_unchanged():
    html = str(_macros().cells(100, 10, 2))
    assert "cmp" not in html
    assert html.count("<td") == 6


def test_cells_with_compare_show_earlier_value_and_delta():
    html = str(_macros().cells(200, 20, 4, prev={"spend": 100, "leads": 10, "deposits": 2}))
    assert html.count('class="cmp"') == 6
    assert "+100%" in html and "$100" in html


def test_cells_for_a_row_with_no_earlier_counterpart_show_dashes_not_zero():
    html = str(_macros().cells(200, 20, 4, prev=None))
    assert html.count('class="cmp"') == 6
    deltas = re.findall(r'cmp-delta[^"]*">([^<]*)<', html)
    prevs = re.findall(r'cmp-prev"[^>]*>([^<]*)<', html)
    assert deltas == ["—"] * 6 and prevs == ["—"] * 6


def test_gone_row_has_dashes_for_now_and_earlier_values_below():
    html = str(_macros().cells(0, 0, 0, prev={"spend": 10, "leads": 2, "deposits": 1}, gone=True))
    assert _text(html).startswith("— ")
    assert "$10" in html


def test_spans_follow_the_same_rule():
    plain = str(_macros().spans(100, 10, 2))
    assert "cmp" not in plain
    withp = str(_macros().spans(150, 15, 3, prev={"spend": 100, "leads": 10, "deposits": 2}))
    assert withp.count('class="cmp"') == 6 and "+50%" in withp


def test_flag_marks_new_and_gone_only():
    m = _macros()
    assert "cmp-flag new" in str(m.flag("new"))
    assert "cmp-flag gone" in str(m.flag("gone"))
    assert str(m.flag("both")) == "" and str(m.flag("")) == ""


def _account_rows(compare, has_data=True):
    tpl = web_app.templates.env.get_template("_account_map_country.html")
    row = {"account": "CR_1", "has_data": has_data, "spend": 120, "leads": 12, "deposits": 3,
           "status": {}, "note": ""}
    mc = {"country": "Costa Rica", "key": "cr", "rows": [row], "virtual": False}
    amap = {"live": {}, "fallback": {}, "splits": {}, "countries": [mc]}
    return tpl.render(mc=mc, amap=amap, compare=compare, mlx={})


def test_account_map_without_compare_has_no_compare_markup():
    html = _account_rows(None)
    assert "cmp-" not in html and 'class="cmp"' not in html


def test_account_map_shows_the_change_next_to_every_number():
    html = _account_rows(_cmp())
    assert html.count('class="cmp"') == 6          # spend, leads, FTDs, CPL, CPA, CR
    assert "+33%" in html                           # 120 против 90


def test_account_without_data_before_is_new():
    c = _cmp()
    tpl_html = _account_rows(pc.PeriodCompare(
        {"mode": "prev", "date_from": "a", "date_to": "a"}, {"countries": []},
        {"countries": [], "totals": None, "cm_section": []}))
    assert "cmp-flag new" in tpl_html
    assert "cmp-flag" not in _account_rows(c)       # есть в обоих — без метки


def test_account_that_stopped_having_data_is_gone():
    c = _cmp()
    html = _account_rows(c, has_data=False)
    assert "cmp-flag gone" in html


# ── сборка в app.py ────────────────────────────────────────────────────────
def test_build_compare_uses_the_report_and_map_of_the_earlier_period(monkeypatch):
    seen = {}

    def fake_period(base, df, a, b):
        seen["period"] = (a, b)
        return _prev_report()

    def fake_map(report, df, a, b, span):
        seen["map"] = (a, b, span)
        return _prev_amap()

    monkeypatch.setattr(web_app, "report_for_period", fake_period)
    monkeypatch.setattr(web_app, "_build_account_map_view", fake_map)
    monkeypatch.setattr(web_app, "_enrich_country_overview", lambda r: None)
    cmp_, note = web_app._build_compare({}, None, "2026-10-07", "2026-10-13", "", "prev", "", "")
    assert note == "" and cmp_.mode == "prev"
    assert seen["period"] == ("2026-09-30", "2026-10-06")
    assert seen["map"] == ("2026-09-30", "2026-10-06", "all")   # без чтений с аккаунтов


def test_build_compare_off_or_impossible_gives_no_compare():
    assert web_app._build_compare({}, None, "2026-10-07", "2026-10-13", "", "", "", "") == (None, "")
    cmp_, note = web_app._build_compare({}, None, "", "", "all", "prev", "", "")
    assert cmp_ is None and "date range" in note


def test_a_failing_earlier_period_keeps_the_page_and_says_so(monkeypatch):
    def boom(*a, **k):
        raise web_app.PeriodReportError("nope")

    monkeypatch.setattr(web_app, "report_for_period", boom)
    cmp_, note = web_app._build_compare({}, None, "2026-10-07", "2026-10-13", "", "prev", "", "")
    assert cmp_ is None and "earlier period" in note


def test_the_calendar_gets_only_validated_comparison_values(monkeypatch):
    """Сырое cmp_from из адреса не должно попасть в разметку календаря (XSS)."""
    evil = '"><img src=x onerror=alert(1)>'
    info, note = pc.resolve_compare("custom", evil, evil, "2026-10-07", "2026-10-13")
    assert info is None and note
    src = (Path(__file__).resolve().parent.parent / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert '"compare_pick": ({"mode": period_cmp.mode' in src
    assert "cmp_from.strip()," not in src.split('"compare_pick"')[1][:200]
