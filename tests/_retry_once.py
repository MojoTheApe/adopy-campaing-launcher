# -*- coding: utf-8 -*-
"""Один повтор запуска браузера и `page.goto` в фикстурах (ACL-1491).

На нагруженном раннере (2 CPU, `-n auto --dist loadfile`, соседние воркеры
поднимают свои Chromium) запуск браузера или первый переход по маршрутной
странице изредка упираются в таймаут Playwright (CI 01–02.10: `Page.goto`
30 с). Это сбой окружения, а не поведения под проверкой, поэтому фикстура
повторяет ровно один раз и только на ошибке Playwright. Второй отказ
падает как обычно — настоящая поломка всё равно видна.

Проверки самих тестов повтор не трогает: он живёт только в подготовке.
"""
from __future__ import annotations

import os

from playwright.sync_api import Error as PlaywrightError


def launch_chromium(pw, **kwargs):
    """`pw.chromium.launch` с одним повтором на ошибке Playwright."""
    kwargs.setdefault("executable_path", os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
    try:
        return pw.chromium.launch(**kwargs)
    except PlaywrightError:
        return pw.chromium.launch(**kwargs)


def goto(page, url: str, **kwargs):
    """`page.goto` с одним повтором на ошибке Playwright (таймаут и прочее)."""
    try:
        return page.goto(url, **kwargs)
    except PlaywrightError:
        return page.goto(url, **kwargs)
