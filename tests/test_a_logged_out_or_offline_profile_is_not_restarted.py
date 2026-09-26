# -*- coding: utf-8 -*-
"""Вход в Facebook и мёртвый прокси — остановка, а не перезапуск (ACL-1320).

Живой прогон c00d8c3 (26.09, 15:21–15:57 UTC, новые аккаунты):

* Facebook_1443, 15:22 — «Navigation … is interrupted by another navigation
  to https://business.facebook.com/business/loginpage/…»: аккаунт разлогинен;
* 15:21–15:47 — `ERR_SOCKS_CONNECTION_FAILED`, `ERR_SSL_PROTOCOL_ERROR` на
  переходах к списку кампаний; следом «вкладка не отвечает», свежая вкладка
  и `profile_restarted_unresponsive` через MLX API — раз за разом;
* Facebook_9943, 15:40 — перезапуск кончился `connect ECONNREFUSED`, и
  профиль остался закрытым, а итог говорил только «не отвечает».

Теперь перед свежей вкладкой и перезапуском — диагноз
(`profile_trouble.classify_page`): вход — `profile_logged_out`, сеть —
`profile_network`, неудачный перезапуск — `profile_restart_failed`. Все три
не повторяются, блокируют с понятным человеку текстом, и следующая кампания
того же задания уже не начинается.
"""
from __future__ import annotations

import os
import sys
import time as _real_time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402
from tests.test_a_campaign_opens_within_two_minutes import _P, _publish, env  # noqa: E402,F401
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401

LIST_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"

#: Текст исключения Facebook_1443 из draft_status 26.09 15:22 (номер кабинета заменён).
LOGIN_INTERRUPT = (
    'Page.goto: Navigation to "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1" '
    'is interrupted by another navigation to "https://business.facebook.com/business/loginpage/'
    '?next=https%3A%2F%2Fadsmanager.facebook.com%2Fadsmanager%2Fmanage%2Fcampaigns%3Fact%3D1'
    '&login_options%5B0%5D=FB&login_options%5B1%5D=SSO"')
SOCKS = "Page.goto: net::ERR_SOCKS_CONNECTION_FAILED at https://adsmanager.facebook.com/adsmanager/manage/campaigns"
SSL = "Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://adsmanager.facebook.com/adsmanager/manage/campaigns"


# ── Диагноз ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://business.facebook.com/business/loginpage/?next=https%3A%2F%2Fadsmanager.facebook.com",
    "https://www.facebook.com/login/?next=https%3A%2F%2Fadsmanager.facebook.com",
    "https://www.facebook.com/login.php",
    "https://facebook.com/login",
])
def test_facebook_login_pages_are_recognised(url):
    assert pt.login_url(url) is True


@pytest.mark.parametrize("url", [
    LIST_URL,
    "https://adsmanager.facebook.com/adsmanager/manage/campaigns/edit?act=1",
    "https://www.facebook.com/loginsomething",
    "https://example.com/login",
    "",
])
def test_ads_manager_and_other_pages_are_not_login(url):
    assert pt.login_url(url) is False


