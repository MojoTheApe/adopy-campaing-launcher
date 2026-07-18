"""Unit tests for campaign-open forbidden zones + URL detect (no browser)."""
from __future__ import annotations

from src.fb_ads_rpa import wizard_guard as wg


def test_forbidden_about_this_update():
    reason = wg.forbidden_click_reason("About this update")
    assert reason
    assert "measurement" in reason.lower() or "attribution" in reason.lower() or "news" in reason.lower()


def test_forbidden_about_daily_budget():
    assert wg.forbidden_click_reason("About daily budget")


def test_allowed_next_and_continue():
    assert wg.forbidden_click_reason("Next") is None
    assert wg.forbidden_click_reason("Continue") is None
    assert wg.forbidden_click_reason("Website") is None
    assert wg.forbidden_click_reason("Cancel") is None


def test_forbidden_match_close():
    assert wg.forbidden_match_reason({"own_text": "Close"})
    assert wg.forbidden_match_reason({"own_text": "Next"}) is None


def test_url_left_ads_manager_news():
    assert wg.url_left_ads_manager(
        "https://www.facebook.com/business/news/click-attribution"
    )
    assert wg.url_left_ads_manager(
        "https://www.facebook.com/business/help/123"
    )


def test_url_stays_on_ads_manager():
    assert not wg.url_left_ads_manager(
        "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123"
    )
    assert not wg.url_left_ads_manager(
        "https://www.facebook.com/adsmanager/manage/campaigns/edit/standalone?act=1"
    )


def test_is_ads_manager_url():
    assert wg.is_ads_manager_url(
        "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
    )
    assert not wg.is_ads_manager_url(
        "https://www.facebook.com/business/news/click-attribution"
    )
