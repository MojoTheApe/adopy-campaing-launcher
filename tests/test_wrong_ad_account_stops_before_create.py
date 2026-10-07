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
    guard.stop_if_wrong(_page(URL.format(WANT)), "act_" + WANT)  # без исключения


def test_another_account_stops_with_the_spec_reason():
    with pytest.raises(profile_trouble.ProfileStop) as exc:
        guard.stop_if_wrong(_page(URL.format(OTHER)), "act_" + WANT)
    assert exc.value.code == "wrong_ad_account"
    assert exc.value.detail == f"expected act_{WANT}, opened act_{OTHER}"
    assert "no access to that ad account" in str(exc.value)


def test_an_unreadable_account_stops():
    for url in ("https://adsmanager.facebook.com/adsmanager/manage/campaigns", "", None):
        with pytest.raises(profile_trouble.ProfileStop) as exc:
            guard.stop_if_wrong(_page(url), "act_" + WANT)
        assert exc.value.detail == f"expected act_{WANT}, opened unknown"


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