def test_navigation_errors_are_classified():
    assert pt.classify_error(RuntimeError(LOGIN_INTERRUPT))[0] == pt.LOGGED_OUT
    assert pt.classify_error(RuntimeError(SOCKS)) == (pt.NETWORK, "ERR_SOCKS_CONNECTION_FAILED")
    assert pt.classify_error(RuntimeError(SSL)) == (pt.NETWORK, "ERR_SSL_PROTOCOL_ERROR")
    assert pt.classify_error(RuntimeError("net::ERR_PROXY_CONNECTION_FAILED"))[0] == pt.NETWORK
    assert pt.classify_error(RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED"))[0] == pt.NETWORK
    assert pt.classify_error(RuntimeError("net::ERR_ABORTED"))[0] == ""
    assert pt.classify_error(RuntimeError("Timeout 30000ms exceeded."))[0] == ""


@pytest.fixture
def chromium():
    # Не на модуль: тесты подключения ниже (`_connect_playwright`) гасят
    # синхронный Playwright этого потока, и общий браузер не закрылся бы.
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


def test_a_visible_login_form_and_a_proxy_error_page_are_read_from_the_dom(chromium):
    page = chromium.new_page()
    try:
        page.set_content('<form><input name="email" type="text"><input name="pass" type="password">'
                         '<button name="login">Log in</button></form>')
        assert pt.classify_page(page, history=False) == (pt.LOGGED_OUT, "login_form")
        page.set_content('<div id="main-frame-error"><h1>This site can’t be reached</h1>'
                         '<div class="error-code">ERR_SOCKS_CONNECTION_FAILED</div></div>')
        assert pt.classify_page(page, history=False) == (pt.NETWORK, "ERR_SOCKS_CONNECTION_FAILED")
        page.set_content('<div role="main"><div role="button">Create</div>'
                         '<input type="search" placeholder="Search"></div>')
        assert pt.classify_page(page, history=False) == ("", "")
    finally:
        page.close()


def test_network_history_counts_only_unresolved_repeated_errors_off_a_meta_page():
    """Ревью PR #323 (F3): история — только вспомогательный признак."""
    ctx = SimpleNamespace()
    blank = SimpleNamespace(url="about:blank", context=ctx)
    meta = SimpleNamespace(url=LIST_URL, context=ctx)
    pt.note_net_error(blank, "ERR_CONNECTION_RESET")
    assert pt.classify_page(blank, read_dom=False) == ("", ""), "одна ошибка — ещё не сеть"
    pt.note_net_error(blank, "ERR_SOCKS_CONNECTION_FAILED")
    assert pt.classify_page(blank, read_dom=False) == (pt.NETWORK, "ERR_SOCKS_CONNECTION_FAILED")
    assert pt.classify_page(meta, read_dom=False) == ("", ""),         "живая страница Ads Manager по одной истории названа сетью"
    assert pt.classify_page(blank, history=False, read_dom=False) == ("", "")
    pt.note_nav_ok(meta)
    assert pt.recent_net_errors(blank) == []
    assert pt.classify_page(blank, read_dom=False) == ("", ""), "удачный переход не обнулил историю"


# ── Переход к списку кампаний ─────────────────────────────────────────────

class _GotoPage:
    def __init__(self, errors, url_after=LIST_URL):
        self.errors = list(errors)
        self.url = "about:blank"
        self.url_after = url_after
        self.context = SimpleNamespace()
        self.gotos = 0

    def goto(self, url, **kw):
        self.gotos += 1
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        self.url = self.url_after


@pytest.fixture
def goto_flow(monkeypatch):
    from src.fb_ads_rpa import ads_manager_flow as amf

    monkeypatch.setattr(amf.time, "sleep", lambda s: None)
    monkeypatch.setattr(amf, "pause_after_navigation", lambda: None)
    ready: list = []
    monkeypatch.setattr(amf.AdsManagerFlow, "_ensure_campaigns_ready", lambda self, name: ready.append(name))
    monkeypatch.setattr("src.fb_ads_rpa.screenshots.capture_on_success", lambda *a, **k: "")

    def make(page):
        flow = amf.AdsManagerFlow(page)
        draft = SimpleNamespace(ad_account_id="act_1", name="Campaign A")
        result = SimpleNamespace(steps_completed=[], screenshot_paths=[])
        return lambda: flow._goto_campaigns(draft, result)

    return SimpleNamespace(make=make, ready=ready)


def test_a_login_redirect_stops_at_the_first_navigation(goto_flow):
    page = _GotoPage([SOCKS, LOGIN_INTERRUPT])
    with pytest.raises(pt.ProfileStop) as stop:
        goto_flow.make(page)()
    assert stop.value.code == pt.LOGGED_OUT
    assert "business.facebook.com/business/loginpage" in stop.value.detail
    assert page.gotos == 2, "после редиректа на вход переход повторяли"


def test_a_redirect_to_login_after_the_page_loaded_stops_too(goto_flow):
    page = _GotoPage([], url_after="https://business.facebook.com/business/loginpage/?next=x")
    with pytest.raises(pt.ProfileStop) as stop:
        goto_flow.make(page)()
    assert stop.value.code == pt.LOGGED_OUT
    assert goto_flow.ready == []


def test_three_proxy_failures_are_a_network_stop(goto_flow):
    page = _GotoPage([SOCKS, SSL, SOCKS])
    with pytest.raises(pt.ProfileStop) as stop:
        goto_flow.make(page)()
    assert stop.value.code == pt.NETWORK
    assert stop.value.detail == "ERR_SOCKS_CONNECTION_FAILED"


def test_one_ssl_error_then_success_is_not_a_stop(goto_flow):
    """20.09 KLD_BFL_S15: разовый ERR_SSL_PROTOCOL_ERROR лечится повтором."""
    page = _GotoPage([SSL])
    goto_flow.make(page)()
    assert goto_flow.ready == ["Campaign A"]
    # Ревью PR #323 (F3): повтор прошёл — ошибка вылечена и не в счёт.
    assert pt.recent_net_errors(page) == []


# ── Открытие кампании: диагноз до свежей вкладки ──────────────────────────

class _TP(_P):
    def __init__(self, name, url=LIST_URL):
        super().__init__(name)
        self.url = url
        self.context = SimpleNamespace()


def test_a_frozen_tab_on_a_chrome_error_page_is_a_network_stop_not_a_fresh_tab(env):
    page = _TP("old", url="chrome-error://chromewebdata/")
    pt.note_net_error(page, "ERR_SOCKS_CONNECTION_FAILED")
    env.sc.frozen = {"old"}
    env.sc.fresh = [_P("new")]
    got = md._open_campaign(page, LIST_URL, press=lambda p, where: True, shot=lambda label: None)
    assert got == pt.NETWORK
    assert not [e for e in env.ev if e[0] == "fresh"], "свежую вкладку открыли на мёртвом прокси"


def test_a_slow_meta_after_one_healed_network_error_is_not_an_account_stop(env):
    """Ревью PR #323 (F3), сценарий ревьюера: ERR_CONNECTION_RESET от
    повтора перехода, Meta просто медленная — свежая вкладка, не остановка."""
    old = _TP("old")
    pt.note_net_error(old, "ERR_CONNECTION_RESET")
    pt.note_net_error(old, "ERR_CONNECTION_RESET")
    env.sc.fresh = [_P("new")]
    env.sc.dialog_at = ("new", env.start + 100)
    got = md._open_campaign(old, LIST_URL, press=lambda p, w: env.clock.sleep(2) or True,
                            shot=lambda *a, **k: None, on_new_page=lambda p: None)
    assert got != pt.NETWORK
    assert [e for e in env.ev if e[0] == "fresh"]


def test_a_login_page_stops_before_create(env):
    page = _TP("old", url="https://business.facebook.com/business/loginpage/?next=x")
    pressed: list = []
    got = md._open_campaign(page, LIST_URL, press=lambda p, where: pressed.append(where) or True,
                            shot=lambda label: None)
    assert got == pt.LOGGED_OUT
    assert pressed == []


def test_a_fresh_tab_that_lands_on_login_is_a_stop(env, monkeypatch):
    env.sc.frozen = {"old"}

    def fresh(p, url, **kw):
        env.ev.append(("fresh", p.name, url))
        raise pt.ProfileStop(pt.LOGGED_OUT, "business.facebook.com/business/loginpage/")

    monkeypatch.setattr(br, "fresh_tab", fresh)
    got = md._open_campaign(_TP("old"), LIST_URL, press=lambda p, where: True, shot=lambda label: None)
    assert got == pt.LOGGED_OUT


def test_a_genuinely_frozen_tab_still_gets_a_fresh_one(env):
    env.sc.frozen = {"old"}
    env.sc.fresh = [_P("new")]
    env.sc.dialog_at = ("new", env.clock.now + 60)
    got = md._open_campaign(_TP("old"), LIST_URL, press=lambda p, where: True, shot=lambda label: None)
    assert got == ""
    assert [e for e in env.ev if e[0] == "fresh"]


def test_the_flow_returns_the_profile_stops(flow, monkeypatch):
    for code in (pt.LOGGED_OUT, pt.NETWORK):
        monkeypatch.setattr(md, "_open_campaign", lambda *a, **k: code)
        assert flow["run"]() == code


def test_a_proxy_error_page_that_outlives_the_reloads_stops_before_create(flow, monkeypatch):
    """Страница ошибки сети пережила две перезагрузки — Create не жмём."""
    page = flow["page"]
    monkeypatch.setattr(md, "_recover_from_browser_error",
                        lambda p: pt.note_net_error(p, "ERR_SOCKS_CONNECTION_FAILED") or False)
    monkeypatch.setattr(pt, "classify_page",
                        lambda p, **kw: (pt.NETWORK, "ERR_SOCKS_CONNECTION_FAILED") if p is page else ("", ""))
    opened: list = []
    monkeypatch.setattr(md, "_open_campaign", lambda *a, **k: opened.append(1) or "")
    assert flow["run"]() == pt.NETWORK
    assert opened == []


# ── Публикатор: не повторять, не перезапускать, сказать человеку ──────────

def test_a_profile_stop_on_the_way_to_the_list_is_not_retried(monkeypatch):
    from src.fb_ads_rpa import ads_manager_flow as amf
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    monkeypatch.setenv("FB_RPA_ENGINE", "recorded_flow")
    monkeypatch.setattr(br, "put_window_back", lambda page: None)
    monkeypatch.setattr(amf.time, "sleep", lambda s: None)
    gotos: list = []

    def goto(self, draft, result):
        gotos.append(1)
        raise pt.ProfileStop(pt.LOGGED_OUT, "business.facebook.com/business/loginpage/")

    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", goto)
    ran: list = []
    monkeypatch.setattr(amf, "run_recorded_campaign_flow", lambda page, **kw: ran.append(page) or "")
    restarts: list = []
    flow = amf.AdsManagerFlow("old-page", restart_profile=lambda: restarts.append(1) or "new-page")
    draft = CampaignDraft("Campaign A", "MX", "Account",
                          ad_sets=[AdSetDraft("Set A", ads=[AdDraft(ad_title="t", ad_body="b")])])
    result = flow.publish_campaign(draft)
    assert ran == [], "поток кампании пошёл в разлогиненный профиль"
    assert gotos == [1], "переход повторили"
    assert restarts == []
    assert result.success is False
    assert pt.LOGGED_OUT in result.blocking_failures
    assert pt.MESSAGES[pt.LOGGED_OUT] in result.error
    assert f"recorded_flow_retry_blocked:{pt.LOGGED_OUT}:block_and_review" in result.steps_completed


def test_a_failed_restart_is_its_own_stop_with_a_message(monkeypatch):
    def restart():
        raise RuntimeError("BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:56240")

    result, seen, _ = _publish(monkeypatch, ["profile_unresponsive"], restart)
    assert seen == ["old-page"]
    assert result.success is False
    assert pt.RESTART_FAILED in result.blocking_failures
    assert pt.MESSAGES[pt.RESTART_FAILED] in result.error
    assert "profile_restarted_unresponsive:failed" in result.steps_completed


def test_the_publisher_turns_a_provider_restart_error_into_a_stop():
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    pub = RpaPublisher.__new__(RpaPublisher)

    def restart(entry, label):
        raise RuntimeError("connect ECONNREFUSED 127.0.0.1:56240")

    pub._provider = SimpleNamespace(restart_unresponsive=restart, close=lambda entry, **kw: None)
    pub._entry = "entry"
    pub._conn = None
    with pytest.raises(pt.ProfileStop) as stop:
        pub._restart_unresponsive_profile("Facebook_9943")
    assert stop.value.code == pt.RESTART_FAILED
    assert "ECONNREFUSED" in stop.value.detail


def test_after_a_profile_stop_the_next_campaign_does_not_touch_the_browser(monkeypatch):
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._built_at = None
    attaches: list = []

    def ensure(label):
        attaches.append(label)
        raise pt.ProfileStop(pt.NETWORK, "ERR_SOCKS_CONNECTION_FAILED")

    monkeypatch.setattr(pub, "_ensure_browser", ensure)

    def draft(name):
        return CampaignDraft(name, "MX", "Facebook_9943",
                             ad_sets=[AdSetDraft("Set", ads=[AdDraft(ad_title="t", ad_body="b")])])

    first = pub.publish_draft(draft("A"))
    second = pub.publish_draft(draft("B"))
    assert attaches == ["Facebook_9943"], "второй кампании снова открывали профиль"
    for got in (first, second):
        assert got.success is False
        assert got.blocking_failures == [pt.NETWORK]
        assert pt.MESSAGES[pt.NETWORK] in got.error


# ── Политики ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [pt.LOGGED_OUT, pt.NETWORK, pt.RESTART_FAILED])
def test_profile_stops_are_blocking_and_never_retried(code):
    assert retry_policy.disposition(code).action == retry_policy.BLOCK_AND_REVIEW
    assert retry_policy.should_retry_campaign(code, completed_attempts=0) is False
    assert code in retry_policy.BLOCKING_REVIEW_STEPS
    assert failure_policy.severity(code) == failure_policy.BLOCKING
    assert pt.MESSAGES[code] in failure_policy.reason(code)


def test_the_flow_stops_are_in_the_non_retryable_set():
    assert {pt.LOGGED_OUT, pt.NETWORK} <= retry_policy.NON_RETRYABLE_FLOW_STEPS
    assert pt.RESTART_FAILED not in retry_policy.CAMPAIGN_RETRY_STEPS


def test_sentry_names_the_profile_reasons():
    assert acl_sentry._reason_kind(LOGIN_INTERRUPT) == "logged-out"
    assert acl_sentry._reason_kind(SOCKS) == "proxy-network"
    assert acl_sentry._reason_kind(SSL) == "proxy-network"
    assert acl_sentry._reason_kind(
        "Restart of unresponsive profile Facebook_9943 failed: connect ECONNREFUSED") == "restart-failed"
    assert acl_sentry.failure_reason(pt.LOGGED_OUT, "login_page") == "login-page"


def test_the_dashboard_says_what_to_do():
    from src.web import publish_progress

    for code in (pt.LOGGED_OUT, pt.NETWORK, pt.RESTART_FAILED):
        assert publish_progress._BLOCKERS[code] == pt.MESSAGES[code]


def test_a_fresh_tab_that_is_also_silent_is_diagnosed_before_the_restart(env, monkeypatch):
    """Свежая вкладка тоже молчит, а сеть профиля за это время упала —
    перед «перезапускай профиль» диагноз: это сеть."""
    ctx = SimpleNamespace()
    old, new = _TP("old"), _TP("new", url="about:blank")
    old.context = new.context = ctx
    env.sc.frozen = {"old", "new"}

    def fresh(p, url, **kw):
        # Свежая вкладка так и не ушла с about:blank: переходы в ней падали.
        env.ev.append(("fresh", p.name, url))
        pt.note_net_error(new, "ERR_SSL_PROTOCOL_ERROR")
        pt.note_net_error(new, "ERR_SSL_PROTOCOL_ERROR")
        return new

    monkeypatch.setattr(br, "fresh_tab", fresh)
    got = md._open_campaign(old, LIST_URL, press=lambda p, where: True, shot=lambda label: None)
    assert got == pt.NETWORK


# ── Память ошибок сети профиля ────────────────────────────────────────────

def test_network_errors_on_the_way_are_remembered(monkeypatch):
    page = SimpleNamespace(url=LIST_URL, context=SimpleNamespace())

    def goto(url, **kw):
        raise RuntimeError(SOCKS)

    page.goto = goto
    page.get_by_role = lambda *a, **k: SimpleNamespace(
        first=SimpleNamespace(wait_for=lambda **kw: None))
    md._open_list_again(page, LIST_URL, _real_time.time() + 5)
    assert pt.recent_net_errors(page) == ["ERR_SOCKS_CONNECTION_FAILED"]

    other = SimpleNamespace(url=LIST_URL, context=SimpleNamespace())
    codes = iter(["ERR_SSL_PROTOCOL_ERROR", ""])
    monkeypatch.setattr(md._telemetry, "browser_error", lambda p: next(codes, ""))
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_reload_editor", lambda p, *a: None)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    assert md._recover_from_browser_error(other) is True
    assert pt.recent_net_errors(other) == [], "перезагрузка вылечила — ошибка не в счёт"

    stuck = SimpleNamespace(url=LIST_URL, context=SimpleNamespace())
    monkeypatch.setattr(md._telemetry, "browser_error", lambda p: "ERR_SSL_PROTOCOL_ERROR")
    assert md._recover_from_browser_error(stuck) is False
    assert pt.recent_net_errors(stuck) == ["ERR_SSL_PROTOCOL_ERROR"] * 2


# ── Подключение к уже открытому профилю ───────────────────────────────────

def _attach_env(monkeypatch, page):
    class _Ctx:
        pages = [page]

    browser = SimpleNamespace(contexts=[_Ctx()])
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: SimpleNamespace(start=lambda: pw))
    monkeypatch.setattr(br, "ensure_playwright_browsers_path", lambda: None)
    monkeypatch.setattr(br, "_unminimize_before_attach", lambda b: [])
    fresh: list = []
    monkeypatch.setattr(br, "fresh_tab", lambda p, url, **kw: fresh.append(url))
    return fresh


