"""Реальный owned-tab путь с синтетическими контекстами, без живого браузера."""
import json
from pathlib import Path

import pytest

from src.fb_api.cdp import CdpError, PageTab


class Browser:
    def __init__(self, pages, *, contexts=(), fail="", expected=None):
        self.pages = pages
        self.contexts = contexts
        self.fail = fail
        self.expected = expected
        self.created = None
        self.sent = []
        self.closed_targets = []
        self.closed = False

    def call(self, method, params=None, *, session_id="", timeout=10):
        params = params or {}
        self.sent.append((method, params, session_id))
        if method == self.fail:
            raise CdpError("private-context-value https://private.invalid/?token=synthetic-secret")
        if method == "Target.getTargets":
            rows = list(self.pages)
            if self.created is not None:
                rows.append({"targetId": "owned", "type": "page", **self.created})
            return {"targetInfos": rows}
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": list(self.contexts)}
        if method == "Target.createTarget":
            self.created = dict(params)
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            return {"sessionId": "owned-session"}
        if method == "Page.navigate":
            if self.created.get("browserContextId") != self.expected:
                return {"errorText": "net::ERR_SSL_PROTOCOL_ERROR"}
            return {"frameId": "frame"}
        if method == "Target.closeTarget":
            self.closed_targets.append(params["targetId"])
        return {}

    def close(self):
        self.closed = True


def page(context=None, *, target="human", kind="page"):
    return {"targetId": target, "type": kind,
            **({"browserContextId": context} if context is not None else {})}


def open_tab(browser):
    return PageTab.open("http://127.0.0.1:1", connect=lambda *_a, **_kw: browser)


def test_nondefault_profile_reaches_meta_in_its_context_and_keeps_human_tab():
    incident = json.loads((Path(__file__).parent / "fixtures" / "ads_manager" /
                           "fb_api_owned_context_2026_10_26.json").read_text(encoding="utf-8"))
    browser = Browser(incident["synthetic_target_infos"], expected=incident["synthetic_expected_context"])
    tab = open_tab(browser)
    try:
        tab.navigate("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=12345")
        assert browser.created["browserContextId"] == "profile-context"
        assert not browser.created.get("background")
    finally:
        tab.close()
    assert browser.closed_targets == ["owned"]


def test_default_profile_preserves_default_without_creating_context():
    browser = Browser([page()])
    tab = open_tab(browser)
    tab.navigate("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=12345")
    assert browser.created == {"url": "about:blank"}
    assert not any(method == "Target.createBrowserContext" for method, _, _ in browser.sent)
    tab.close()


@pytest.mark.parametrize("contexts,expected", [([], None)])
def test_no_pages_uses_only_unambiguous_browser_context(contexts, expected):
    browser = Browser([], contexts=contexts, expected=expected)
    tab = open_tab(browser)
    tab.navigate("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=12345")
    assert browser.created.get("browserContextId") == expected
    tab.close()
    assert browser.closed_targets == []  # последняя вкладка остаётся открытой


@pytest.mark.parametrize("pages,contexts", [
    ([page("first"), page("second", target="other")], []),
    ([page(), page("profile", target="other")], []),
    ([], ["first", "second"]),
])
def test_ambiguous_contexts_stop_before_target_creation(pages, contexts):
    browser = Browser(pages, contexts=contexts)
    with pytest.raises(CdpError, match="multiple browser contexts"):
        open_tab(browser)
    assert browser.created is None
    assert browser.closed and not browser.closed_targets


@pytest.mark.parametrize("failed_method,pages", [
    ("Target.getTargets", [page("profile")]),
    ("Target.getBrowserContexts", []),
])
def test_discovery_failure_stops_without_leaking_raw_error(failed_method, pages):
    browser = Browser(pages, fail=failed_method)
    with pytest.raises(CdpError) as exc:
        open_tab(browser)
    assert "browser context" in str(exc.value)
    assert "private" not in str(exc.value) and "synthetic-secret" not in str(exc.value)
    assert browser.created is None and browser.closed


def test_same_context_multiple_pages_ignore_nonpage_targets():
    browser = Browser([page("profile"), page("profile", target="second"),
                       page("foreign-worker", target="worker", kind="service_worker")], expected="profile")
    tab = open_tab(browser)
    assert browser.created["browserContextId"] == "profile"
    tab.close()
    assert browser.closed_targets == ["owned"]


def test_attach_failure_cleans_up_only_new_owned_target():
    browser = Browser([page("profile")], fail="Target.attachToTarget")
    with pytest.raises(CdpError):
        open_tab(browser)
    assert browser.closed_targets == ["owned"] and browser.closed


def test_context_diagnostics_contain_counts_and_mode_only(monkeypatch):
    from src.fb_api import launch
    events = []
    monkeypatch.setattr(launch, "_record", events.append)
    browser = Browser([page("private-profile-context")], expected="private-profile-context")
    tab = open_tab(browser)
    event = next(row for row in events if row.get("event") == "fb_api_tab_context")
    assert event["context_kind"] == "nondefault" and event["page_count"] == 1
    assert "private-profile-context" not in json.dumps(events)
    tab.close()


def test_no_page_does_not_assume_sole_named_context_is_the_profile():
    browser = Browser([], contexts=["sole-profile"])
    with pytest.raises(CdpError, match="open Ads Manager"):
        open_tab(browser)
    assert browser.created is None and browser.closed


@pytest.mark.parametrize("reply", [
    {}, {"targetInfos": {}}, {"targetInfos": [None]},
    {"targetInfos": [{"type": "page", "browserContextId": None}]},
    {"targetInfos": [{"type": "page", "browserContextId": "private\ncontext"}]},
])
def test_malformed_discovery_stops_before_opening_target(reply):
    class Malformed(Browser):
        def call(self, method, *args, **kwargs):
            if method == "Target.getTargets":
                return reply
            return super().call(method, *args, **kwargs)
    browser = Malformed([])
    with pytest.raises(CdpError, match="could not determine"):
        open_tab(browser)
    assert browser.created is None and browser.closed


def test_discovery_timeout_keeps_named_bounded_error_without_raw_text():
    from src.fb_api.cdp import CdpTimeout
    class Slow(Browser):
        def call(self, method, *args, **kwargs):
            if method == "Target.getTargets":
                raise TimeoutError("private-context-value")
            return super().call(method, *args, **kwargs)
    browser = Slow([])
    with pytest.raises(CdpTimeout, match="Target.getTargets") as exc:
        open_tab(browser)
    assert "private" not in str(exc.value)
    assert browser.created is None and browser.closed


def test_page_setup_failure_closes_only_new_owned_target():
    browser = Browser([page("profile")], fail="Page.enable")
    with pytest.raises(CdpError):
        open_tab(browser)
    assert browser.created["browserContextId"] == "profile"
    assert browser.closed_targets == ["owned"] and browser.closed
