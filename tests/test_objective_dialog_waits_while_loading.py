# -*- coding: utf-8 -*-
"""Окно выбора цели: пока Meta крутит «Loading creation», ждём, а не перезагружаем.

ACL-1313, прогон 24.09 (три задания параллельно): после Create модалка
«Loading creation» висела минутами, удачные переходы занимали 55–79 с — ровно
около прежнего порога 75 с. Перезагрузка поверх крутящейся модалки
выбрасывала начатую загрузку, и все пять отказов `objective_leads` того
вечера — цепочка «75 с → reload → Create → 75 с → reload». А повтор кампании
через 1,5 с попадал в ту же нагрузку: CL FX сделала 9 Create за 23 минуты.

Проверяется на подставной странице с поддельными часами:
`_wait_objective_dialog` ждёт, пока видна загрузка, и сдаётся по потолку;
без загрузки — одно ожидание и отказ; `retry_policy.backoff_s` даёт 90 с на
попытку для медленных шагов; `AdsManagerFlow` спит именно столько.
"""
from __future__ import annotations

import ast
import sys
import time as _real_time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import retry_policy  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class _Radio:
    """`get_by_role('radio', name='Leads').first`: появляется в момент `appears_at`."""

    def __init__(self, clock, appears_at):
        self.clock = clock
        self.appears_at = appears_at
        self.waits: list[int] = []

    def wait_for(self, state="visible", timeout=30000):
        self.waits.append(timeout)
        deadline = self.clock.now + timeout / 1000
        if self.appears_at is not None and self.appears_at <= deadline:
            self.clock.now = max(self.clock.now, self.appears_at)
            return None
        self.clock.now = deadline
        raise TimeoutError(f"Timeout {timeout}ms exceeded")


class _Page:
    def __init__(self, radio):
        self.radio = radio
        self.roles: list[tuple] = []

    def get_by_role(self, role, name=None, **kw):
        self.roles.append((role, name))
        return SimpleNamespace(first=self.radio)


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep))
    state = {"loading_until": None, "calls": 0}

    def loading(page):
        state["calls"] += 1
        return state["loading_until"] is not None and clock.now < state["loading_until"]

    monkeypatch.setattr(md, "_loading_visible", loading)
    return SimpleNamespace(clock=clock, state=state, start=clock.now)


def test_the_leads_radio_arriving_while_loading_is_waited_for(env):
    """Живой хвост 24.09: модалка крутится дольше 75 с, окно цели приходит на 120-й."""
    env.state["loading_until"] = env.start + 500
    radio = _Radio(env.clock, appears_at=env.start + 120)
    page = _Page(radio)
    assert md._wait_objective_dialog(page) is True
    assert page.roles[0] == ("radio", "Leads")
    assert radio.waits[0] == 75000, "первое ожидание — прежние 75 с"
    assert len(radio.waits) > 1, "после 75 с ожидание продолжилось"
    assert env.clock.now - env.start == pytest.approx(120, abs=0.01)


def test_without_a_loading_modal_it_gives_up_after_one_wait(env):
    """Модалки нет — ждать нечего: один отказ и дальше решает вызывающий."""
    radio = _Radio(env.clock, appears_at=None)
    assert md._wait_objective_dialog(_Page(radio)) is False
    assert radio.waits == [75000]
    assert env.clock.now - env.start == pytest.approx(75, abs=0.01)


def test_an_endless_loading_modal_stops_at_the_ceiling(env):
    env.state["loading_until"] = env.start + 10_000
    radio = _Radio(env.clock, appears_at=None)
    assert md._wait_objective_dialog(_Page(radio), first_s=75, loading_max_s=180) is False
    spent = env.clock.now - env.start
    assert 180 <= spent <= 180 + 5 + 0.01, spent
    assert md.OBJECTIVE_LOADING_MAX_S == 180.0