def test_attaching_to_a_silent_profile_on_the_login_page_is_a_stop(monkeypatch):
    class _Page:
        url = "https://business.facebook.com/business/loginpage/?next=x"

        def wait_for_function(self, *a, **k):
            raise TimeoutError("Timeout 5000ms exceeded.")

    fresh = _attach_env(monkeypatch, _Page())
    with pytest.raises(pt.ProfileStop) as stop:
        br._connect_playwright(cdp_url="http://127.0.0.1:1", provider="multilogin_direct",
                               debug_port="1", ws_endpoint=None, check_responsive=True)
    assert stop.value.code == pt.LOGGED_OUT
    assert fresh == [], "свежую вкладку открыли на странице входа"


def test_the_attach_path_hands_a_profile_stop_up_without_stopping_the_profile(monkeypatch):
    ev: list = []
    provider = br.MultiloginAttachOnlyProvider()
    monkeypatch.setattr(br, "_find_mlx_profile_dir", lambda pid: "C:/mlx/p-1")
    monkeypatch.setattr(provider._direct, "_profile_process_running", lambda *a: True)
    monkeypatch.setattr(provider._direct, "_discover_debug_port", lambda *a: 9222)

    def connect(**kw):
        raise pt.ProfileStop(pt.NETWORK, "ERR_SOCKS_CONNECTION_FAILED")

    monkeypatch.setattr(br, "_connect_playwright", connect)
    monkeypatch.setattr(provider, "_stop_profile", lambda *a: ev.append("stop"))
    monkeypatch.setattr(provider, "_start_via_mlx_api", lambda *a, **kw: ev.append("start"))
    entry = SimpleNamespace(profile_id="p-1", folder_id="f-1", multilogin_instance="A")
    with pytest.raises(pt.ProfileStop):
        provider.open(entry, "Facebook_1443")
    assert ev == [], "профиль остановили или подняли заново"


