# -*- coding: utf-8 -*-
"""Список кампаний готов к Create до первого нажатия (ACL-1320).

`_goto_campaigns` считал список открытым по `domcontentloaded`. В прогоне
cf563b4ba594 (26.09) первое нажатие Create шло в страницу, которая минутами не
дорисовывалась. Теперь после перехода ждём (≤ `CAMPAIGNS_READY_S`) видимую
Create без маршрутного спиннера Meta; нет — одна перезагрузка и ещё одно
ожидание; строка телеметрии `campaigns_list_ready`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import ads_manager_flow as amf  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa.draft import RpaPublishResult  # noqa: E402


class _Page:
    """Список: кнопка Create появляется только после `ready_after` перезагрузок."""

    def __init__(self, ready_after=0, placeholder_after=None):
        self.ready_after = ready_after
        self.placeholder_after = placeholder_after
        self.reloads = 0
        self.gotos: list[str] = []
        self.waits: list[tuple] = []
        self.context = object()
        self.url = ""

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url  # ACL-1697: адрес открытого кабинета сверяется после перехода

    def reload(self, **kw):
        self.reloads += 1

    def get_by_role(self, role, name=None, **kw):
        page = self

        class _Btn:
            def wait_for(self, state="visible", timeout=0):
                page.waits.append(("create", timeout))
                if page.reloads < page.ready_after:
                    raise TimeoutError(f"Timeout {timeout}ms exceeded")

        return SimpleNamespace(first=_Btn())

    def wait_for_selector(self, sel, state="hidden", timeout=0):
        self.waits.append(("placeholder", timeout))
        if self.placeholder_after is not None and self.reloads < self.placeholder_after:
            raise TimeoutError(f"Timeout {timeout}ms exceeded")

    def wait_for_function(self, *a, **k):
        return True


@pytest.fixture
def run(monkeypatch):
    rows: list = []
    ev: list = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(amf, "pause_after_navigation", lambda: None)
    monkeypatch.setattr(br, "_disable_background_throttling", lambda ctx, p: ev.append("unthrottle"))
    import src.fb_ads_rpa.screenshots as shots
    monkeypatch.setattr(shots, "capture_on_success", lambda *a, **k: "")

    def go(page):
        flow = amf.AdsManagerFlow(page)
        draft = MagicMock(ad_account_id="act_123")
        draft.name = "Campaign A"
        flow._goto_campaigns(draft, RpaPublishResult(campaign_name="Campaign A"))
        ready = [r for r in rows if r[0] == "campaigns_list_ready"]
        return ready[-1] if ready else None

    return SimpleNamespace(go=go, rows=rows, ev=ev)


def test_a_ready_list_is_not_reloaded(run):
    page = _Page()
    row = run.go(page)
    assert page.reloads == 0
    assert row[1] is True and row[2]["note"] == "ready"
    assert all(t <= amf.CAMPAIGNS_READY_S * 1000 for _, t in page.waits)


def test_a_list_without_create_is_reloaded_once(run):
    page = _Page(ready_after=1)
    row = run.go(page)
    assert page.reloads == 1
    assert row[1] is True and row[2]["note"] == "after_reload" and row[2]["diag"]["first"] == "no_create"
    assert "unthrottle" in run.ev, "после перезагрузки троттлинг не снят"


def test_a_spinning_route_placeholder_is_not_ready(run):
    page = _Page(placeholder_after=1)
    row = run.go(page)
    assert page.reloads == 1 and row[2]["diag"]["first"] == "route_placeholder"


def test_a_list_that_never_gets_ready_is_reloaded_only_once(run):
    page = _Page(ready_after=99)
    row = run.go(page)
    assert page.reloads == 1
    assert row[1] is False and row[2]["note"] == "no_create"
