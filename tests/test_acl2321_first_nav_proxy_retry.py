"""ACL-2321: прокси/TLS-сбой на первой навигации — один повтор и понятная ошибка."""
from __future__ import annotations

import pytest

from src.fb_api import cdp as cdp_mod
from src.fb_api import launch

SSL = "net::ERR_SSL_PROTOCOL_ERROR"
FB = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
PROXY_ADDR = "203.0.113.7:8080"


class _Cdp:
    def __init__(self, errors):
        self.errors = list(errors)
        self.urls = []

    def call(self, method, params=None, *, session_id="", timeout=10.0):
        if method == "Page.navigate":
            self.urls.append(params["url"])
            err = self.errors.pop(0) if self.errors else ""
            return {"frameId": "F", **({"errorText": err} if err else {})}
        if method == "Target.createTarget":
            return {"targetId": "T"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S"}
        return {}

    def close(self):
        pass

    def on_event(self, *a, **k):
        pass


def _tab(errors, monkeypatch):
    events, beats, slept = [], [], []
    monkeypatch.setattr(launch, "_record", lambda e: events.append(e))
    from src.fb_ads_rpa import run_telemetry
    monkeypatch.setattr(run_telemetry, "beat", lambda: beats.append(1))
    monkeypatch.setenv(launch.PROXY_PROBE_ENV, "off")
    tab = object.__new__(cdp_mod.PageTab)
    tab._first_nav_cold = False
    tab._first_nav_done = False
    tab._navigation_url = ""
    tab._sleep = slept.append
    cdp = _Cdp(errors)
    tab.call = lambda method, params=None, **kw: cdp.call(method, params, **kw)
    return tab, cdp, events, beats, slept


@pytest.mark.parametrize("code", ["ERR_SSL_PROTOCOL_ERROR", "ERR_PROXY_CONNECTION_FAILED",
                                  "ERR_TUNNEL_CONNECTION_FAILED", "ERR_TIMED_OUT"])
def test_proxy_error_at_first_navigation_retries_once_then_stops_with_proxy_message(monkeypatch, code):
    tab, cdp, events, beats, slept = _tab([f"net::{code}"] * 2, monkeypatch)
    with pytest.raises(cdp_mod.ProxyNotConnectedError) as info:
        tab.navigate(FB)
    assert str(info.value) == cdp_mod.proxy_not_connected_message(f"ERR_{code[4:]}")
    assert "Check the proxy of this profile in Multilogin" in str(info.value)
    assert "Nothing was sent by this attempt." in str(info.value)
    if code == "ERR_SSL_PROTOCOL_ERROR":
        assert "(SSL error)" in str(info.value)
    else:
        assert f"(network error {code})" in str(info.value) and "SSL" not in str(info.value)
    assert cdp.urls == [FB, FB]
    assert sum(slept) == launch.PROXY_NET_RETRY_S
    assert events == [{"event": "fb_api_first_nav_proxy_error", "code": code, "retried": True,
                       "outcome": "failed", "retry_code": code}]


def test_success_on_the_retry_continues(monkeypatch):
    tab, cdp, events, beats, slept = _tab([SSL, ""], monkeypatch)
    tab.navigate(FB)
    assert cdp.urls == [FB, FB] and tab._navigation_url == FB
    assert events[0]["outcome"] == "recovered" and events[0]["code"] == "ERR_SSL_PROTOCOL_ERROR"


def test_job_stays_alive_during_the_pause(monkeypatch):
    tab, cdp, events, beats, slept = _tab([SSL, ""], monkeypatch)
    tab.navigate(FB)
    # Пауза нарезана: сторож тишины получает beat между кусками, а не одну 30-секундную дыру.
    assert len(slept) >= 6 and max(slept) <= cdp_mod._PAUSE_BEAT_S
    assert len(beats) == len(slept)


def test_only_the_first_navigation_is_retried(monkeypatch):
    tab, cdp, events, beats, slept = _tab(["", SSL], monkeypatch)
    tab.navigate(FB)
    with pytest.raises(cdp_mod.PageError, match="ERR_SSL_PROTOCOL_ERROR") as info:
        tab.navigate(FB)                      # позднее (после возможной записи) — без повтора
    assert not isinstance(info.value, cdp_mod.ProxyNotConnectedError)
    assert cdp.urls == [FB, FB] and slept == [] and events == []


def test_other_navigation_errors_are_not_retried(monkeypatch):
    tab, cdp, events, beats, slept = _tab(["net::ERR_NAME_NOT_RESOLVED"], monkeypatch)
    with pytest.raises(cdp_mod.PageError, match="ERR_NAME_NOT_RESOLVED"):
        tab.navigate(FB)
    assert cdp.urls == [FB] and slept == [] and events == []


def test_no_proxy_address_in_telemetry_or_message(monkeypatch):
    tab, cdp, events, beats, slept = _tab([SSL] * 2, monkeypatch)
    with pytest.raises(cdp_mod.ProxyNotConnectedError) as info:
        tab.navigate(FB)
    blob = repr(events) + str(info.value)
    assert PROXY_ADDR not in blob and "http" not in repr(events)
    assert set(events[0]) == {"event", "code", "retried", "outcome", "retry_code"}


def test_read_failure_shows_the_plain_proxy_message():
    from src.analysis_agent import account_spend_api as api

    msg = cdp_mod.proxy_not_connected_message("ERR_TIMED_OUT")
    assert api._failure("acc", cdp_mod.ProxyNotConnectedError(msg)) == (msg, "")


def test_retry_pause_is_capped_by_the_warm_prepare_budget(monkeypatch):
    from src.fb_api import warm

    tab, cdp, events, beats, slept = _tab([SSL, ""], monkeypatch)
    monkeypatch.setattr(cdp_mod.time, "monotonic", lambda: 1000.0)
    token = warm._PREPARE_DEADLINE.set(1000.0 + 25.0)     # left 25 s, reserve 15 s -> 10 s pause
    try:
        tab.navigate(FB)
    finally:
        warm._PREPARE_DEADLINE.reset(token)
    assert sum(slept) == 25.0 - cdp_mod._RETRY_NAV_RESERVE_S
    assert cdp.urls == [FB, FB]


def test_no_budget_left_means_no_pause_but_still_the_proxy_message(monkeypatch):
    from src.fb_api import warm

    tab, cdp, events, beats, slept = _tab([SSL, SSL], monkeypatch)
    monkeypatch.setattr(cdp_mod.time, "monotonic", lambda: 1000.0)
    token = warm._PREPARE_DEADLINE.set(1005.0)
    try:
        with pytest.raises(cdp_mod.ProxyNotConnectedError):
            tab.navigate(FB)
    finally:
        warm._PREPARE_DEADLINE.reset(token)
    assert slept == []
