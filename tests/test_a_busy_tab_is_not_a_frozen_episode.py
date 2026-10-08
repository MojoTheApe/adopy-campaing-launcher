# -*- coding: utf-8 -*-
"""ACL-1739: занятая, но живая вкладка не копит эпизоды застоя (Doc124).

Прогоны CO_bnc (df8549045b4a, e3f55897e533 и др., 07-08.10): окно профиля под
4-7 другими окнами, 5-секундная проба не успевала, а через минуту
stall_evidence показывал `responsive: true`. Пять таких проб останавливали
кампанию (`tab_frozen_budget_exceeded`). Здесь только заглушки страницы.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


class _Page:
    """Короткая проба не отвечает; длинная — по сценарию."""

    url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"

    def __init__(self, long_answers: bool):
        self.long_answers = long_answers
        self.timeouts: list[int] = []

    def wait_for_function(self, js, arg=None, timeout=None):
        self.timeouts.append(timeout)
        if timeout >= int(md.FROZEN_CONFIRM_S * 1000) and self.long_answers:
            return object()
        raise TimeoutError(f"Timeout {timeout}ms exceeded.")


def _arm(monkeypatch):
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    md._flow_budget_arm()


def test_a_busy_tab_counts_no_episode(monkeypatch):
    _arm(monkeypatch)
    page = _Page(long_answers=True)
    for _ in range(md.TAB_FROZEN_STOP_EPISODES + 2):
        assert md._tab_frozen(page) is False
        md._flow_budget.frozen_last = None  # каждая проба — «новый эпизод»
    assert md._flow_budget.frozen == []
    md._flow_budget_check()  # не бросает
    md._flow_budget_disarm()


def test_a_really_frozen_tab_still_counts(monkeypatch):
    _arm(monkeypatch)
    md._flow_budget.defer = True
    page = _Page(long_answers=False)
    assert md._tab_frozen(page) is True
    assert len(md._flow_budget.frozen) == 1
    md._flow_budget.defer = False
    md._flow_budget_disarm()


def test_the_budget_state_is_per_thread(monkeypatch):
    _arm(monkeypatch)
    seen = {}

    def other():
        seen["armed"] = getattr(md._flow_budget, "armed", False)

    t = threading.Thread(target=other)
    t.start(); t.join()
    assert seen["armed"] is False
    md._flow_budget_disarm()
