# -*- coding: utf-8 -*-
"""Не тот рекламный кабинет — стоп до Create (ACL-1697).

Job 05224bdd (07.10): кабинет FacebookDoc124 сменили на странице Accounts, а
Ads Manager открыл другой (у Facebook-пользователя профиля нет доступа к act
из черновика, либо Meta помнит прошлый). Робот кабинет не сверял и мог строить
кампании в чужом. Теперь перед КАЖДЫМ Create открытый кабинет (`act=` в адресе
страницы) сверяется с черновиком: иной или нечитаемый — отказ `wrong_ad_account`,
клика по Create нет. Страницы подставные, как в
test_create_quick_retry_returns_to_list.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import ad_account_guard as guard  # noqa: E402
from src.fb_ads_rpa import ads_manager_flow as amf  # noqa: E402
from src.fb_ads_rpa import failure_policy, profile_trouble, retry_policy  # noqa: E402
from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401

WANT = "1633810528347011"
OTHER = "999888777666555"
URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={}&business_id=5"


def _page(url):
    return SimpleNamespace(url=url)


# -- чтение открытого кабинета ------------------------------------------------

def test_the_opened_account_is_read_from_the_url():
    assert guard.opened_act(_page(URL.format(WANT))) == WANT
    assert guard.opened_act(_page("https://adsmanager.facebook.com/x?foo=1&act=act_" + WANT)) == WANT


def test_same_account_continues():
    assert guard.mismatch(_page(URL.format(WANT)), "act_" + WANT) == ""
    assert guard.mismatch(_page(URL.format(WANT)), WANT) == ""
    assert guard.stop_if_wrong(_page(URL.format(WANT)), "act_" + WANT) is True


def test_another_account_stops_with_the_spec_reason():
    with pytest.raises(profile_trouble.ProfileStop) as exc:
        guard.stop_if_wrong(_page(URL.format(OTHER)), "act_" + WANT)
    assert exc.value.code == "wrong_ad_account"
    assert exc.value.detail == f"expected act_{WANT}, opened act_{OTHER}"
    assert "no access to that ad account" in str(exc.value)


def test_an_ads_manager_page_without_act_stops():
    url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
    with pytest.raises(profile_trouble.ProfileStop) as exc:
        guard.stop_if_wrong(_page(url), "act_" + WANT)
    assert exc.value.detail == f"expected act_{WANT}, opened unknown"


def test_a_page_that_is_not_ads_manager_is_not_judged():
    """Обрыв сети, пустая вкладка, чужой хост: не `wrong_ad_account`, но и не «можно жать»."""
    for url in ("chrome-error://chromewebdata/", "about:blank", "", None,
                "https://www.facebook.com/login?next=adsmanager"):
        assert guard.stop_if_wrong(_page(url), "act_" + WANT) is False


def test_a_dead_page_is_unreadable():
    class Dead:
        @property
        def url(self):
            raise RuntimeError("closed")

    assert guard.mismatch(Dead(), "act_" + WANT).endswith("opened unknown")


def test_a_draft_without_an_account_stops():
    assert guard.mismatch(_page(URL.format(WANT)), "") != ""


# -- политика -----------------------------------------------------------------

def test_wrong_ad_account_is_blocking_with_a_guide_entry():
    name = "wrong_ad_account: expected act_1, opened act_2"
    assert failure_policy.severity(name) == failure_policy.BLOCKING
    assert "wrong_ad_account" in failure_policy.GUIDE
    assert "wrong_ad_account" in profile_trouble.ACCOUNT_STOPS
    assert retry_policy.disposition(name).action != retry_policy.RETRY_CAMPAIGN


# -- список кампаний ----------------------------------------------------------

def _flow_on(page, monkeypatch):
    f = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    f.page = page
    f.cfg = {}
    f.nav_timeout = 1000
    monkeypatch.setattr(amf, "pause_after_navigation", lambda: None)
    monkeypatch.setattr(profile_trouble, "classify_page", lambda *a, **kw: ("", ""))
    f.ready = MagicMock()
    f._ensure_campaigns_ready = f.ready
    return f


def _goto_page(land):
    page = MagicMock()
    page.url = ""

    def goto(url, **kw):
        page.url = land(url)

    page.goto.side_effect = goto
    return page


def test_goto_campaigns_stops_on_a_redirect_to_another_account(monkeypatch):
    page = _goto_page(lambda url: URL.format(OTHER))
    f = _flow_on(page, monkeypatch)
    draft = SimpleNamespace(ad_account_id="act_" + WANT, name="C1")
    with pytest.raises(profile_trouble.ProfileStop) as exc:
        f._goto_campaigns(draft, SimpleNamespace(steps_completed=[], screenshot_paths=[]))
    assert exc.value.code == "wrong_ad_account"
    f.ready.assert_not_called()


def test_goto_campaigns_continues_on_the_same_account(monkeypatch):
    page = _goto_page(lambda url: url)
    f = _flow_on(page, monkeypatch)
    draft = SimpleNamespace(ad_account_id="act_" + WANT, name="C1")
    result = SimpleNamespace(steps_completed=[], screenshot_paths=[])
    f._goto_campaigns(draft, result)
    assert "open_campaigns" in result.steps_completed


def test_goto_campaigns_rechecks_after_the_ready_reload(monkeypatch):
    """Готовность списка перезагрузила страницу, и Meta сменила кабинет."""
    page = _goto_page(lambda url: url)
    f = _flow_on(page, monkeypatch)
    f.ready.side_effect = lambda name: setattr(page, "url", URL.format(OTHER))
    draft = SimpleNamespace(ad_account_id="act_" + WANT, name="C1")
    with pytest.raises(profile_trouble.ProfileStop):
        f._goto_campaigns(draft, SimpleNamespace(steps_completed=[], screenshot_paths=[]))


def test_the_stop_name_carries_the_detail(monkeypatch):
    f = _flow_on(MagicMock(), monkeypatch)
    stop = profile_trouble.ProfileStop("wrong_ad_account", "expected act_1, opened act_2")
    assert f._record_profile_stop(stop) == "wrong_ad_account: expected act_1, opened act_2"


# -- Create в рекордере -------------------------------------------------------

def _spy_create(flow):
    """Клики по кнопке Create: страница подставная, считаем `click()` у её локатора."""
    presses: list[str] = []
    page = flow["page"]
    base = page.get_by_role.side_effect
    create = MagicMock()
    create.first = create
    create.click.side_effect = lambda *a, **kw: presses.append("create")
    create.is_visible.return_value = True
    create.bounding_box.return_value = None

    def by_role(role, name=None, **kw):
        if role == "button" and name == "Create":
            return create
        return base(role, name, **kw)

    page.get_by_role.side_effect = by_role
    return presses


def _run(flow, act):
    return dom.run_recorded_campaign_flow(
        flow["page"], campaign_name="Example", budget="10", adset_name="Set",
        creative_path="a.png", ad_title="t", ad_body="b",
        destination_url="https://example.com", expected_act=act)


def test_create_is_not_pressed_in_another_account(flow, monkeypatch):
    presses = _spy_create(flow)
    flow["page"].url = URL.format(OTHER)
    out = _run(flow, "act_" + WANT)
    assert out == "wrong_ad_account"
    assert dom.last_wrong_ad_account() == f"expected act_{WANT}, opened act_{OTHER}"
    assert presses == []


def test_create_is_pressed_in_the_same_account(flow, monkeypatch):
    presses = _spy_create(flow)
    flow["page"].url = URL.format(WANT)
    out = _run(flow, "act_" + WANT)
    assert not out.startswith("wrong_ad_account")
    assert presses and presses[0] == "create"


def test_create_is_not_pressed_when_the_account_is_unreadable(flow, monkeypatch):
    presses = _spy_create(flow)
    flow["page"].url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
    out = _run(flow, "act_" + WANT)
    assert out == "wrong_ad_account"
    assert dom.last_wrong_ad_account() == f"expected act_{WANT}, opened unknown"
    assert presses == []


def test_the_quick_retry_press_rechecks_the_account(flow, monkeypatch):
    """Первое нажатие не взялось, а до повтора Meta сменила кабинет."""
    presses = _spy_create(flow)
    page = flow["page"]
    page.url = URL.format(WANT)
    flow["took"][:] = ["nothing", "ok"]
    real_took = dom._create_took

    def took(p, list_url, timeout_s=8.0):
        res = real_took(p, list_url, timeout_s)
        page.url = URL.format(OTHER)
        return res

    monkeypatch.setattr(dom, "_create_took", took)
    out = _run(flow, "act_" + WANT)
    assert out == "wrong_ad_account"
    assert presses == ["create"], "повторный Create ушёл в чужой кабинет"


def test_the_check_runs_again_after_the_list_is_reloaded(flow, monkeypatch):
    """Окно цели не пришло, список открыт заново — и Meta открыла другой кабинет."""
    presses = _spy_create(flow)
    page = flow["page"]
    page.url = URL.format(WANT)
    flow["waits"][:] = [False, True]
    base_goto = page.goto.side_effect

    def goto(url, **kw):
        base_goto(url, **kw)
        page.url = URL.format(OTHER)

    page.goto.side_effect = goto
    out = _run(flow, "act_" + WANT)
    assert out == "wrong_ad_account"
    assert presses == ["create"], "после перезагрузки Create нажат без новой проверки"


# -- заметка в предпроверке (ACL-1604) ---------------------------------------

def test_preflight_notes_a_profile_history_that_differs_from_the_map(monkeypatch):
    from src.fb_ads_rpa import mlx_profiles
    from src.web import app as web

    web._HISTORY_ACT_CACHE.clear()
    entry = SimpleNamespace(ad_account_id="act_111", profile_id="p-1")
    monkeypatch.setattr(mlx_profiles, "ad_account_id_for", lambda pid: "act_222")
    note = web._history_act_note(entry)
    assert "act_222" in note and "act_111" in note and "wrong_ad_account" in note

    web._HISTORY_ACT_CACHE.clear()
    monkeypatch.setattr(mlx_profiles, "ad_account_id_for", lambda pid: "act_111")
    assert web._history_act_note(entry) == ""
    web._HISTORY_ACT_CACHE.clear()
    monkeypatch.setattr(mlx_profiles, "ad_account_id_for", lambda pid: "")
    assert web._history_act_note(entry) == ""


# -- ложные остановки: обрыв сети не «чужой кабинет» (ревью PR #699, B1) --------

CHROME_ERROR = "chrome-error://chromewebdata/"


def _fake_clock(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(dom, "time", SimpleNamespace(
        time=lambda: clock["now"], sleep=lambda s: clock.__setitem__("now", clock["now"] + s)))


@pytest.mark.parametrize("url", [CHROME_ERROR, "about:blank"])
def test_an_offsite_page_at_the_press_gets_no_click_and_no_account_stop(flow, monkeypatch, url):
    presses = _spy_create(flow)
    _fake_clock(monkeypatch)
    flow["page"].url = url
    out = _run(flow, "act_" + WANT)
    assert out != "wrong_ad_account"
    assert presses == []
    # восстановление шло: список открывали заново
    assert any(e[0] == "goto" for e in flow["events"])


def _reload_lands_on(flow, first_url):
    page = flow["page"]
    page.url = URL.format(WANT)
    flow["waits"][:] = [False, True]
    base_goto = page.goto.side_effect
    landed = []

    def goto(url, **kw):
        base_goto(url, **kw)
        landed.append(url)
        # первая перезагрузка списка — сбой, дальше — список нужного кабинета
        page.url = first_url if len(landed) == 1 else URL.format(WANT)

    page.goto.side_effect = goto


def test_a_reload_that_lands_on_a_blank_page_then_recovers_continues(flow, monkeypatch):
    presses = _spy_create(flow)
    _fake_clock(monkeypatch)
    _reload_lands_on(flow, "about:blank")
    out = _run(flow, "act_" + WANT)
    assert out != "wrong_ad_account"
    assert len(presses) >= 2, "после восстановления Create не нажат"


def test_a_reload_that_lands_on_chrome_error_goes_to_network_diagnosis(flow, monkeypatch):
    """Обрыв сети на перезагрузке: прежняя диагностика сети, не чужой кабинет и не слепой клик."""
    presses = _spy_create(flow)
    _fake_clock(monkeypatch)
    _reload_lands_on(flow, CHROME_ERROR)
    out = _run(flow, "act_" + WANT)
    assert out == "profile_network"
    assert presses == ["create"], "на странице ошибки сети Create нажат"


# -- мутации ревью: обе проверки должны быть подключены (N1) ---------------------

def test_the_classic_wizard_never_clicks_create_in_another_account(monkeypatch):
    f = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    f.page = _page(URL.format(OTHER))
    f.cfg = {}
    f.nav_timeout = f.action_timeout = 1000
    f._expected_act = WANT
    clicked = MagicMock()
    monkeypatch.setattr(amf, "_wait_for_ads_manager_ready", lambda *a, **kw: None)
    monkeypatch.setattr(amf, "_click_first", clicked)
    with pytest.raises(profile_trouble.ProfileStop):
        f._start_create_wizard(SimpleNamespace(steps_completed=[]))
    clicked.assert_not_called()


def test_the_classic_wizard_does_not_click_on_an_offsite_page(monkeypatch):
    f = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    f.page = _page(CHROME_ERROR)
    f.cfg = {}
    f.nav_timeout = f.action_timeout = 1000
    f._expected_act = WANT
    clicked = MagicMock()
    monkeypatch.setattr(amf, "_wait_for_ads_manager_ready", lambda *a, **kw: None)
    monkeypatch.setattr(amf, "_click_first", clicked)
    with pytest.raises(amf.AdsManagerFlowError):
        f._start_create_wizard(SimpleNamespace(steps_completed=[]))
    clicked.assert_not_called()


def test_publish_campaign_passes_the_draft_account_to_the_recorded_flow(monkeypatch):
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    monkeypatch.setenv("FB_RPA_ENGINE", "recorded_flow")
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, draft, result: None)
    from src.fb_ads_rpa import browser as br
    monkeypatch.setattr(br, "put_window_back", lambda page: None)
    seen: list = []

    def flow_run(page, **kw):
        seen.append(kw)
        return ""

    monkeypatch.setattr(amf, "run_recorded_campaign_flow", flow_run)
    flow = amf.AdsManagerFlow("page")
    draft = CampaignDraft("Campaign A", "MX", "Account", ad_account_id="act_" + WANT,
                          ad_sets=[AdSetDraft("Set A", ads=[AdDraft(ad_title="t", ad_body="b")])])
    flow.publish_campaign(draft)
    assert seen and seen[0].get("expected_act") == "act_" + WANT
