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


#: Секунды после открытия кампании, на которых вкладка не ответила
#: (строки `tab_frozen`, `responsive:false`, `tab_frozen:true` телеметрии).
HEALTHY_7CFDC = (244, 252, 381, 402)          # 26.09, кампания дошла до конца за 16,4 мин
FROZEN_37E3 = (334, 358, 539, 755, 931, 1486, 1588, 3079, 3772, 3805,
               4293, 4399, 4461, 4657, 5751, 6011)   # 28.09, 6540, больше часа


def _replay(clock, monkeypatch, moments, *, pad_s=0.0):
    """Прогнать моменты «вкладка не ответила» через `_tab_frozen`.

    `pad_s` — ещё по пробе каждые 5 с в пределах `pad_s` после каждой: в
    телеметрию попадает только часть проб, живьём их больше.
    """
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    md._flow_budget_arm()
    start = clock.now
    probes = sorted({m + k * 5.0 for m in moments for k in range(int(pad_s / 5) + 1)})
    for t in probes:
        clock.now = start + t
        md._flow_budget_step("step")     # шаги идут: проверяется только застой
        md._tab_frozen(object())


def test_a_healthy_campaign_with_two_short_stalls_is_not_stopped(clock, monkeypatch):
    """Ревью PR #368 (B1): 7cfdc772376d остановился бы на 402-й секунде."""
    _replay(clock, monkeypatch, HEALTHY_7CFDC)
    _replay(clock, monkeypatch, HEALTHY_7CFDC, pad_s=60)


def test_a_tab_that_keeps_not_answering_stops_the_campaign(clock, monkeypatch):
    """37e397574548: пятый эпизод застоя — на 1486-й секунде (24,8 мин);
    прежде кампания шла больше часа."""
    with pytest.raises(md.FlowBudgetExceeded) as stop:
        _replay(clock, monkeypatch, FROZEN_37E3)
    assert stop.value.reason == "tab_frozen_budget_exceeded"
    assert stop.value.detail["frozen_episodes"] == md.TAB_FROZEN_STOP_EPISODES
    assert stop.value.detail["campaign_s"] == 1486
    assert stop.value.detail["episodes_at_s"] == [334, 539, 755, 931, 1486]


def test_one_long_stall_is_one_episode(clock, monkeypatch):
    """Проба каждые 10 с 15 минут подряд — один застой; его лечит сторож."""
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    md._flow_budget_arm()
    for _ in range(90):
        clock.now += 10
        md._flow_budget_step("step")
        assert md._tab_frozen(object()) is True
    assert len(md._flow_budget.frozen) == 1


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


def test_the_step_after_a_long_creative_starts_its_own_clock(clock, monkeypatch):
    """Ревью PR #368: креатив шёл 15 мин под своим сроком — первая же проба
    после него не должна давать `step_budget_exceeded`."""
    md._flow_budget_arm()

    def creative(*a, **k):
        clock.now += 15 * 60
        return False

    monkeypatch.setattr(md, "_upload_ad_creative_with_retry", creative)
    assert md.upload_ad_creative_with_retry(object(), "Video", "a.mp4") is False
    md._sleep_h(1.0)      # не бросает: отсчёт шага — с конца креатива
    assert md._flow_budget.step == "ad_creative"


def test_the_table_modal_does_not_hide_a_create_modal_beside_it(monkeypatch):
    """Ревью PR #368: обе модалки на экране — нажатие Create взялось."""
    import os

    from playwright.sync_api import sync_playwright

    from tests.test_single_screen_editor_failures_are_named import OBJ, _wrap

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            page = b.new_page()
            page.set_content(_wrap(OBJ["table_loading"] + OBJ["create_loading"]))
            assert md._table_loading(page) is False
            page.set_content(_wrap(OBJ["table_loading"]))
            assert md._table_loading(page) is True
        finally:
            b.close()


