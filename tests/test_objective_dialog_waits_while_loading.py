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
без загрузки — не дольше `OBJECTIVE_IDLE_S` подряд (ACL-1313, 25.09: было
75 с); `retry_policy.backoff_s` даёт 25 с на попытку для медленных шагов
(было 90); `AdsManagerFlow` спит именно столько.
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
    rows: list[dict] = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append({"step": step, "ok": ok, **kw}))
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep))
    state = {"loading_until": None, "calls": 0}

    def loading(page):
        state["calls"] += 1
        return state["loading_until"] is not None and clock.now < state["loading_until"]

    monkeypatch.setattr(md, "_loading_visible", loading)
    return SimpleNamespace(clock=clock, state=state, start=clock.now, rows=rows)


def test_the_leads_radio_arriving_while_loading_is_waited_for(env):
    """Живой хвост 24.09: модалка крутится дольше 75 с, окно цели приходит на 120-й."""
    env.state["loading_until"] = env.start + 500
    radio = _Radio(env.clock, appears_at=env.start + 120)
    page = _Page(radio)
    assert md._wait_objective_dialog(page) is True
    assert page.roles[0] == ("radio", "Leads")
    assert env.clock.now - env.start == pytest.approx(120, abs=0.01)
    assert env.rows[-1]["step"] == "objective_wait" and env.rows[-1]["note"] == "radio"


def test_a_short_gap_after_loading_is_waited_for(env):
    """Загрузка кончилась, окно отрисовывается через 8 с пустого экрана."""
    env.state["loading_until"] = env.start + 40
    radio = _Radio(env.clock, appears_at=env.start + 48)
    assert md._wait_objective_dialog(_Page(radio)) is True


def test_without_a_loading_modal_it_gives_up_after_the_idle_bound(env):
    """Ни загрузки, ни окна — ждать нечего: отказ через OBJECTIVE_IDLE_S, не 75 с."""
    radio = _Radio(env.clock, appears_at=None)
    assert md._wait_objective_dialog(_Page(radio)) is False
    spent = env.clock.now - env.start
    assert md.OBJECTIVE_IDLE_S <= spent <= md.OBJECTIVE_IDLE_S + md.OBJECTIVE_POLL_S + 0.01, spent
    assert spent < 30
    assert env.rows[-1]["note"] == "idle"


def test_the_idle_bound_counts_from_the_last_loading(env):
    """Загрузка была 60 с: пустой экран после неё отсчитывается заново."""
    env.state["loading_until"] = env.start + 60
    radio = _Radio(env.clock, appears_at=None)
    assert md._wait_objective_dialog(_Page(radio), first_s=200) is False
    spent = env.clock.now - env.start
    assert 60 - md.OBJECTIVE_POLL_S + md.OBJECTIVE_IDLE_S <= spent <= 60 + md.OBJECTIVE_IDLE_S + md.OBJECTIVE_POLL_S + 0.01


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
    # Ревью PR #323 (F1): после конца загрузки — ещё `OBJECTIVE_IDLE_S`
    # пустого экрана (нормальная фаза перед окном цели), не больше.
    assert env.clock.now - env.start < 90 + md.OBJECTIVE_IDLE_S + 2 * md.OBJECTIVE_POLL_S


def test_the_quick_path_is_unchanged(env):
    """Медиана перехода — 19 с; даже без видимой загрузки он укладывается."""
    radio = _Radio(env.clock, appears_at=env.start + 19)
    assert md._wait_objective_dialog(_Page(radio)) is True
    assert env.clock.now - env.start == pytest.approx(19, abs=0.01)


def test_the_recorded_flow_waits_through_the_helper_before_reloading():
    """`run_recorded_campaign_flow` открывает кампанию через `_open_campaign`
    (ACL-1320), а тот ждёт окно цели через `_wait_objective_dialog` — и только
    потом идёт на список заново (`_open_list_again`)."""
    tree = ast.parse((ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8"))

    def fn(name):
        return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)

    def calls(node, name):
        return [c.lineno for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name]

    assert calls(fn("run_recorded_campaign_flow"), "_open_campaign"), "кампанию открывают мимо бюджета"
    # Ревью PR #323 (F9): `_open_campaign` ставит срок потока, раунды — в
    # `_open_campaign_rounds`.
    assert calls(fn("_open_campaign"), "_open_campaign_rounds")
    opener = fn("_open_campaign_rounds")
    assert calls(opener, "_wait_objective_dialog"), "окно цели ждут не через _wait_objective_dialog"
    assert calls(opener, "_open_list_again")
    assert not calls(opener, "recover_stuck_page"), "раунд без окна цели — список заново, не reload"


# ── пауза перед новой попыткой кампании ───────────────────────────────────

@pytest.mark.parametrize("step,attempt,expected", [
    ("create", 1, 25.0),
    ("create", 2, 50.0),
    ("objective_leads", 1, 25.0),
    ("objective_leads", 2, 50.0),
    ("objective_leads:detail", 1, 25.0),
    (" create :timeout", 1, 25.0),
    ("create", 0, 25.0),
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


def test_the_campaign_retry_after_a_slow_meta_step_waits_longer(monkeypatch):
    result, sleeps = _publish_with_failures(monkeypatch, ["objective_leads", "create"])
    assert sleeps == [25.0, 50.0]
    assert result.success is True
    assert "recorded_flow_retry_scheduled:objective_leads:attempt2" in result.steps_completed


def test_the_campaign_retry_after_an_ordinary_step_stays_quick(monkeypatch):
    result, sleeps = _publish_with_failures(monkeypatch, ["campaign_name"])
    assert sleeps == [1.5]
    assert result.success is True


def test_objective_wait_says_what_was_loading(env, monkeypatch):
    """25.09: потолок 181 с по строке objective_wait было не разобрать —
    текстовая накладка это или спиннер маршрута."""
    kinds = iter(["text:Loading creation…"] * 3 + ["route_placeholder"] * 100)
    monkeypatch.setattr(md, "_loading_kind", lambda page: next(kinds))
    env.state["loading_until"] = env.start + 60
    radio = _Radio(env.clock, appears_at=env.start + 60)
    assert md._wait_objective_dialog(_Page(radio)) is True
    diag = env.rows[-1]["diag"]
    assert diag["loading_kinds"]["text:Loading creation…"] == 3
    assert diag["loading_kinds"]["route_placeholder"] == diag["loading_polls"] - 3
