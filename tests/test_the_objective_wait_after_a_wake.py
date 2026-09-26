# -*- coding: utf-8 -*-
"""Окно цели после пробуждения вкладки и на медленной странице (ACL-1320).

Прогон cf563b4ba594 (26.09, Doc19): сторож разбудил вкладку на 100-й
секунде ожидания окна цели, и ожидание тут же сдалось по `first_s` —
разбуженной странице не дали ни секунды. Опрос, который висел десятки
секунд на медленной вкладке, считался «пустым экраном» и приближал отказ
по `idle_s`, хотя страница была занята.

Теперь: после пробуждения — `OBJECTIVE_AFTER_WAKE_S` свежего ожидания;
медленный опрос (> `OBJECTIVE_SLOW_POLL_FACTOR` × шаг) — «занята»;
вкладка, дважды не ответившая на проверку живости, — `unresponsive`, решать
вызывающему; `loading_max_s` — потолок и без загрузки. Поддельные часы.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class _Radio:
    def __init__(self, clock, appears_at, poll_cost=None):
        self.clock, self.appears_at, self.poll_cost = clock, appears_at, poll_cost

    def wait_for(self, state="visible", timeout=30000):
        cost = self.poll_cost if self.poll_cost is not None else timeout / 1000
        deadline = self.clock.now + cost
        if self.appears_at is not None and self.appears_at <= deadline:
            self.clock.now = max(self.clock.now, self.appears_at)
            return None
        self.clock.now = deadline
        raise TimeoutError(f"Timeout {timeout}ms exceeded")


class _Page:
    url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"

    def __init__(self, radio):
        self.radio = radio

    def get_by_role(self, role, name=None, **kw):
        return SimpleNamespace(first=self.radio)


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    rows: list[dict] = []
    woke: list[str] = []
    state = {"frozen": False}
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append({"step": step, "ok": ok, **kw}))
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep))
    monkeypatch.setattr(md, "_loading_visible", lambda p: False)
    monkeypatch.setattr(md, "_page_responsive",
                        lambda p, timeout_s=5.0: (clock.sleep(timeout_s) or False) if state["frozen"] else True)
    monkeypatch.setattr(md, "_unstick", lambda p, where, **kw: woke.append(where) or "woke")
    return SimpleNamespace(clock=clock, rows=rows, woke=woke, state=state, start=clock.now)


def test_a_woken_tab_gets_fresh_waiting(env):
    """Сторож будит на 60-й секунде; окно приходит на 75-й — его дожидаемся,
    хотя `first_s` (61 с) к тому времени уже вышел."""
    radio = _Radio(env.clock, appears_at=env.start + 75)
    assert md._wait_objective_dialog(_Page(radio), first_s=61, idle_s=1000) is True
    assert env.woke == ["objective_wait"]
    assert md.OBJECTIVE_AFTER_WAKE_S >= 15


def test_the_fresh_wait_after_a_wake_is_bounded(env):
    radio = _Radio(env.clock, appears_at=None)
    assert md._wait_objective_dialog(_Page(radio), first_s=61, idle_s=1000) is False
    spent = env.clock.now - env.start
    assert spent <= md.STALL_BUDGET_S + md.OBJECTIVE_AFTER_WAKE_S + 2 * md.OBJECTIVE_POLL_S + 0.01, spent


def test_a_slow_poll_is_a_busy_page_not_an_idle_one(env):
    """Каждый опрос висит 10 с (> 3 × 2.5 с): страница занята — ждём дальше."""
    radio = _Radio(env.clock, appears_at=env.start + 50, poll_cost=10.0)
    out: dict = {}
    assert md._wait_objective_dialog(_Page(radio), idle_s=20, out=out) is True
    assert out["outcome"] == "radio"


def test_a_tab_that_does_not_answer_is_handed_back(env):
    """Вкладка не отвечает: сторож сразу, потом `unresponsive` — не `first_s`."""
    env.state["frozen"] = True
    out: dict = {}
    assert md._wait_objective_dialog(_Page(_Radio(env.clock, appears_at=None)), out=out) is False
    assert out["outcome"] == "unresponsive" and out["frozen"] == md.OBJECTIVE_FROZEN_POLLS
    assert env.woke == ["objective_wait"], "сторож не сработал сразу"
    assert env.clock.now - env.start < 20
    assert env.rows[-1]["step"] == "objective_wait" and env.rows[-1]["note"] == "unresponsive"


def test_the_ceiling_holds_without_a_loading_modal(env):
    """`loading_max_s` — бюджет вызывающего: без загрузки он тоже потолок."""
    radio = _Radio(env.clock, appears_at=None)
    out: dict = {}
    assert md._wait_objective_dialog(_Page(radio), first_s=75, idle_s=1000,
                                     loading_max_s=30, out=out) is False
    assert out["outcome"] == "budget"
    assert env.clock.now - env.start <= 30 + md.OBJECTIVE_POLL_S + 0.01


def test_a_slow_load_then_a_short_blank_gap_is_not_cut_by_the_round(env, monkeypatch):
    """Ревью PR #323 (F1): «Loading creation» 0–50 с, окно цели на 56-й с.
    Раунд `_open_campaign` передаёт first_s≈37 — после загрузки его уже нет:
    ждём пустой экран до `idle_s`, окно приходит."""
    monkeypatch.setattr(md, "_loading_visible", lambda p: env.clock.now < env.start + 50)
    monkeypatch.setattr(md, "_loading_kind", lambda p: "text:Loading creation")
    monkeypatch.setattr(md, "_network_trouble", lambda p: False)
    monkeypatch.setattr(md, "_stall_evidence", lambda *a, **k: {})
    radio = _Radio(env.clock, appears_at=env.start + 56)
    out: dict = {}
    assert md._wait_objective_dialog(_Page(radio), first_s=37, loading_max_s=112, out=out) is True, out


def test_without_any_loading_the_round_still_ends_at_first_s(env, monkeypatch):
    monkeypatch.setattr(md, "_stall_evidence", lambda *a, **k: {})
    radio = _Radio(env.clock, appears_at=None)
    out: dict = {}
    assert md._wait_objective_dialog(_Page(radio), first_s=15, idle_s=30, loading_max_s=112, out=out) is False
    assert out["outcome"] == "first_s"


def test_a_busy_tab_that_answers_between_long_tasks_is_not_unresponsive(env, monkeypatch):
    """Ревью PR #323 (F8): «не ответила» — только подряд. Вкладка молчит
    через раз (долгие задачи гидратации) — окно цели дожидаемся."""
    calls = {"n": 0}

    def responsive(p, timeout_s=5.0):
        calls["n"] += 1
        if calls["n"] % 2:
            env.clock.sleep(timeout_s)
            return False
        return True

    monkeypatch.setattr(md, "_page_responsive", responsive)
    radio = _Radio(env.clock, appears_at=env.start + 40)
    out: dict = {}
    assert md._wait_objective_dialog(_Page(radio), first_s=75, idle_s=60, out=out) is True, out
    assert out["frozen"] >= md.OBJECTIVE_FROZEN_POLLS


def test_the_liveness_probe_gives_a_busy_tab_five_seconds():
    assert md.LIVENESS_PROBE_S == 5.0
