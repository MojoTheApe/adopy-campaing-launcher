# -*- coding: utf-8 -*-
"""Активность книги считается один раз на ревизию книги (финальное ревью
PR #314 по ACL-1319).

`workbook_activity` пересчитывалась на каждой отрисовке Home: разбор дат и
группировка всей книги. Теперь ответ кэшируется на тот же DataFrame (кэш
отчёта держит один объект, пока не сменится файл или не пересоберётся отчёт);
множество имён книги строится один раз, а не на каждый аккаунт списка;
сообщение о размещённых аккаунтах согласовано по числу.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import available_accounts as av  # noqa: E402


def _book():
    return pd.DataFrame({
        "Account Alias": ["FacebookDoc19 SA", "FacebookDQ501 MX", "FacebookOld CL"],
        "$ Spend": [10.0, 5.0, 3.0],
        "Date": ["2026-09-20", "2026-09-25", "2026-06-01"],
    })


@pytest.fixture
def counted(monkeypatch):
    monkeypatch.setattr(av, "_ACTIVITY_CACHE", {"ref": None, "rows": None, "days": None, "value": None})
    calls: list = []
    real = av._workbook_activity
    monkeypatch.setattr(av, "_workbook_activity", lambda df, days: calls.append(days) or real(df, days))
    return calls


def test_the_same_workbook_is_computed_once(counted):
    df = _book()
    first = av.workbook_activity(df)
    assert av.workbook_activity(df) is first
    assert counted == [av.WINDOW_DAYS]
    assert "facebookdoc19" in first.recent and "facebookold" not in first.recent


def test_a_new_workbook_or_window_is_computed_again(counted):
    df = _book()
    av.workbook_activity(df)
    av.workbook_activity(df.copy())
    av.workbook_activity(df, days=7)
    assert len(counted) == 3


def test_no_workbook_is_not_cached(counted):
    assert av.workbook_activity(None) is None
    assert counted == []


class _CountingList(list):
    iterations = 0

    def __iter__(self):
        type(self).iterations += 1
        return super().__iter__()


def test_the_name_set_is_built_once_not_per_account():
    names = _CountingList(["facebookdoc19", "facebookmktobflx84"])
    activity = av.Activity(window_from="2026-08-27", window_to="2026-09-25", names=names)
    _CountingList.iterations = 0
    for _ in range(50):
        assert av.workbook_key("FacebookMKTOBFLX84 - Someone", activity) == "facebookmktobflx84"
    assert _CountingList.iterations == 0, "множество имён строится на каждый аккаунт"


def _placed_msg_js() -> str:
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    m = re.search(r"function amapPlacedMsg\(names\) \{.*?\n\}", html, re.S)
    assert m, "amapPlacedMsg не найдена"
    return m.group(0)


@pytest.mark.parametrize("names, expected", [
    (["FacebookDoc19"], "FacebookDoc19 is placed in a country — take it out of the country first."),
    (["FacebookDoc19", "FacebookDQ501"],
     "FacebookDoc19, FacebookDQ501 are placed in countries — take them out of their countries first."),
])
def test_the_placed_message_agrees_in_number(names, expected):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            p = b.new_page()
            got = p.evaluate(f"(names) => {{ {_placed_msg_js()}; return amapPlacedMsg(names); }}", names)
        finally:
            b.close()
    assert got == expected
