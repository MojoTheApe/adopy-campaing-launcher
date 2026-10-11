"""Прослойка `_static_scripts_come_from_disk` (tests/conftest.py, ACL-1800).

Заглушка маршрута, отвечающая JSON-ом на всё подряд, не должна подменять скрипт
страницы: `/static/*.js` отдаётся с диска, остальное идёт в заглушку теста.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import BrowserContext, Page  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeRoute:
    def __init__(self, url):
        self.request = type("Req", (), {"url": url})()
        self.answers = []

    def fulfill(self, **kw):
        self.answers.append(kw)


def _wrapped(handler):
    """Заглушка теста, обёрнутая прослойкой (так её получает Playwright)."""
    return Page.route.static_wrap(handler)


@pytest.mark.parametrize("cls", [Page, BrowserContext])
def test_route_is_wrapped_for_pages_and_contexts(cls):
    assert cls.route.__name__ == "patched"


def test_static_script_is_served_from_disk_before_the_json_stub():
    stub_calls = []
    wrapped = _wrapped(lambda route: stub_calls.append(route.request.url))
    r = FakeRoute("http://x.test/static/acm_commands.js")
    wrapped(r)
    assert stub_calls == [] and r.answers[0]["content_type"] == "application/javascript"
    assert r.answers[0]["body"] == (ROOT / "src" / "web" / "static" / "acm_commands.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("url", [
    "http://x.test/api/buyers", "http://x.test/static/missing.js", "http://x.test/static/logo.svg",
    "http://x.test/static/../app.py", "http://x.test/static/%2e%2e/x.js", "http://x.test/"])
def test_everything_else_goes_to_the_test_stub(url):
    stub_calls = []
    wrapped = _wrapped(lambda route: stub_calls.append(route.request.url))
    r = FakeRoute(url)
    wrapped(r)
    assert stub_calls == [url] and r.answers == []
