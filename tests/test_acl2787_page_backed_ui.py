"""ACL-2787: драйвер `page_backed_ui.press` на настоящей разметке Ads Manager (fixture) в Playwright, без сети."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from src.fb_api import page_backed_ui as pbu

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "tests/fixtures/ads_manager/simple_create_2026_09_27.html").read_text(encoding="utf-8")
NEW_AD = "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=123"
FAST = dict(load_wait_s=1.5, settle_wait_s=1.0, poll_s=0.05)

#: Эмуляция Meta: клик по «Use Facebook Page» убирает контрол (идентичность создана) или открывает диалог.
_META = """
window.__clicks = [];
document.addEventListener('click', (e) => {
  const t = (e.target.textContent || '').trim();
  window.__clicks.push(t.slice(0, 40));
  if (window.__meta === 'create' && /^use facebook page$/i.test(t)) { e.target.remove(); }
  if (window.__meta === 'dialog' && /^use facebook page$/i.test(t)) {
    const d = document.createElement('div'); d.setAttribute('role', 'dialog'); d.textContent = 'Confirm';
    document.body.appendChild(d);
  }
}, true);
"""


class FakeTab:
    """Вкладка поверх Playwright-страницы: ровно то, чем пользуется драйвер (navigate/evaluate_main/call/pump/close)."""

    def __init__(self, page, html, *, href=NEW_AD, meta="create", navigate_error=None):
        self.page, self.html, self.href, self.meta, self.navigate_error = page, html, href, meta, navigate_error
        self.navigated, self.calls, self.closed, self.on_event = [], [], False, None

    def navigate(self, url, timeout=30.0):
        self.navigated.append(url)
        if self.navigate_error:
            raise self.navigate_error
        self.page.set_content(self.html)
        self.page.evaluate(f"window.__meta = {self.meta!r}")
        self.page.evaluate(_META)

    def evaluate_main(self, expr, timeout=10.0):
        got = self.page.evaluate(expr)
        if isinstance(got, dict) and "href" in got:
            got["href"] = self.href            # адрес страницы Meta: set_content его не меняет
        return got

    def call(self, method, params=None, **_kw):
        self.calls.append(method)
        if method == "Input.dispatchMouseEvent" and params["type"] == "mousePressed":
            self.page.mouse.click(params["x"], params["y"])
        return {}

    def pump(self, seconds):
        time.sleep(min(seconds, 0.02))

    def close(self, verify=False):
        self.closed = True


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1400, "height": 1000})
    yield pg
    pg.close()


def _press(page, html=HTML, name="Sample Page", **kw):
    tab_kw = {k: kw.pop(k) for k in ("href", "meta", "navigate_error") if k in kw}
    tab = FakeTab(page, html, **tab_kw)
    why = pbu.press("act_123", "111", cdp_url="http://127.0.0.1:9222", page_name=name,
                    open_tab=lambda url: tab, **{**FAST, **kw})
    return why, tab


def _clicks(page):
    return page.evaluate("window.__clicks || []")


def test_success_presses_only_the_control_and_closes_the_tab(page):
    why, tab = _press(page)
    assert why == ""
    assert tab.navigated == [NEW_AD] and tab.closed
    clicks = [c.lower() for c in _clicks(page)]
    assert any("use facebook page" in c for c in clicks) and not any("publish" in c for c in clicks)


def test_no_instagram_block(page):
    why, tab = _press(page, HTML.replace("Instagram profile", "Something else"))
    assert why.startswith("timed out: the Instagram profile block did not appear")
    assert _clicks(page) == [] and tab.closed


def test_no_use_facebook_page_button(page):
    why, tab = _press(page, HTML.replace("Use Facebook Page", "Instagram account"))
    assert why == "the Instagram profile block has no 'Use Facebook Page' control"
    assert _clicks(page) == [] and tab.closed


def test_wrong_page_selected_is_not_clicked(page):
    why, tab = _press(page, name="Another Page")
    assert why.startswith("the editor shows another Page (Sample Page)")
    assert _clicks(page) == [] and tab.closed


def test_unknown_page_name_opens_nothing():
    opened = []
    why = pbu.press("act_123", "111", cdp_url="http://x", page_name="", open_tab=lambda u: opened.append(u))
    assert "Page name is unknown" in why and opened == []


def test_control_that_stays_after_the_click_is_a_timeout(page):
    why, tab = _press(page, meta="none")
    assert why == "timed out: the 'Use Facebook Page' control stayed after the click" and tab.closed


def test_unexpected_dialog_after_the_click(page):
    why, tab = _press(page, meta="dialog")
    assert why == "an unexpected dialog opened after the click" and tab.closed


def test_navigation_failure_closes_the_tab(page):
    why, tab = _press(page, navigate_error=TimeoutError("navigation timeout"))
    assert why.startswith("the new ad editor did not open") and tab.closed


def test_exception_in_the_page_closes_the_tab(page):
    class Boom(FakeTab):
        def evaluate_main(self, expr, timeout=10.0):
            raise RuntimeError("cdp lost")
    tab = Boom(page, HTML)
    why = pbu.press("act_123", "111", cdp_url="http://x", page_name="Sample Page", open_tab=lambda u: tab, **FAST)
    assert why == "the press failed (cdp lost)" and tab.closed


def test_tab_that_cannot_be_opened_is_a_reason_not_a_crash():
    def boom(url):
        raise OSError("no browser")
    assert pbu.press("act_1", "1", cdp_url="http://x", page_name="P", open_tab=boom).startswith(
        "could not open a tab in the profile")


@pytest.mark.parametrize("href", [
    "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1&selected_ad_ids=4",
    "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone?act=1",
    "https://adsmanager.facebook.com/checkpoint/1/",
])
def test_an_existing_ad_editor_or_checkpoint_is_never_clicked(page, href):
    why, tab = _press(page, href=href)
    assert why.startswith("unexpected page") and _clicks(page) == [] and tab.closed


def test_only_the_new_ad_address_is_ever_opened(page):
    _, tab = _press(page)
    for url in tab.navigated:
        assert url == NEW_AD and "selected_ad_ids" not in url and "/ads/edit" not in url


def test_publish_is_never_clicked_even_if_it_covers_the_control(page):
    # Publish поверх контрола: под курсором не «Use Facebook Page» -> клика нет
    cover = ('<div style="position:fixed;left:0;top:0;width:100%;height:100%;z-index:9999" role="button">'
             "Publish</div>")
    why, tab = _press(page, HTML + cover)
    assert why.startswith("timed out: the 'Use Facebook Page' control was covered")
    assert not any("publish" in c.lower() for c in _clicks(page)) and tab.closed


def test_leave_site_dialog_is_accepted_on_the_way_out(page):
    why, tab = _press(page)
    assert why == "" and callable(tab.on_event)
    tab.on_event({"method": "Page.javascriptDialogOpening"})
    assert "Page.handleJavaScriptDialog" in tab.calls


def test_forbidden_zone_text_is_refused():
    assert pbu._is_forbidden("Publish") and pbu._is_forbidden("Review and publish")
    assert not pbu._is_forbidden("Use Facebook Page")


def test_selectors_live_in_the_yaml_config():
    cfg = pbu._config()
    assert cfg["url"].format(act_id="1").endswith("simple_create?act=1")
    assert cfg["ig"] == ["instagram profile"] and cfg["btn"] == ["use facebook page"] and cfg["page"] == ["facebook page"]


@pytest.mark.parametrize("href", [
    "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=999",
    "https://adsmanager.facebook.com/adsmanager/manage/simple_create",
    "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=123&selected_campaign_ids=5",
    "https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=123&selected_adset_ids=5",
    "https://adsmanager.facebook.com/adsmanager/manage/campaigns/edit?act=123",
    "https://adsmanager.facebook.com/adsmanager/manage/adsets/edit/standalone?act=123",
])
def test_allow_list_refuses_everything_but_this_accounts_new_editor(page, href):
    why, tab = _press(page, href=href)
    assert why.startswith("unexpected page") and _clicks(page) == [] and tab.closed


def test_only_beforeunload_dialogs_are_accepted(page):
    _, tab = _press(page)
    seen = []
    tab.call = lambda m, p=None, **k: seen.append((m, p))
    tab.on_event({"method": "Page.javascriptDialogOpening", "params": {"type": "beforeunload"}})
    tab.on_event({"method": "Page.javascriptDialogOpening", "params": {"type": "confirm"}})
    assert seen == [("Page.handleJavaScriptDialog", {"accept": True}),
                    ("Page.handleJavaScriptDialog", {"accept": False})]


def test_a_control_that_keeps_moving_is_not_clicked(page):
    class Moving(FakeTab):
        n = 0
        def evaluate_main(self, expr, timeout=10.0):
            got = super().evaluate_main(expr, timeout)
            if got.get("state") == "ready":
                Moving.n += 1
                got["x"] += Moving.n
            return got
    tab = Moving(page, HTML)
    why = pbu.press("act_123", "111", cdp_url="http://x", page_name="Sample Page", open_tab=lambda u: tab, **FAST)
    assert why.startswith("timed out") and _clicks(page) == [] and tab.closed
