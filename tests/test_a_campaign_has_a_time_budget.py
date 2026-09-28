# -*- coding: utf-8 -*-
"""Срок кампании, шага и зависшей вкладки (ACL-1354).

Живые прогоны 28.09: 37e397574548 (6540, прежний мастер) — вкладка раз за
разом не отвечала на проверку живости (`tab_frozen` на 5,8 / 6,2 / 9,2 /
12,8 / 25 / 26,7 мин кампании), `ad_creative_upload` 610 с, `ad_copy`
2281 с, `ad2_create` 578 с, кампания шла больше часа. a71e4fc14aa1 (1443,
новый редактор) — 119 мин. Обычная кампания 28.09 — 4–13 мин, самый долгий
шаг — 5,5 мин.

Теперь у записанного потока есть срок кампании, срок шага и предел ответов
«вкладка не отвечает»; выход по ним — остановка с названной причиной, без
повтора кампании (черновик частичный).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401
from tests.test_the_single_screen_editor_flow import sim  # noqa: E402,F401

STOPS = ("tab_frozen_budget_exceeded", "campaign_budget_exceeded", "step_budget_exceeded")


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(md, "time", SimpleNamespace(time=c.time, sleep=c.sleep, monotonic=c.time))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread=0.3: base)
    yield c
    md._flow_budget_disarm()


def test_a_tab_that_keeps_not_answering_stops_the_campaign(clock, monkeypatch):
    """37e397574548: третий «не отвечает» — на 9,2 мин; дальше шёл ещё час."""
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    md._flow_budget_arm()
    for minute in (5.8, 6.2):
        clock.now = 1000.0 + minute * 60
        assert md._tab_frozen(object()) is True
    clock.now = 1000.0 + 9.2 * 60
    with pytest.raises(md.FlowBudgetExceeded) as stop:
        md._tab_frozen(object())
    assert stop.value.reason == "tab_frozen_budget_exceeded"
    assert stop.value.detail["tab_frozen"] == 3


def test_one_long_stall_does_not_stop_the_campaign(clock, monkeypatch):
    """Несколько проверок подряд в одном застое лечит сторож — не остановка."""
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    md._flow_budget_arm()
    for _ in range(5):
        clock.now += 10
        assert md._tab_frozen(object()) is True


def test_a_step_longer_than_its_budget_stops_at_the_next_pause(clock):
    md._flow_budget_arm()
    md._flow_budget_step("ad_name")
    clock.now += md.STEP_BUDGET_S - 5
    md._sleep_h(1.0)
    clock.now += 10
    with pytest.raises(md.FlowBudgetExceeded) as stop:
        md._sleep_h(1.0)
    assert stop.value.reason == "step_budget_exceeded"
    assert stop.value.detail["after_step"] == "ad_name"


def test_the_creative_step_keeps_its_own_longer_budget(clock, monkeypatch):
    """Загрузка тяжёлого видео (до 20 мин) — под сроком шага креатива."""
    md._flow_budget_arm()
    monkeypatch.setattr(md._creative_tls, "deadline", clock.now + md.CREATIVE_STEP_BUDGET_S,
                        raising=False)
    clock.now += md.STEP_BUDGET_S + 60
    md._sleep_h(1.0)
    clock.now += md.CREATIVE_STEP_BUDGET_S
    with pytest.raises(md.FlowBudgetExceeded):
        md._sleep_h(1.0)


def test_the_campaign_has_a_budget_even_with_short_steps(clock):
    md._flow_budget_arm()
    with pytest.raises(md.FlowBudgetExceeded) as stop:
        for _ in range(1000):
            clock.now += 120
            md._flow_budget_step("step")
            md._sleep_h(0.5)
    assert stop.value.reason == "campaign_budget_exceeded"
    assert clock.now - 1000.0 <= md.CAMPAIGN_FLOW_BUDGET_S + 200


def test_outside_a_campaign_flow_nothing_is_stopped(clock, monkeypatch):
    md._flow_budget_disarm()
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    clock.now += 10 ** 6
    for _ in range(10):
        clock.now += 300
        md._tab_frozen(object())
        md._sleep_h(1.0)


def test_the_flow_stops_with_a_named_reason_and_goes_no_further(sim, monkeypatch):
    """Шаг назначения «висит» дольше срока шага — кампания остановлена,
    текст объявления уже не пишется."""
    clock = _Clock()
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep,
                                                    monotonic=clock.time))
    copies: list = []

    def slow_destination(p, url):
        clock.now += md.STEP_BUDGET_S + 30
        md._flow_budget_check()      # как в любом цикле ожидания (`_sleep_h`)
        return True

    monkeypatch.setattr(md, "set_ad_destination_website", slow_destination)
    monkeypatch.setattr(md, "write_ad_copy", lambda *a, **k: copies.append(1) or True)
    assert sim["run"]() == "step_budget_exceeded"
    assert copies == []
    assert any(r[0] == "step_budget_exceeded" and r[1] is False for r in sim["rows"])
    assert getattr(md._flow_budget, "armed", False) is False


@pytest.mark.parametrize("stop", STOPS)
def test_the_budget_stops_block_and_are_not_replayed(stop):
    assert failure_policy.severity(stop) == failure_policy.BLOCKING
    assert "time limit" in failure_policy.reason(stop) or "not answering" in failure_policy.reason(stop)
    assert stop in retry_policy.FLOW_BUDGET_STOPS
    assert stop not in retry_policy.CAMPAIGN_RETRY_STEPS
    assert retry_policy.disposition(stop).action == retry_policy.BLOCK_AND_REVIEW
    assert stop in retry_policy.BLOCKING_REVIEW_STEPS
    assert not retry_policy.should_retry_campaign(stop, completed_attempts=0)
