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