def test_a_fresh_tab_that_meets_the_login_page_raises_a_stop(monkeypatch):
    class _New:
        url = "about:blank"

        def goto(self, url, **kw):
            raise RuntimeError(LOGIN_INTERRUPT)

    new = _New()
    old = SimpleNamespace(url=LIST_URL, close=lambda **kw: None,
                          context=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True)))
    monkeypatch.setattr(br, "_open_tab_via_browser", lambda ctx, **kw: new)
    monkeypatch.setattr(br, "page_answers", lambda p, t=5.0: True)
    monkeypatch.setattr(br, "_disable_background_throttling", lambda c, p: None)
    monkeypatch.setattr(br, "_keep_renderer_active", lambda c, p: None)
    with pytest.raises(pt.ProfileStop) as stop:
        br.fresh_tab(old, LIST_URL)
    assert stop.value.code == pt.LOGGED_OUT


def test_the_publisher_does_not_wrap_a_profile_stop(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp

    pub = rp.RpaPublisher.__new__(rp.RpaPublisher)
    pub._conn = None
    pub._entry = None
    pub.hand_off = pub.dry_run = False

    def open_(entry, label):
        raise pt.ProfileStop(pt.LOGGED_OUT, "login_form")

    pub._provider = SimpleNamespace(open=open_, close=lambda entry, **kw: None)
    monkeypatch.setattr(pub, "_resolve_entry", lambda label: SimpleNamespace(profile_id="p-1"))
    monkeypatch.setattr(rp, "get_mlx_attach_only", lambda: True)
    with pytest.raises(pt.ProfileStop):
        pub._ensure_browser("Facebook_1443")


def test_the_new_single_screen_editor_stops_the_whole_account():
    """Одно-экранный редактор Meta включён на весь кабинет: остальные кампании
    задания этого аккаунта не начинаются (иначе каждая оставит полчерновика)."""
    from src.fb_ads_rpa import profile_trouble as pt
    assert "simple_create_layout" in pt.ACCOUNT_STOPS
    assert "single-screen" in pt.message("simple_create_layout")


def test_a_slow_navigation_in_a_fresh_tab_keeps_the_tab(monkeypatch):
    """Ревью PR #323 (F10): переход в свежей вкладке упал по таймауту, а
    вкладка отвечает — её и отдаём (не None → не перезапуск профиля)."""
    class _New:
        url = "about:blank"

        def goto(self, url, **kw):
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")

    new = _New()
    old = SimpleNamespace(url=LIST_URL, close=lambda **kw: None,
                          context=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True)))
    monkeypatch.setattr(br, "_open_tab_via_browser", lambda ctx, **kw: new)
    monkeypatch.setattr(br, "page_answers", lambda p, t=5.0: True)
    monkeypatch.setattr(br, "_disable_background_throttling", lambda c, p: None)
    monkeypatch.setattr(br, "_keep_renderer_active", lambda c, p: None)
    assert br.fresh_tab(old, LIST_URL) is new
    answers = iter([True, False])
    monkeypatch.setattr(br, "page_answers", lambda p, t=5.0: next(answers))
    assert br.fresh_tab(old, LIST_URL) is None, "молчащую вкладку после таймаута отдали"