def test_inside_a_step_record_the_stop_waits_for_the_row(clock, monkeypatch):
    """Ревью PR #368: `_shot` сперва пишет строку шага, потом — остановка."""
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    md._flow_budget_arm()
    md._flow_budget.frozen[:] = [clock.now - 1000 + k for k in range(md.TAB_FROZEN_STOP_EPISODES - 1)]
    md._flow_budget.defer = True
    try:
        assert md._tab_frozen(object()) is True      # отмечено, но не брошено
    finally:
        md._flow_budget.defer = False
    with pytest.raises(md.FlowBudgetExceeded):
        md._flow_budget_check()



#: Сколько висит неограниченный вызов (`evaluate`, `is_visible`) на зависшей вкладке.
HANG_S = 1200.0


class _Locator:
    """`is_visible` на зависшей вкладке таймаута не соблюдает (ACL-1320)."""

    def __init__(self, tab):
        self.tab = tab
        self.first = self

    def is_visible(self, timeout=None):
        self.tab.clock.now += HANG_S
        raise TimeoutError("late")


class _FrozenTab:
    """Вкладка 573ab2cf3646: драйвер соблюдает таймаут `wait_for_function`,
    а `evaluate` и вызовы локаторов висят 20 минут (ACL-1641)."""

    url = "https://adsmanager.facebook.com/adsmanager/manage/adsets/edit/standalone"

    def __init__(self, clock):
        self.clock = clock

    def wait_for_function(self, js, arg=None, timeout=0):
        self.clock.now += timeout / 1000.0
        raise TimeoutError("Timeout %dms exceeded" % timeout)

    def evaluate(self, *a, **k):
        self.clock.now += HANG_S
        raise TimeoutError("late")

    def get_by_text(self, *a, **k):
        return _Locator(self)

    def locator(self, *a, **k):
        return _Locator(self)


@pytest.fixture
def stuck(clock, monkeypatch):
    monkeypatch.setattr(md, "_reload_refused", lambda page, where: False)
    monkeypatch.setattr(md, "_telemetry", SimpleNamespace(record=lambda *a, **k: None))
    return _FrozenTab(clock)


def _over(clock, start):
    return clock.now - start - md.STEP_BUDGET_S


def test_the_step_limit_fires_on_a_frozen_tab_after_a_failed_create(clock, stuck):
    """573ab2cf3646 (06.10): отказ `create_additional_ad` на 81-й секунде шага,
    дальше разбор отказа на вкладке, где `evaluate` и `is_visible` висят.
    Срок шага (600 с) сработал на 1221-й; теперь — в пределах срока плюс запас."""
    md._flow_budget_arm()
    md._flow_budget_step("ad2_creative_upload")
    start = clock.now
    clock.now += 81
    with pytest.raises(md.FlowBudgetExceeded) as stop:
        for _ in range(1000):
            md.recover_stuck_page(stuck, "ad2_create", editor=True)
            try:
                md._adopt_shell_or_recreate(stuck, "adset", frozenset())
            except Exception:
                pass                     # вызывающий пишет `fail_adN_create` и идёт дальше
    assert stop.value.reason == "step_budget_exceeded"
    assert 0 <= _over(clock, start) <= 30


def test_bounded_reads_hear_the_step_limit_on_their_own(clock, stuck):
    """Чтения страницы (`_eval_bounded`) сами проверяют срок шага: цикл из них
    над зависшей вкладкой не тянет шаг. Голый `page.evaluate` здесь повис бы."""
    md._flow_budget_arm()
    md._flow_budget_step("ad2_creative_upload")
    start = clock.now
    with pytest.raises(md.FlowBudgetExceeded) as stop:
        for _ in range(1000):
            md._chrome_error_page(stuck)
            md._tree_new_ad_shells(stuck)
            md._tree_shell_ids(stuck)
            md._open_tree_ad(stuck, "New Leads Ad")
    assert stop.value.reason == "step_budget_exceeded"
    assert 0 <= _over(clock, start) <= 30


