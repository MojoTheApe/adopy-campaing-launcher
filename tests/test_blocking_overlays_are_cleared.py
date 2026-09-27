# -*- coding: utf-8 -*-
"""«Turn off ad blocker» и тост сети закрываются, список перезагружается (ACL-1313).

Прогон 279ead80503a, 25.09 (rpa_screenshots/279ead80503a/
20260925_062343_recorded_after_create_retry.png): после Create поверх списка
кампаний встало окно «Turn off ad blocker» (крестик и OK), сверху — тост
«Your internet connection was lost. Some of your work may not be available.»
с крестиком, а поверх всего — вечная «Loading…». `_network_trouble` только
обрывал ожидание окна цели; окно так и стояло, и следующий Create уходил в
него же.

Просьба владельца: окно закрыть (OK или крестик), затем перезагрузить список
и записать в телеметрию `network_trouble` — это сигнал о сети или прокси
профиля. `_clear_blocking_overlays` делает это и заодно закрывает баннеры из
test_banners_are_closed_by_their_own_x.py; зовётся в начале каждого раздела и
перед каждым Create.

Разметки окна в снимках нет (rpa_pages той минуты не сохранился), поэтому
она синтетическая, по скриншоту: заголовок, текст, крестик в углу, кнопка OK;
модалка «Loading…» частично перекрывает окно.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401

AD_BLOCKER = """
<div id="adblock" role="dialog" style="position:fixed; top:180px; left:530px; width:450px; height:150px; background:#eee">
  <div><h3>Turn off ad blocker</h3>
    <div role="button" aria-label="Close" tabindex="0" style="position:absolute; top:8px; right:8px; width:16px; height:16px"><svg width="16" height="16"></svg></div></div>
  <div><span>To use Ads Manager, turn off your ad blocker or add this site to your allowlist, then refresh the page.</span></div>
  <div style="position:absolute; bottom:10px; right:10px"><div role="button" tabindex="0" style="width:40px; height:28px">OK</div></div>
</div>
"""

TOAST = """
<div id="toast" style="position:fixed; top:75px; left:550px; width:420px; height:24px; background:#333; color:#fff">
  <span>Your internet connection was lost. Some of your work may not be available.</span>
  <div role="button" aria-label="Close" tabindex="0" style="display:inline-block; width:12px; height:12px"><svg width="12" height="12"></svg></div>
</div>
"""

# «Loading…» поверх окна: центр окна прикрыт, кнопка OK — нет.
LOADING = """
<div id="loading" style="position:fixed; top:200px; left:530px; width:450px; height:80px; background:#fff">
  <span>Loading...</span>
</div>
"""

_SPY = """
() => {
  window.clicked = [];
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[role=button], button');
    if (!b) return;
    window.clicked.push(b.getAttribute('aria-label') || b.textContent.trim());
    const own = b.closest('#adblock, #toast');
    if (own) own.remove();
  }, true);
}
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    reloads = []
    monkeypatch.setattr(md, "_reload_editor", lambda pg, edit_url="": reloads.append(pg.url))
    p.reloads = reloads
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())
            if r["step"] == "overlays_cleared"]


def _set(page, html):
    page.set_content(html)
    page.evaluate(_SPY)


def test_the_ad_blocker_modal_is_closed_with_ok_and_the_list_reloaded(page, tmp_path):
    _set(page, AD_BLOCKER + TOAST + LOADING)
    closed = md._clear_blocking_overlays(page, "objective", editor=False)
    assert page.locator("#adblock").count() == 0, "окно ad blocker осталось"
    assert page.locator("#toast").count() == 0, "тост сети остался"
    assert "OK" in page.evaluate("window.clicked")
    assert "ad_blocker_modal:ok" in closed
    assert "Your internet connection was lost" in closed
    assert closed[-1] == "network_trouble:reloaded"
    assert len(page.reloads) == 1
    row = _rows(tmp_path)[-1]
    assert row["note"] == "network_trouble"
    assert row["diag"]["where"] == "objective" and row["diag"]["reloaded"] is True


def test_the_modal_without_ok_is_closed_by_its_x(page):
    _set(page, AD_BLOCKER.replace(">OK<", "><"))
    closed = md._clear_blocking_overlays(page, "create", editor=False)
    assert page.locator("#adblock").count() == 0
    assert "ad_blocker_modal:x" in closed


def test_in_the_editor_unsaved_edits_are_not_thrown_away(page, monkeypatch):
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: False)
    _set(page, AD_BLOCKER)
    closed = md._clear_blocking_overlays(page, "adset", editor=True)
    assert page.locator("#adblock").count() == 0
    assert closed[-1] == "network_trouble:unsaved_edits"
    assert page.reloads == []


def test_in_the_editor_saved_edits_allow_the_reload(page, monkeypatch):
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: True)
    _set(page, AD_BLOCKER)
    assert md._clear_blocking_overlays(page, "ad", editor=True)[-1] == "network_trouble:reloaded"
    assert len(page.reloads) == 1


def test_the_toast_alone_in_the_editor_is_closed_without_a_reload(page, tmp_path):
    _set(page, TOAST)
    closed = md._clear_blocking_overlays(page, "ad2", editor=True)
    assert page.locator("#toast").count() == 0
    assert closed == ["Your internet connection was lost", "network_trouble:not_reloaded"]
    assert page.reloads == []
    assert _rows(tmp_path)[-1]["note"] == "network_trouble"


def test_a_clean_page_is_left_alone(page, tmp_path):
    _set(page, "<div><span>Campaigns</span><div role='button'>Create</div></div>")
    assert md._clear_blocking_overlays(page, "campaign", editor=False) == []
    assert page.evaluate("window.clicked") == []
    assert page.reloads == [] and _rows(tmp_path) == []


# ── в потоке: где зовётся ──────────────────────────────────────────────────

def test_every_section_and_create_clears_overlays_first(flow, monkeypatch):
    calls = []
    monkeypatch.setattr(md, "_clear_blocking_overlays",
                        lambda p, where, **kw: calls.append(where) or [])
    flow["took"][:] = ["nothing", "ok"]
    flow["run"]()
    assert calls[0] == "campaign", "до первого Create не почистили"
    assert "create_quick_retry" in calls
    assert "adset" in calls and "ad" in calls


def test_a_cleared_ad_blocker_replaces_the_blind_reload(flow, monkeypatch):
    """Окно закрыто и список уже перезагружен — второй reload не нужен.

    ACL-1320: раунд без окна цели идёт на список заново (`_open_campaign`),
    окна поверх него закрываются перед новым Create; `recover_stuck_page` на
    этом пути не зовётся вовсе."""
    calls = []

    def clear(p, where, **kw):
        calls.append(where)
        return ["ad_blocker_modal:ok", "network_trouble:reloaded"] if where == "create_retry" else []

    monkeypatch.setattr(md, "_clear_blocking_overlays", clear)
    flow["waits"][:] = [False, True]
    flow["run"]()
    ev = flow["events"]
    assert "create" in calls and "create_retry" in calls
    assert not [e for e in ev if e[0] == "recover" and e[1] in ("objective", "create")]
    assert ev.count(("create_button",)) >= 2, "после закрытия окна Create не нажали"
