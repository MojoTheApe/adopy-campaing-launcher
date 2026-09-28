# -*- coding: utf-8 -*-
"""Окно-экскурсия Meta закрывается своим крестиком; новый редактор «в один
экран» — понятная остановка (ACL-1320).

Живой прогон 26.09 (Facebook_9943): поверх редактора кампании встало окно
«A streamlined way to create campaigns» — «Create your campaign in a single
step…», счётчик «1 of 3», кнопка «Next» и крестик в шапке; адрес
`/adsmanager/manage/simple_create`. Робот обязан закрывать такие окна их
собственным крестиком — никогда «Next» / «Get started» / «Try it» и никогда
крестиком самого редактора. А новый редактор «в один экран» записанный поток
не поддерживает: кампании падали на `campaign_name` / `next_to_adset` после
минут попыток — теперь это сразу `simple_create_layout`, без повторов.

Разметка синтетическая, по описанию владельца и снимкам прогона.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401

SIMPLE_CREATE = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1"
                 "&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4&sc_1nn=true")

_X_SVG = '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M2 2L14 14M14 2L2 14"/></svg>'

_EDITOR = """
<div data-surface="/am/editor_drawer" style="position:absolute;left:0;top:0;width:1000px;height:700px">
  <div role="button" aria-label="Close" onclick="log('drawer_x')"
       style="position:absolute;right:8px;top:8px;width:24px;height:24px">{svg}</div>
  <input placeholder="Enter your campaign name here" style="margin-top:60px">
  {inside}
</div>
"""

_TOUR = """
<div role="dialog" id="tour" style="position:fixed;left:300px;top:150px;width:420px;height:260px;background:#fff">
  <div style="position:relative;height:48px">
    <div role="heading" aria-level="2">A streamlined way to create campaigns</div>
    <div role="button" {x_label} onclick="log('x'); document.getElementById('tour').remove()"
         style="position:absolute;right:8px;top:8px;width:24px;height:24px">{svg}</div>
  </div>
  <div>Create your campaign in a single step, with campaign, ad set and ad on one screen.</div>
  <div style="position:absolute;bottom:12px;left:12px"><span>1 of 3</span></div>
  <div role="button" onclick="log('next')" style="position:absolute;bottom:12px;right:12px">Next</div>