def test_a_loading_modal_that_clears_without_the_dialog_ends_the_wait(env):
    """Загрузка кончилась, а окна цели нет — это уже не ожидание, а отказ."""
    env.state["loading_until"] = env.start + 90
    radio = _Radio(env.clock, appears_at=None)
    assert md._wait_objective_dialog(_Page(radio)) is False
    assert env.clock.now - env.start < 100


def test_the_quick_path_is_unchanged(env):
    radio = _Radio(env.clock, appears_at=env.start + 19)
    assert md._wait_objective_dialog(_Page(radio)) is True
    assert radio.waits == [75000]


def test_the_recorded_flow_waits_through_the_helper_before_reloading():
    """`run_recorded_campaign_flow` ждёт окно цели через `_wait_objective_dialog`,
    и только потом — `recover_stuck_page`."""
    tree = ast.parse((ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_recorded_campaign_flow")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    wait_lines = [c.lineno for c in calls if c.func.id == "_wait_objective_dialog"]
    recover_lines = [c.lineno for c in calls if c.func.id == "recover_stuck_page"
                     and c.args[1:2] and isinstance(c.args[1], ast.Constant) and c.args[1].value == "objective"]
    assert wait_lines, "окно цели ждут не через _wait_objective_dialog"
    assert recover_lines and min(wait_lines) < min(recover_lines)


# ── пауза перед новой попыткой кампании ───────────────────────────────────

@pytest.mark.parametrize("step,attempt,expected", [
    ("create", 1, 90.0),
    ("create", 2, 180.0),
    ("objective_leads", 1, 90.0),
    ("objective_leads", 2, 180.0),
    ("objective_leads:detail", 1, 90.0),
    (" create :timeout", 1, 90.0),
    ("create", 0, 90.0),
    ("campaign_name", 1, 1.5),
    ("campaign_name", 2, 1.5),
    ("objective_continue", 1, 1.5),
    ("", 1, 1.5),
])
def test_backoff_is_long_only_where_meta_is_slow(step, attempt, expected):
    assert retry_policy.backoff_s(step, attempt) == expected


def _publish_with_failures(monkeypatch, fails: list[str]):
    """Прогнать `publish_campaign` по записанному пути с поддельным потоком."""
    from src.fb_ads_rpa import ads_manager_flow as amf
    from src.fb_ads_rpa import browser as br
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    monkeypatch.setenv("FB_RPA_ENGINE", "recorded_flow")
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, draft, result: None)
    monkeypatch.setattr(br, "put_window_back", lambda page: None)
    queue = list(fails)
    monkeypatch.setattr(amf, "run_recorded_campaign_flow", lambda page, **kw: queue.pop(0) if queue else "")
    sleeps: list[float] = []

    class _Time:
        """Модуль time для ads_manager_flow: всё настоящее, кроме sleep."""

        def __getattr__(self, name):
            return getattr(_real_time, name)

        @staticmethod
        def sleep(s):
            sleeps.append(s)

    monkeypatch.setattr(amf, "time", _Time())

    flow = amf.AdsManagerFlow(object())
    draft = CampaignDraft("Campaign A", "MX", "Account",
                          ad_sets=[AdSetDraft("Set A", ads=[AdDraft(ad_title="t", ad_body="b")])])
    result = flow.publish_campaign(draft)
    return result, sleeps


def test_the_campaign_retry_after_a_slow_meta_step_waits_minutes(monkeypatch):
    result, sleeps = _publish_with_failures(monkeypatch, ["objective_leads", "create"])
    assert sleeps == [90.0, 180.0]
    assert result.success is True
    assert "recorded_flow_retry_scheduled:objective_leads:attempt2" in result.steps_completed


def test_the_campaign_retry_after_an_ordinary_step_stays_quick(monkeypatch):
    result, sleeps = _publish_with_failures(monkeypatch, ["campaign_name"])
    assert sleeps == [1.5]
    assert result.success is True