class _AnsweringTab:
    """Вкладка отвечает, страница читается, ничего не найдено — ни одной
    проверки срока по дороге, кроме входной."""

    url = "https://adsmanager.facebook.com/adsmanager/manage/adsets/edit/standalone"

    def wait_for_function(self, js, arg=None, timeout=0):
        return SimpleNamespace(json_value=lambda: {"v": None}, dispose=lambda: None)

    def get_by_text(self, *a, **k):
        return SimpleNamespace(first=SimpleNamespace(is_visible=lambda timeout=None: False))


def test_recover_stuck_page_checks_the_step_limit_on_entry(clock, stuck, monkeypatch):
    monkeypatch.setattr(md, "_page_responsive", lambda page, timeout_s=5.0: True)
    md._flow_budget_arm()
    md._flow_budget_step("ad2_creative_upload")
    assert md.recover_stuck_page(_AnsweringTab(), "ad2_create", editor=True) is False
    clock.now += md.STEP_BUDGET_S + 1
    with pytest.raises(md.FlowBudgetExceeded):
        md.recover_stuck_page(_AnsweringTab(), "ad2_create", editor=True)


def test_a_failed_tree_read_does_not_create_a_second_ad(monkeypatch):
    """Ревью PR #664 (B3): дерево не прочиталось — повторное создание могло
    добавить второе объявление рядом с заготовкой. ACL-1675: заготовка
    опознаётся по id строки."""
    created = []
    opened = []
    monkeypatch.setattr(md, "_open_shell_by_id", lambda page, rid: opened.append(rid) or False)
    monkeypatch.setattr(md, "_tree_shell_ids", lambda page: None)
    monkeypatch.setattr(md, "create_additional_ad", lambda page, name: created.append(1) or True)
    with pytest.raises(RuntimeError):
        md._adopt_shell_or_recreate(object(), "adset", frozenset())
    assert created == []
    monkeypatch.setattr(md, "_tree_shell_ids", lambda page: frozenset())   # дерево читается, заготовки нет
    assert md._adopt_shell_or_recreate(object(), "adset", frozenset()) is True
    assert created == [1]
    # Ревью PR #664 (B3'): новая заготовка видна, но открыть её не вышло — не создавать.
    created.clear()
    new_row = md._SHELL_ROW_PREFIX + "7"
    monkeypatch.setattr(md, "_tree_shell_ids", lambda page: frozenset({new_row}))
    assert md._adopt_shell_or_recreate(object(), "adset", frozenset()) is False
    assert created == [] and opened == [new_row]
    # Заготовка была ещё до отказа (в снимке), новой нет — повтор безопасен.
    assert md._adopt_shell_or_recreate(object(), "adset", frozenset({new_row})) is True
    assert created == [1]


def test_an_unreadable_chrome_error_page_is_still_an_error_page(clock):
    class Tab:
        url = "chrome-error://chromewebdata/"

        def wait_for_function(self, *a, **k):
            raise TimeoutError("Timeout")

    assert md._chrome_error_page(Tab()) == "ERR_UNKNOWN"
    Tab.url = "https://adsmanager.facebook.com/x"
    assert md._chrome_error_page(Tab()) == ""


def test_component_failed_and_all_edits_saved_skip_a_frozen_tab(clock, stuck):
    start = clock.now
    assert md._component_failed(stuck) is False
    assert md._all_edits_saved(stuck) is False
    assert clock.now - start < HANG_S           # до `is_visible` не дошли


def test_create_additional_ad_refuses_at_once_on_a_frozen_tab(clock, stuck):
    start = clock.now
    assert md.create_additional_ad(stuck, "adset") is False
    assert clock.now - start < HANG_S


def test_eval_bounded_hears_stop_on_a_hung_tab(clock, stuck):
    """ACL-1641 (N5): Stop (`JobCancelled`) слышен и на вкладке, что не отвечает."""
    import threading
    from src.fb_ads_rpa import job_cancel

    event = threading.Event()
    event.set()
    job_cancel.bind(event)
    try:
        with pytest.raises(job_cancel.JobCancelled):
            md._eval_bounded(stuck, "() => 1")
    finally:
        job_cancel.unbind()
