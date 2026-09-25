# -*- coding: utf-8 -*-
"""Отсутствующий элемент не стоит фиксированного ожидания (ACL-1313, 25.09).

На каждой кампании ждали то, чего почти никогда нет:
* «Manual campaign setup» — `click(timeout=3000)`, 3 с;
* заголовок «Leads» — `click(timeout=5000)`, 5 с;
* онбординг-модалка — 4 с «появись, если ты есть», хотя поле имени уже видно;
* тост «You've switched audience setup» — 3 с ожидания появления.

Поток гоняется целиком на подставной странице с поддельными часами
(фикстура `flow` из test_create_quick_retry_returns_to_list.py): отсутствующий
элемент там тратит свой таймаут по часам, как Playwright. Длительности шагов
берутся из телеметрии самого потока.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import (  # noqa: E402,F401
    _REAL_FIRST_VISIBLE, flow)

_ABSENT_TEXT = ("Manual campaign setup", "You've switched audience setup")
_ABSENT_ROLE = {("heading", "Leads"), ("button", "Don't show again"), ("button", "OK")}


class _Absent:
    """Элемента нет: любое ожидание или клик тратят таймаут по часам."""

    def __init__(self, clock):
        self.clock = clock

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self, timeout=0):
        self.clock["now"] += (timeout or 0) / 1000
        return False

    def _spend(self, timeout):
        self.clock["now"] += timeout / 1000
        raise TimeoutError("absent")

    def click(self, timeout=30000, **kw):
        self._spend(timeout)

    def wait_for(self, state="visible", timeout=30000):
        self._spend(timeout)


def _text_of(arg):
    return arg.pattern if isinstance(arg, re.Pattern) else str(arg)


@pytest.fixture
def run(flow, monkeypatch, tmp_path):
    clock = {"now": 1000.0}
    monkeypatch.setattr(dom, "time", SimpleNamespace(
        time=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + s)))
    monkeypatch.setattr(dom, "_first_visible", _REAL_FIRST_VISIBLE)
    page = flow["page"]
    absent = _Absent(clock)
    present_text = page.get_by_text.return_value
    by_role = page.get_by_role.side_effect

    def get_by_text(arg, **kw):
        if any(t.lower() in _text_of(arg).lower() for t in _ABSENT_TEXT):
            return absent
        return present_text

    def get_by_role(role, name=None, **kw):
        if (role, name if isinstance(name, str) else None) in _ABSENT_ROLE:
            return absent
        return by_role(role, name, **kw)

    page.get_by_text.side_effect = get_by_text
    page.get_by_role.side_effect = get_by_role

    def go():
        flow["run"]()
        rows = [json.loads(line) for line in
                rt.TELEMETRY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        return {r["step"]: r.get("duration_s") for r in rows if r.get("duration_s") is not None}

    return go


def test_absent_manual_setup_and_leads_heading_cost_nothing(run):
    steps = run()
    assert steps["leads"] < 1.0, steps


def test_no_onboarding_modal_costs_nothing_when_the_name_field_is_there(run):
    steps = run()
    assert steps["onboarding_modal"] < 1.0, steps


def test_an_absent_audience_switch_toast_costs_nothing(run):
    steps = run()
    assert steps["further_limit_continue"] < 1.0, steps


def test_parking_the_tab_after_the_batch_does_not_sleep():
    """Конец задания: уйти из редактора на список — и всё, без 3 с простоя."""
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    page = MagicMock()
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit?act=123&selected=1"
    RpaPublisher._goto_campaigns_list(page)
    page.goto.assert_called_once()
    assert "campaigns?act=123" in page.goto.call_args.args[0]
    page.wait_for_timeout.assert_not_called()
