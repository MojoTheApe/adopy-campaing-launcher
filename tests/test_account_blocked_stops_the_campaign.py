# -*- coding: utf-8 -*-
"""Заблокированный аккаунт останавливает кампанию и не повторяется (ACL-1313).

Прогон ecce52f330b8 (22.09): красный баннер «Verification required to
advertise», у кампании в колонке Delivery «Account disabled», кнопка Create
серая. Робот трижды жал Create, дважды перезагружал страницу и через 5 минут
упал на `objective_leads` — шаг, который повтор кампании начинает заново. В
редакторе объявления та же блокировка — «Review 1 error — Start
authentication» (код #3858385), и её `_review_errors_visible` принимал за
ошибку лид-формы и запускал бесполезную починку Browser add-ons.

Разметка синтетическая: снимка rpa_pages этого прогона уже нет (ротация
output), есть только скриншоты — список кампаний с баннером повторяет
20260922_165914_recorded_fail_objective_leads.png; вид ошибки в редакторе —
по тексту из задачи ACL-1313 («Review 1 error — Start authentication»,
«(#3858385)»).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture, screenshots  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

_BANNER = """
<div role="alert" style="background:#a00;color:#fff;padding:8px">
  <div><span>Verification required to advertise</span></div>
  <div>Complete verification to remove restrictions from your ad account. Removing restrictions
    will allow you to create and run new ads and resume delivery for existing ads.</div>
  <div role="button">Verify now</div>
</div>
"""

_LIST = """
<!doctype html><html><body>
%(banner)s
<div><span>Campaigns</span></div>
<div role="button" aria-disabled="%(disabled)s" id="create" onclick="window.__create=(window.__create||0)+1">Create</div>
<table>
  <tr><th>Campaign</th><th>Delivery</th></tr>
  <tr><td>Example_Campaign_A</td><td><span>%(delivery)s</span></td></tr>
  <tr><td>Example_Campaign_B</td><td><span>Off</span></td></tr>
</table>
<div>Why is my ad account disabled? Learn more in the Help Center.</div>
</body></html>
"""


def _list(*, banner=True, delivery="Account disabled", disabled="true") -> str:
    return _LIST % {"banner": _BANNER if banner else "", "delivery": delivery, "disabled": disabled}


_EDITOR_AUTH = """
<div id="review">
  <div role="button"><span>Review 1 error</span></div>
  <div><span>Your ad account needs to be authenticated before you can publish. (#3858385)</span></div>
  <div role="button"><span>Start authentication</span></div>
</div>
<div><span>Primary text</span></div>
"""

_EDITOR_LEAD_FORM = """
<div id="review">
  <div role="button"><span>Review 1 error</span></div>
  <div><span>Param call_to_action[value][lead_gen_form_id] must be a valid Lead Gen Data id.</span></div>
</div>
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
def rows(monkeypatch):
    got = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: got.append((step, ok, k)))
    monkeypatch.setattr(md, "_OTHER_REVIEW_ERRORS_SEEN", set())
    return got


@pytest.fixture
def page(browser, monkeypatch, rows):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


# ── детектор ──────────────────────────────────────────────────────────────

def test_the_verification_banner_is_a_blocked_account(page):
    page.set_content(_list(delivery="Off"))
    got = md._account_blocked_text(page)
    assert got and "Verification required to advertise" in got
    assert md._account_blocked_match(got) == "verification_required"


def test_account_disabled_in_the_delivery_column_is_a_blocked_account(page):
    page.set_content(_list(banner=False))
    assert md._account_blocked_text(page) == "Account disabled"


def test_start_authentication_in_the_ad_editor_is_a_blocked_account(page):
    page.set_content(_EDITOR_AUTH)
    got = md._account_blocked_text(page)
    assert got and md._account_blocked_match(got) in ("start_authentication", "authentication_error")


_EDITOR_AUTH_NO_CODE = """
<div id="review">
  <div role="button"><span>Review 1 error</span></div>
  <div><span>Your ad account needs to be authenticated before you can publish.</span></div>
  <div role="button"><span>Start authentication</span></div>
</div>
<div><span>Primary text</span></div>
"""

_UNRELATED_AUTH = """
<div><span>Campaigns</span></div>
<div id="security"><div><span>Two-factor authentication</span></div>
  <div><span>Protect your account with a second step.</span></div>
  <div role="button"><span>Start authentication</span></div></div>
<div><span>Review 2 errors in other campaigns</span></div>
""" + "<div>" + "filler text " * 60 + "</div>"


