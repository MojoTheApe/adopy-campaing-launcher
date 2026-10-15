# -*- coding: utf-8 -*-
"""Закрытая вкладка профиля посреди объявлений — чистая остановка, без повтора
(прогон 503b0c077db6, GT_ait_All_Video_Day, 25.09 20:59).

Вкладку закрыли, пока создавалось третье объявление. Отказ ушёл мягкой
строкой `ad3_create:Page.evaluate: Target page, context or browser has been
clos…`, и поток продолжил работу по мёртвой странице. Теперь поток
возвращает `browser_closed`: остановка, которую повтор не лечит (повтор
кампании создал бы второй черновик), с понятной оператору причиной.

`run_recorded_campaign_flow` гоняется на подставной странице (как в
test_additional_ad_without_creative_has_no_text.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: E402,F401

CLOSED = "Page.evaluate: Target page, context or browser has been closed"


def test_a_closed_tab_during_the_next_ad_stops_the_flow_cleanly(flow, monkeypatch):
    calls = []

    def create(p, name):
        calls.append(name)
        p.is_closed.return_value = True     # вкладки действительно нет
        raise RuntimeError(CLOSED)

    monkeypatch.setattr(dom, "create_additional_ad", create)
    flow["fail_upload"] = set()
    result, soft = flow["run"]()
    assert result == "browser_closed"
    assert calls == ["Set"], "после закрытия вкладки больше ничего не делали"
    assert not any(s.startswith("ad2_create:") for s in soft), soft


def test_an_ordinary_error_is_still_a_soft_failure(flow, monkeypatch):
    def create(p, name):
        raise RuntimeError("Locator.click: Timeout 5000ms exceeded")

    monkeypatch.setattr(dom, "create_additional_ad", create)
    flow["fail_upload"] = set()
    result, soft = flow["run"]()
    assert result == ""
    assert any(s.startswith("ad2_create:Locator.click") for s in soft), soft


def test_browser_closed_is_never_retried_and_blocks_with_a_reason():
    assert retry_policy.should_retry_campaign("browser_closed", completed_attempts=0) is False
    assert "browser_closed" in retry_policy.NON_RETRYABLE_FLOW_STEPS
    assert failure_policy.severity("browser_closed") == failure_policy.BLOCKING
    reason = failure_policy.reason("browser_closed")
    assert "unclassified" not in reason and "duplicate" in reason and reason.isascii()


class _Page:
    def __init__(self, closed=False, probe_error=None):
        self.closed, self.probe_error = closed, probe_error

    def is_closed(self):
        return self.closed

    def evaluate(self, js):
        if self.probe_error:
            raise RuntimeError(self.probe_error)
        return 1


def test_page_gone_needs_the_page_itself_to_be_gone():
    """Текст «Target page … closed» бывает и у живой страницы (отсоединённая
    CDP-сессия, закрытая соседняя вкладка) — решает сама страница."""
    assert dom._page_gone(_Page(closed=True), RuntimeError(CLOSED))
    assert dom._page_gone(_Page(probe_error=CLOSED), RuntimeError("Browser closed."))
    assert not dom._page_gone(_Page(), RuntimeError(CLOSED)), "живая страница — не закрытая"
    assert not dom._page_gone(_Page(closed=True), RuntimeError("Timeout 5000ms exceeded"))


def test_a_detached_session_on_a_live_page_is_a_soft_failure(flow, monkeypatch):
    def create(p, name):
        p.is_closed.return_value = False    # страница жива, отвалилась только сессия
        raise RuntimeError(CLOSED)

    monkeypatch.setattr(dom, "create_additional_ad", create)
    flow["fail_upload"] = set()
    result, soft = flow["run"]()
    assert result == ""
    assert any(s.startswith("ad2_create:") for s in soft), soft


def test_failures_before_the_stop_are_kept(flow, monkeypatch):
    """B2: отказы, собранные до закрытия вкладки, остаются в итоге."""
    def create(p, name):
        p.is_closed.return_value = True
        raise RuntimeError(CLOSED)

    monkeypatch.setattr(dom, "create_additional_ad", create)
    flow["fail_upload"] = {"a.png"}
    dom._set_upload_why(None)  # причина соседнего теста не должна утекать в строку отказа
    result, soft = flow["run"]()
    assert result == "browser_closed"
    assert "ad_creative_upload" in soft, soft


@pytest.mark.parametrize("which", ["first", "second"])
def test_a_closed_tab_while_writing_text_stops_too(flow, monkeypatch, which):
    """B6/B7: ветки текста первого и второго объявления."""
    def write(p, title, body, **kw):
        if which == "second" and kw.get("ad_name") != "Ad 2":
            flow["copies"].append((kw.get("ad_name"), title, body))
            return True
        p.is_closed.return_value = True
        raise RuntimeError(CLOSED)

    created = []
    monkeypatch.setattr(dom, "write_ad_copy", write)
    monkeypatch.setattr(dom, "create_additional_ad", lambda p, name: created.append(name) or True)
    flow["fail_upload"] = set()
    result, soft = flow["run"]()
    assert result == "browser_closed"
    if which == "first":
        assert created == [], "после закрытия вкладки второе объявление не создавали"
    assert not any("_copy:" in s for s in soft), soft


def test_browser_closed_is_a_blocking_review_step_with_its_reason_in_the_error(monkeypatch):
    """B4/B5: остановка блокирует публикацию, и оператор видит причину в ошибке."""
    assert "browser_closed" in retry_policy.BLOCKING_REVIEW_STEPS
    from tests.test_objective_dialog_waits_while_loading import _publish_with_failures

    result, sleeps = _publish_with_failures(monkeypatch, ["browser_closed"])
    assert sleeps == [], "не повторяется"
    assert result.success is False
    assert "browser_closed" in result.blocking_failures
    assert failure_policy.reason("browser_closed") in result.error
