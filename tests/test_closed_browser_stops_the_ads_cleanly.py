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


def test_page_gone_recognises_playwrights_wording():
    assert dom._page_gone(RuntimeError(CLOSED))
    assert dom._page_gone(RuntimeError("Browser closed."))
    assert not dom._page_gone(RuntimeError("Timeout 5000ms exceeded"))