</div>
"""

_PAGE = """<html><body>
<script>window.clicks = []; function log(w) {{ window.clicks.push(w); }}</script>
{body}
</body></html>"""


@pytest.fixture
def page(monkeypatch):
    rows: list = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **kw: rows.append((step, kw)))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            p = b.new_page(viewport={"width": 1280, "height": 800})
            p.rows = rows
            yield p
        finally:
            b.close()


def _set(page, body):
    page.set_content(_PAGE.format(body=body))


def _notes(page):
    return [kw.get("note") for step, kw in page.rows if step == "overlays_cleared"]


def test_the_tour_over_the_editor_is_closed_by_its_own_x(page):
    tour = _TOUR.format(x_label='aria-label="Close"', svg=_X_SVG)
    _set(page, _EDITOR.format(svg=_X_SVG, inside="") + tour)
    got = md._close_onboarding_tour(page, "onboarding_modal")
    assert got == "A streamlined way to create campaigns"
    assert page.evaluate("window.clicks") == ["x"]
    assert page.locator("#tour").count() == 0
    assert "onboarding_closed:A streamlined way to create campaigns" in _notes(page)


def test_a_header_x_without_a_label_is_found(page):
    tour = _TOUR.format(x_label="", svg=_X_SVG)
    _set(page, _EDITOR.format(svg=_X_SVG, inside="") + tour)
    assert md._close_onboarding_tour(page) == "A streamlined way to create campaigns"
    assert page.evaluate("window.clicks") == ["x"]


def test_a_tour_inside_the_editor_drawer_never_closes_the_drawer(page):
    tour = _TOUR.format(x_label='aria-label="Close"', svg=_X_SVG)
    _set(page, _EDITOR.format(svg=_X_SVG, inside=tour))
    assert md._close_onboarding_tour(page) == "A streamlined way to create campaigns"
    assert page.evaluate("window.clicks") == ["x"]


def test_the_editor_itself_is_never_taken_for_a_tour(page):
    """Редактор в роли dialog, со своим счётчиком и крестиком, — не экскурсия."""
    editor = ('<div role="dialog" style="position:fixed;left:0;top:0;width:900px;height:600px">'
              '<div role="heading">A streamlined way to create campaigns</div><span>1 of 3</span>'
              '<input placeholder="Enter your campaign name here">'
              '<div role="button" aria-label="Close" onclick="log(\'editor_x\')" '
              'style="position:absolute;right:8px;top:8px;width:24px;height:24px">' + _X_SVG + '</div></div>')
    _set(page, editor)
    assert md._close_onboarding_tour(page) == ""
    assert page.evaluate("window.clicks") == []


def test_a_tour_without_an_x_is_left_alone_and_never_pushed_forward(page):
    tour = ('<div role="dialog" style="position:fixed;left:300px;top:150px;width:420px;height:200px">'
            '<div role="heading">A streamlined way to create campaigns</div><span>1 of 3</span>'
            '<div role="button" onclick="log(\'next\')">Next</div>'
            '<div role="button" onclick="log(\'start\')">Get started</div>'
            '<div role="button" onclick="log(\'try\')">Try it</div></div>')
    _set(page, tour)
    assert md._close_onboarding_tour(page) == ""
    assert page.evaluate("window.clicks") == []
    assert any(n.startswith("onboarding_no_close:") for n in _notes(page))


def test_the_overlay_sweep_closes_the_tour(page, monkeypatch):
    monkeypatch.setattr(md, "_tab_frozen", lambda p: False)
    tour = _TOUR.format(x_label='aria-label="Close"', svg=_X_SVG)
    _set(page, _EDITOR.format(svg=_X_SVG, inside="") + tour)
    closed = md._clear_blocking_overlays(page, "campaign", editor=False)
    assert "onboarding_closed:A streamlined way to create campaigns" in closed
    assert page.evaluate("window.clicks") == ["x"]


# ── Новый редактор «в один экран» ─────────────────────────────────────────

def test_the_single_screen_editor_is_recognised_by_its_address():
    assert md._simple_create_layout(MagicMock(url=SIMPLE_CREATE)) is True
    assert md._simple_create_layout(MagicMock(
        url="https://adsmanager.facebook.com/adsmanager/manage/campaigns/edit/standalone?act=1")) is False
    assert md._simple_create_layout(MagicMock(
        url="https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&next=/adsmanager/manage/simple_create")) is False


def test_the_flow_stops_in_the_single_screen_editor_before_the_name(flow, monkeypatch):
    """ACL-1327: робот теперь проходит новый редактор
    (test_the_single_screen_editor_flow.py); прежняя остановка до имени
    осталась откатом FB_RPA_SIMPLE_CREATE=0."""
    monkeypatch.setenv("FB_RPA_SIMPLE_CREATE", "0")
    monkeypatch.setattr(md, "_open_campaign", lambda *a, **k: "")
    flow["page"].url = SIMPLE_CREATE
    shots: list = []
    real_capture = md._telemetry.normalise_step
    monkeypatch.setattr(md._telemetry, "normalise_step",
                        lambda label: shots.append(label) or real_capture(label))
    assert flow["run"]() == "simple_create_layout"
    assert "fail_simple_create_layout" in shots
    assert "after_campaign_name" not in shots, "имя кампании вписывали в неподдерживаемый редактор"


def test_the_single_screen_editor_is_a_blocking_stop_without_retries():
    step = "simple_create_layout"
    assert step in retry_policy.NON_RETRYABLE_FLOW_STEPS
    assert retry_policy.disposition(step).action == retry_policy.BLOCK_AND_REVIEW
    assert failure_policy.severity(step) == failure_policy.BLOCKING
    assert "single-screen campaign editor" in failure_policy.reason(step)
    assert acl_sentry._reason_kind("simple_create") == "simple-create"
    from src.web import publish_progress

    assert "single-screen" in publish_progress._BLOCKERS[step]


def test_a_pager_alone_marks_a_tour(page):
    tour = ('<div role="dialog" id="t" style="position:fixed;left:300px;top:150px;width:420px;height:200px">'
            '<div role="heading">Meet the new workspace</div><span>2 of 4</span>'
            '<div role="button" aria-label="Close" onclick="log(\'x\'); document.getElementById(\'t\').remove()"'
            ' style="position:absolute;right:8px;top:8px;width:24px;height:24px">' + _X_SVG + '</div></div>')
    _set(page, tour)
    assert md._close_onboarding_tour(page) == "Meet the new workspace"
    assert page.evaluate("window.clicks") == ["x"]


def test_a_known_title_alone_marks_a_tour(page):
    tour = ('<div role="dialog" id="t" style="position:fixed;left:300px;top:150px;width:420px;height:200px">'
            '<div role="heading">A streamlined way to create campaigns</div>'
            '<div role="button" aria-label="Close" onclick="log(\'x\'); document.getElementById(\'t\').remove()"'
            ' style="position:absolute;right:8px;top:8px;width:24px;height:24px">' + _X_SVG + '</div></div>')
    _set(page, tour)
    assert md._close_onboarding_tour(page) == "A streamlined way to create campaigns"
    assert page.evaluate("window.clicks") == ["x"]


def test_an_ordinary_dialog_is_not_a_tour(page):
    other = ('<div role="dialog" style="position:fixed;left:300px;top:150px;width:420px;height:200px">'
             '<div role="heading">Discard drafts?</div>'
             '<div role="button" aria-label="Close" onclick="log(\'x\')"'
             ' style="position:absolute;right:8px;top:8px;width:24px;height:24px">' + _X_SVG + '</div></div>')
    _set(page, other)
    assert md._close_onboarding_tour(page) == ""
    assert page.evaluate("window.clicks") == []


# ── Ревью PR #323 (F4) ────────────────────────────────────────────────────

def _editor_dialog(inner):
    return ('<div role="dialog" style="position:fixed;left:0;top:0;width:1000px;height:700px">'
            '<div role="button" aria-label="Close" onclick="log(\'editor_x\')" '
            'style="position:absolute;right:8px;top:8px;width:24px;height:24px">' + _X_SVG + '</div>'
            + inner + '</div>')


def test_a_tour_nested_in_an_editor_dialog_closes_only_the_tour(page):
    """Редактор — role=dialog без полей имени (ещё грузится), внутри —
    экскурсия: закрывается крестик экскурсии, не редактора."""
    tour = _TOUR.format(x_label='aria-label="Close"', svg=_X_SVG)
    _set(page, _editor_dialog(tour + '<div>Loading...</div>'))
    assert md._close_onboarding_tour(page) == "A streamlined way to create campaigns"
    assert page.evaluate("window.clicks") == ["x"]


def test_a_promo_line_in_an_editor_dialog_is_not_a_tour(page):
    _set(page, _editor_dialog('<div>Try a new way to create campaigns with Advantage+</div>'))
    assert md._close_onboarding_tour(page) == ""
    assert page.evaluate("window.clicks") == []


def test_a_dialog_holding_the_editor_drawer_is_never_touched(page):
    inner = ('<div data-surface="/am/editor_drawer"><span>1 of 3</span>'
             '<div role="heading">A streamlined way to create campaigns</div></div>')
    _set(page, _editor_dialog(inner))
    assert md._close_onboarding_tour(page) == ""
    assert page.evaluate("window.clicks") == []


def test_a_close_labelled_forward_button_is_never_taken(page):
    """«Next» с aria-label Close (Meta так размечает кнопки-стрелки) — не крестик."""
    tour = ('<div role="dialog" id="t" style="position:fixed;left:300px;top:150px;width:420px;height:220px">'
            '<div role="heading">A streamlined way to create campaigns</div><span>1 of 3</span>'
            '<div role="button" aria-label="Close" onclick="log(\'next\')">Next</div>'
            '<div role="button" aria-label="Close" onclick="log(\'x\'); document.getElementById(\'t\').remove()"'
            ' style="position:absolute;right:8px;top:8px;width:24px;height:24px">' + _X_SVG + '</div></div>')
    _set(page, tour)
    assert md._close_onboarding_tour(page) == "A streamlined way to create campaigns"
    assert page.evaluate("window.clicks") == ["x"]


def test_the_x_of_a_dialog_nested_in_the_tour_is_not_the_tours(page):
    inner = ('<div role="dialog" style="position:absolute;left:10px;top:60px;width:200px;height:60px">'
             '<div>Tip</div><div role="button" aria-label="Close" onclick="log(\'inner_x\')"'
             ' style="width:20px;height:20px">' + _X_SVG + '</div></div>')
    tour = ('<div role="dialog" id="t" style="position:fixed;left:300px;top:150px;width:420px;height:260px">'
            + inner + '<div role="heading">A streamlined way to create campaigns</div><span>1 of 3</span>'
            '<div role="button" aria-label="Close" onclick="log(\'x\'); document.getElementById(\'t\').remove()"'
            ' style="position:absolute;right:8px;top:8px;width:24px;height:24px">' + _X_SVG + '</div></div>')
    _set(page, tour)
    assert md._close_onboarding_tour(page) == "A streamlined way to create campaigns"
    assert page.evaluate("window.clicks") == ["x"]
