# -*- coding: utf-8 -*-
"""Зависшая вкладка: ни одного вызова без таймаута перед ожиданием (ACL-1320).

Прогон cf563b4ba594 (26.09, Doc19): ~14 из 19 минут вкладка списка кампаний
не отвечала, а робот сидел в вызовах, у которых таймаута нет вовсе:
`locator.count()`, `is_visible(timeout=…)` (Playwright этот таймаут
игнорирует), `page.evaluate`, CDP `send`. Сторож `_StallWatch` проверялся
только в начале попытки и сработал на 108-й и 224-й секунде вместо 60-й.

Теперь перед каждым таким местом — `_tab_frozen` (ожидание с таймаутом,
который соблюдает драйвер), чтения страницы идут через `_eval_bounded`, а
сторож получает `frozen=True` и действует сразу. Подставные страницы здесь
падают на любом неограниченном вызове — тест ловит сам факт такого вызова.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401


class _Unbounded(AssertionError):
    """Вызов без таймаута на зависшей вкладке — то, чего быть не должно."""


class _FrozenPage:
    """Вкладка, которая не отвечает: проба живости кончается таймаутом,
    а всё без таймаута — это вечное ожидание (здесь — исключение-метка)."""

    url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"

    def __init__(self):
        self.probes: list[int] = []
        #: неограниченные вызовы: исключение-метку код может проглотить
        #: своим `except`, поэтому сам факт вызова записывается здесь.
        self.unbounded: list[str] = []

    def wait_for_function(self, js, arg=None, timeout=None):
        self.probes.append(timeout)
        raise TimeoutError(f"Timeout {timeout}ms exceeded.")

    def _hang(self, *a, **k):
        self.unbounded.append("call")
        raise _Unbounded("unbounded call on a frozen tab")

    evaluate = _hang
    get_by_text = _hang
    get_by_role = _hang
    locator = _hang

    @property
    def context(self):
        self.unbounded.append("context")
        raise _Unbounded("CDP session on a frozen tab")


@pytest.fixture
def rows(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: out.append({"step": step, "ok": ok, **kw}))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    return out


def test_the_probe_is_bounded_by_the_driver(rows):
    page = _FrozenPage()
    assert md._tab_frozen(page) is True
    assert page.probes == [int(md.LIVENESS_PROBE_S * 1000)]


def test_loading_is_not_asked_of_a_frozen_tab(rows):
    page = _FrozenPage()
    assert md._loading_visible(page) is False
    assert page.unbounded == []


def test_the_route_placeholder_is_read_with_a_timeout(rows):
    page = _FrozenPage()
    assert md._route_placeholder_visible(page) is False
    assert page.probes, "чтение должно идти через wait_for_function"
    assert page.unbounded == []


def test_the_drafts_counter_is_read_with_a_timeout(rows):
    page = _FrozenPage()
    assert md._pending_drafts(page) is None
    assert page.probes and page.unbounded == []


def test_create_took_says_frozen_at_once(rows):
    page = _FrozenPage()
    assert md._create_took(page, page.url, timeout_s=30) == "frozen"
    assert len(page.probes) == 1 and page.unbounded == []


def test_no_cdp_click_is_sent_to_a_frozen_tab(rows):
    page = _FrozenPage()
    md._cdp_mouse_click(page, 100, 50)
    assert page.unbounded == []
    assert [r["note"] for r in rows if r["step"] == "tab_frozen"] == ["cdp_click"]


def test_overlays_are_not_searched_on_a_frozen_tab(rows):
    page = _FrozenPage()
    assert md._clear_blocking_overlays(page, "create", editor=False) == ["tab_frozen"]
    assert page.unbounded == []


def test_the_stall_guard_fires_at_once_on_a_frozen_tab(monkeypatch, rows):
    fired: list[tuple] = []
    monkeypatch.setattr(md, "_unstick",
                        lambda p, where, **kw: fired.append((where, kw["waited_s"])) or "reloaded")
    watch = md._StallWatch(SimpleNamespace(url=""), "create", editor=False)
    assert watch.check() == "", "живая вкладка — бюджет ещё не вышел"
    assert watch.check(frozen=True) == "reloaded"
    assert fired and fired[0][1] < md.STALL_BUDGET_S


def test_the_flow_click_hands_a_frozen_tab_to_the_guard_within_its_budget(flow, monkeypatch):
    """`_click` не входит в `_wait_loading`/`wait_for` на зависшей вкладке, а
    сторож срабатывает сразу — не на 108-й секунде, как в cf563b4ba594."""
    fired: list[tuple] = []
    monkeypatch.setattr(md, "_page_responsive", lambda p, timeout_s=5.0: False)
    monkeypatch.setattr(md, "_unstick",
                        lambda p, where, **kw: fired.append((where, kw["waited_s"])) or "woke")
    flow["run"]()
    assert fired, "сторож не сработал на зависшей вкладке"
    where, waited = fired[0]
    assert where.startswith("create") and waited < md.STALL_BUDGET_S
    page = flow["page"]
    assert not page.get_by_text.return_value.wait_for.called, "ждали кнопку на зависшей вкладке"
