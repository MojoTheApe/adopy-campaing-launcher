# -*- coding: utf-8 -*-
"""Повтор в фикстурах — ровно один и только на ошибке Playwright (ACL-1491).

Второй отказ должен падать: повтор лечит сбой окружения на нагруженном
раннере, а не прячет поломку. Ошибка не из Playwright не повторяется вовсе.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from tests import _retry_once


class _Flaky:
    def __init__(self, *failures):
        self.failures = list(failures)
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


class _Page:
    def __init__(self, goto):
        self.goto = goto


class _Pw:
    def __init__(self, launch):
        self.chromium = type("C", (), {"launch": staticmethod(launch)})()


def test_one_timeout_is_retried_once():
    flaky = _Flaky(PlaywrightTimeout("Timeout 30000ms exceeded."))
    assert _retry_once.goto(_Page(flaky), "http://acl.test/") == "ok"
    assert flaky.calls == 2


def test_a_second_failure_is_not_hidden():
    flaky = _Flaky(PlaywrightTimeout("first"), PlaywrightTimeout("second"))
    with pytest.raises(PlaywrightTimeout, match="second"):
        _retry_once.goto(_Page(flaky), "http://acl.test/")
    assert flaky.calls == 2


def test_a_non_playwright_error_is_not_retried():
    flaky = _Flaky(ValueError("bug in the page under test"))
    with pytest.raises(ValueError):
        _retry_once.goto(_Page(flaky), "http://acl.test/")
    assert flaky.calls == 1


def test_browser_launch_is_retried_once():
    flaky = _Flaky(PlaywrightError("Browser closed before it started"))
    assert _retry_once.launch_chromium(_Pw(flaky)) == "ok"
    assert flaky.calls == 2
    twice = _Flaky(PlaywrightError("one"), PlaywrightError("two"))
    with pytest.raises(PlaywrightError, match="two"):
        _retry_once.launch_chromium(_Pw(twice))
