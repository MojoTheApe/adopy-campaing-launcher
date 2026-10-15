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


def test_no_objective_dialog_on_the_list_is_reloaded_even_without_loading_text(env):
    """19.09 18:12 CO_bnc Mix: окна цели нет, надписи «Loading…» тоже нет."""
    page = Page("https://adsmanager.facebook.com/adsmanager/manage/campaigns")
    assert md.recover_stuck_page(page, "objective", editor=False, force=True) is True
    assert page.reloads == 1 and env["events"][-1] == ("loading_modal_reload", True, "no_objective_dialog")


def test_force_never_reloads_an_editor(env):
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True, force=True) is False
    assert page.reloads == 0


def test_the_page_is_woken_before_it_is_reloaded(env, monkeypatch):
    # 19.09 вечер: свёрнутое окно и фоновая вкладка — перезагрузка без
    # «разбудить» снова упиралась в «Loading creation».
    from src.fb_ads_rpa import browser
    order = []
    page = Page("https://adsmanager.facebook.com/adsmanager/manage/campaigns")
    monkeypatch.setattr(browser, "wake_page", lambda p: order.append("wake") or {"minimized": True, "restored": True})
    real_reload = page.reload
    page.reload = lambda **kw: (order.append("reload"), real_reload(**kw))
    assert md.recover_stuck_page(page, "objective", editor=False, force=True) is True
    assert order == ["wake", "reload"]


def test_a_failing_wake_does_not_block_the_reload(env, monkeypatch):
    from src.fb_ads_rpa import browser

    def broken(p):
        raise RuntimeError("no cdp")

    monkeypatch.setattr(browser, "wake_page", broken)
    page = Page("chrome-error://chromewebdata/")
    assert md.recover_stuck_page(page, "ad2_before", editor=True) is True
    assert page.reloads == 1


def test_a_component_that_failed_to_load_is_reloaded(env, monkeypatch):
    """20.09 00:35 CO_bnc Mix: «Error: component failed to load» на месте
    панели плейсментов — следом отказали placements и devices_ios."""
    env.update(saved=True)
    monkeypatch.setattr(md, "_component_failed", lambda page: True)
    page = Page()
    assert md.recover_stuck_page(page, "placements", editor=True) is True
    assert page.reloads == 1 and env["events"][-1][:2] == ("loading_modal_reload", True)


def test_a_failed_component_keeps_unsaved_edits(env, monkeypatch):
    env.update(saved=False)
    monkeypatch.setattr(md, "_component_failed", lambda page: True)
    page = Page()
    assert md.recover_stuck_page(page, "placements", editor=True) is False
    assert page.reloads == 0 and env["events"] == [("loading_modal_reload", False, "unsaved_edits")]


def test_a_reload_that_times_out_falls_back_to_reloading_from_inside(env, monkeypatch):
    """20.09 00:55 CO_bnc GMB: `page.reload` не уложился в минуту дважды подряд."""
    env.update(loading=True, saved=True)
    calls = []

    class SlowPage(Page):
        def reload(self, **kw):
            calls.append("reload")
            raise RuntimeError("Page.reload: Timeout 60000ms exceeded.")

        def evaluate(self, script, *a):
            calls.append("js")
            return None

        def wait_for_selector(self, sel, **kw):
            calls.append("wait")
            return None

    page = SlowPage()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is True
    assert calls == ["reload", "js", "wait"], calls
    assert "js_reload" in env["events"][-1][2]


def test_the_pending_drafts_counter_is_read_from_the_button(env):
    """20.09 01:20: «Loading creation» держалось на аккаунтах с 10-11 черновиками."""

    class Btn(Page):
        def __init__(self, text):
            super().__init__()
            self.text = text

        def evaluate(self, script, *a):
            import re as _re
            m = _re.search(r"\((\d+)\)", self.text)
            return int(m.group(1)) if m else (0 if "Review and publish" in self.text else None)

        # ACL-1320: счётчик читается через `_eval_bounded` — ожидание с
        # таймаутом драйвера, ответ в поле `v`.
        def wait_for_function(self, script, arg=None, timeout=None):
            from types import SimpleNamespace as _NS

            got = self.evaluate(script)
            return _NS(json_value=lambda: {"v": got}, dispose=lambda: None)

    assert md._pending_drafts(Btn("Review and publish (11)")) == 11
    assert md._pending_drafts(Btn("Review and publish")) == 0
    assert md._pending_drafts(Btn("")) is None


def test_an_ssl_error_page_that_kept_the_meta_url_is_recognised(monkeypatch):
    """20.09 02:00 (профиль KLD_BFL_S15): «This site can't provide a secure
    connection / ERR_SSL_PROTOCOL_ERROR», а в адресной строке — адрес Meta."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            pg = b.new_page()
            pg.set_content("<div id='main-frame-error'>This site can&#8217;t provide a secure "
                           "connection<div>ERR_SSL_PROTOCOL_ERROR</div></div>")
            assert md._chrome_error_page(pg) == "ERR_SSL_PROTOCOL_ERROR"
            pg.set_content("<div>Ads Manager</div>")
            assert md._chrome_error_page(pg) == ""
        finally:
            b.close()


def test_long_text_is_inserted_after_a_few_real_keystrokes(monkeypatch):
    """20.09, решение пользователя: посимвольный набор стоил ~45 минут на круге.
    Начало строки печатаем нажатиями, длинный остаток вставляем целиком."""
    typed, inserted = [], []

    class Keyboard:
        def type(self, text, delay=None):
            typed.append(text)

        def insert_text(self, text):
            inserted.append(text)

    class Pg:
        keyboard = Keyboard()

    monkeypatch.delenv("FB_RPA_HUMAN_TYPING", raising=False)
    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    md._type_human(Pg(), "Invest like a pro with Banco General today")
    assert "".join(typed) == "Invest l", typed
    assert inserted == ["ike a pro with Banco General today"], inserted


def test_the_old_per_character_typing_can_be_switched_back_on(monkeypatch):
    typed, inserted = [], []

    class Keyboard:
        def type(self, text, delay=None):
            typed.append(text)

        def insert_text(self, text):
            inserted.append(text)

    class Pg:
        keyboard = Keyboard()

    monkeypatch.setenv("FB_RPA_HUMAN_TYPING", "1")
    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    md._type_human(Pg(), "Invest like a pro")
    assert typed == ["Invest like a pro"] and inserted == []
