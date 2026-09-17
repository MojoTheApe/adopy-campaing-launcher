# -*- coding: utf-8 -*-
"""Период «16 Sep 2026» (сначала день) читается так же, как «Sep 16, 2026» (ACL-1223).

17.09 Doc20: кнопка периода показывала «16 Sep 2026», Amount spent $55.81 был на
экране, а чтение отказало «на экране период не найден».
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402


def test_day_first_and_month_first_give_the_same_dates():
    assert sp._dates_in("16 Sep 2026") == ["2026-09-16"]
    assert sp._dates_in("16 Sept 2026") == ["2026-09-16"]
    assert sp._dates_in("Sep 16, 2026") == ["2026-09-16"]
    assert sp._dates_in("1 Sep 2026 – 16 Sep 2026") == ["2026-09-01", "2026-09-16"]


def test_the_period_finder_pattern_sees_both_orders():
    assert re.search(sp.RE_PERIOD_DATE, "16 Sep 2026", re.I)
    assert re.search(sp.RE_PERIOD_DATE, "Sep 16, 2026", re.I)


def test_a_different_day_is_still_refused():
    assert sp._period_matches("16 Sep 2026", "2026-09-16") is True
    assert sp._period_matches("16 Sep 2026", "2026-09-15") is False
    assert sp._period_matches("1 Sep 2026 – 5 Sep 2026", "2026-09-01", "2026-09-06") is False


def test_total_spent_with_a_capital_s_is_read():
    """Doc20 17.09: итоговая строка «$55.81 Total Spent» — британская локаль."""
    import os
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        p = b.new_page()
        p.set_content(
            "<div><span>Results from 4 campaigns</span><div><span>$55.81</span><span>Total Spent</span></div></div>"
        )
        got = p.evaluate(sp._FIND_TOTALS_JS)
        b.close()
    assert got["spend"] == "$55.81" and got["label"] is True


def test_a_busy_table_is_waited_for(monkeypatch):
    """17.09: дерево падало на table-loading сразу после раскрытия итогов."""
    answers = [
        {"rows": [], "how": "table-loading", "kinds": [], "error": "table-loading"},
        {"rows": [], "how": "table-loading", "kinds": [], "error": "table-loading"},
        {"rows": [], "how": "no-row-surfaces", "kinds": []},
    ]
    calls = []

    class P:
        def evaluate(self, js, level):
            calls.append(level)
            return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(sp, "_sleep", lambda s: None)
    rows, how, err = sp.read_level(P(), "campaign")
    assert len(calls) == 3 and "table-loading" not in (err or "")


def test_rows_marked_on_display_contents_wrappers_are_read():
    """17.09: Meta вешает data-surface строк на <span style="display:contents"> —
    без собственных прямоугольников; дерево читало ноль строк из 88."""
    import os
    from playwright.sync_api import sync_playwright

    row = "120249333114990244"
    html = (
        '<div role="table"><div role="presentation">'
        f'<span style="display:contents" data-surface="/am/table/table_row:{row}unit/table_cell:forObjectType(name,CAMPAIGN_GROUP)">'
        '<div>KW_trl_All_Video_Day_35_16.09</div></span>'
        f'<span style="display:contents" data-surface="/am/table/table_row:{row}unit/table_cell:spend">'
        '<div>$55.81</div></span>'
        "</div></div>"
    )
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        p = b.new_page()
        p.set_content(html)
        rows, how, err = sp.read_level(p, "campaign")
        b.close()
    assert not err, err
    assert len(rows) == 1 and rows[0].name == "KW_trl_All_Video_Day_35_16.09" and rows[0].spend == "$55.81"