def test_review_error_with_start_authentication_is_a_blocked_account_without_the_code(page):
    """PR #314, M20: «Review 1 error — Start authentication» без кода #3858385."""
    page.set_content(_EDITOR_AUTH_NO_CODE)
    got = md._account_blocked_text(page)
    assert got and md._account_blocked_match(got) == "start_authentication"


def test_start_authentication_elsewhere_on_the_page_is_not_a_blocked_account(page):
    """PR #314, находка 9: кнопка «Start authentication» вне ошибки проверки
    кампанию не останавливает."""
    page.set_content(_UNRELATED_AUTH)
    assert md._account_blocked_text(page) is None


def test_start_authentication_text_needs_a_review_error_next_to_it():
    assert md._account_blocked_match("Review 1 error Start authentication") == "start_authentication"
    assert md._account_blocked_match("Start authentication") is None
    assert md._account_blocked_match("Start authentication (#3858385)") == "authentication_error"


def test_a_healthy_account_is_not_blocked(page):
    """Статусы Off/Active и справка «Why is my ad account disabled?» — не блокировка."""
    page.set_content(_list(banner=False, delivery="Active", disabled="false"))
    assert md._account_blocked_text(page) is None


def test_a_hidden_banner_is_not_a_blocked_account(page):
    page.set_content(_list(delivery="Off").replace('role="alert" style="', 'role="alert" style="display:none;'))
    assert md._account_blocked_text(page) is None


# ── ошибки проверки: лид-форма отдельно, прочие отдельно ──────────────────

def test_start_authentication_is_not_a_lead_form_error(page, rows):
    page.set_content(_EDITOR_AUTH)
    assert md._review_errors_visible(page) is False
    other = [r for r in rows if r[0] == "review_error_other"]
    assert len(other) == 1
    assert "Start authentication" in other[0][2]["note"]
    assert other[0][2]["diag"]["account_blocked"] in ("start_authentication", "authentication_error")
    # Второй раз тот же текст телеметрию не засоряет.
    md._review_errors_visible(page)
    assert len([r for r in rows if r[0] == "review_error_other"]) == 1


def test_the_lead_gen_form_error_still_counts(page, rows):
    page.set_content(_EDITOR_LEAD_FORM)
    assert md._review_errors_visible(page) is True
    assert not [r for r in rows if r[0] == "review_error_other"]


# ── поток: остановка до Create ────────────────────────────────────────────

def test_the_flow_stops_before_create_on_a_blocked_account(page, rows, monkeypatch):
    monkeypatch.setattr(br, "ensure_editor_window", lambda p, section: True)
    monkeypatch.setattr(md, "_recover_from_browser_error", lambda p: False)
    monkeypatch.setattr(md, "_dismiss_floating_banners", lambda p: None)
    monkeypatch.setattr(md, "_pending_drafts", lambda p: 0)
    monkeypatch.setattr(rt, "page_context", lambda p: {})
    monkeypatch.setattr(rt, "window_state", lambda p: {})
    monkeypatch.setattr(screenshots, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **kw: None)
    page.set_content(_list())
    got = md.run_recorded_campaign_flow(
        page, campaign_name="Example", budget="10", adset_name="Set",
        creative_path="a.png", ad_title="t", ad_body="b", destination_url="https://example.com")
    assert got == "account_blocked"
    assert page.evaluate("window.__create || 0") == 0, "Create нажимать нельзя"
    step, ok, kw = [r for r in rows if r[0] == "account_blocked"][-1]
    assert ok is False
    assert "Verification required to advertise" in kw["note"]
    assert kw["diag"]["where"] == "campaigns_list"
    assert kw["diag"]["kind"] == "verification_required"


def test_the_flow_checks_the_ad_editor_too():
    """Проверка стоит и после перехода к объявлению, и перед каждым следующим."""
    import ast
    src = (ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "run_recorded_campaign_flow")
    body = ast.get_source_segment(src, fn)
    for where in ('"campaigns_list"', '"ad_editor"', '"ad_copy"', 'f"{tag}_before"'):
        assert f"_stop_if_account_blocked({where})" in body, where


# ── политика ──────────────────────────────────────────────────────────────

def test_account_blocked_is_never_retried():
    decision = retry_policy.disposition("account_blocked")
    assert decision.action == retry_policy.BLOCK_AND_REVIEW
    assert retry_policy.should_retry_campaign("account_blocked", completed_attempts=0) is False
    assert "account_blocked" in retry_policy.NON_RETRYABLE_FLOW_STEPS


def test_account_blocked_blocks_with_an_operator_reason():
    assert failure_policy.severity("account_blocked") == failure_policy.BLOCKING
    reason = failure_policy.reason("account_blocked")
    assert "unclassified" not in reason
    assert "Verification required to advertise" in reason and "not be retried" in reason
    assert reason.isascii()
