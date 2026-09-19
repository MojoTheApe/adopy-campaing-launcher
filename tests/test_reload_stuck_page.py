"""ACL-1233: зависшая загрузка Meta и страница ошибки Chrome — перезагрузка."""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md


class Page:
    def __init__(self, url="https://adsmanager.facebook.com/adsmanager/manage/ads/edit"):
        self.url = url
        self.reloads = 0

    def reload(self, **kw):
        self.reloads += 1
        if self.url.startswith("chrome-error://"):
            self.url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit"


@pytest.fixture
def env(monkeypatch):
    state = {"loading": False, "saved": False, "clears_after": None, "calls": 0, "events": []}

    def loading(page):
        state["calls"] += 1
        if state["clears_after"] is not None and state["calls"] > state["clears_after"]:
            return False
        return state["loading"]

    monkeypatch.setattr(md, "_loading_visible", loading)
    monkeypatch.setattr(md, "_all_edits_saved", lambda page: state["saved"])
    monkeypatch.setattr(md, "_chrome_error_page",
                        lambda page: "ERR_SSL_PROTOCOL_ERROR" if page.url.startswith("chrome-error://") else "")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "LOADING_STUCK_S", 0.05)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok, **kw: state["events"].append((step, ok, kw.get("note"))))
    return state


def test_a_chrome_error_page_is_always_reloaded(env):
    page = Page("chrome-error://chromewebdata/")
    assert md.recover_stuck_page(page, "ad2_before", editor=True) is True
    assert page.reloads == 1
    assert env["events"] == [("error_page_reload", True, "error_page:ERR_SSL_PROTOCOL_ERROR")]


def test_a_stuck_loading_modal_with_saved_edits_is_reloaded(env):
    env.update(loading=True, saved=True)
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is True
    assert page.reloads == 1 and env["events"][-1][:2] == ("loading_modal_reload", True)


def test_unsaved_edits_are_never_thrown_away(env):
    env.update(loading=True, saved=False)
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is False
    assert page.reloads == 0
    assert env["events"] == [("loading_modal_reload", False, "unsaved_edits")]


def test_a_loading_modal_that_clears_is_left_alone(env):
    env.update(loading=True, saved=True, clears_after=2)
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is False
    assert page.reloads == 0 and env["events"] == []


def test_the_campaign_list_has_nothing_to_lose_so_it_reloads(env):
    env.update(loading=True, saved=False)
    page = Page("https://adsmanager.facebook.com/adsmanager/manage/campaigns")
    assert md.recover_stuck_page(page, "objective", editor=False) is True
    assert page.reloads == 1


def test_a_healthy_page_is_not_touched(env):
    page = Page()
    assert md.recover_stuck_page(page, "ad3_before", editor=True) is False
    assert page.reloads == 0 and env["events"] == []
