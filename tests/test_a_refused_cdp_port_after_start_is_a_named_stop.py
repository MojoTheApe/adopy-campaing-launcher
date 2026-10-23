# -*- coding: utf-8 -*-
"""Отказ порта CDP сразу после старта профиля через MLX (ACL-1403).

Sentry PYTHON-4E (30.09, comp38339): MLX вернул порт, а `connect_over_cdp`
получил ECONNREFUSED — голый RuntimeError «job_exception». Теперь: короткое
ожидание порта, затем `ProfileStop(profile_cdp_refused)` с английским текстом.
Только моки: ни Multilogin, ни браузера.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import browser, failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402

REFUSED = ("BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:64534"
           " Call log: retrieving websocket url from http://127.0.0.1:64534")


def _provider():
    prov = browser.MultiloginAttachOnlyProvider.__new__(browser.MultiloginAttachOnlyProvider)
    prov.cancel_check = None
    return prov


def _started():
    return SimpleNamespace(port="64534", ws_endpoint="")


@pytest.fixture
def slept(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(browser.time, "sleep", lambda s: pauses.append(s))
    return pauses


def test_persistent_refusal_is_a_named_stop_after_bounded_waits(monkeypatch, slept):
    calls: list[str] = []

    def refuse(**kw):
        calls.append(kw["cdp_url"])
        raise RuntimeError(REFUSED)

    monkeypatch.setattr(browser, "_connect_playwright", refuse)
    with pytest.raises(pt.ProfileStop) as stop:
        _provider()._connect_started(_started(), "p-1")
    assert stop.value.code == pt.CDP_REFUSED
    assert "did not accept the connection" in str(stop.value)
    assert "64534" in stop.value.detail
    assert len(calls) == len(browser.CDP_PORT_WAIT_S) + 1
    assert slept == list(browser.CDP_PORT_WAIT_S)


def test_startup_race_succeeds_on_a_later_try(monkeypatch, slept):
    attempts = {"n": 0}
    sentinel = object()

    def flaky(**kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError(REFUSED)
        return sentinel

    monkeypatch.setattr(browser, "_connect_playwright", flaky)
    assert _provider()._connect_started(_started(), "p-1") is sentinel
    assert attempts["n"] == 3 and len(slept) == 2


def test_other_errors_are_not_retried_or_renamed(monkeypatch, slept):
    def boom(**kw):
        raise RuntimeError("Timeout 30000ms exceeded")

    monkeypatch.setattr(browser, "_connect_playwright", boom)
    with pytest.raises(RuntimeError, match="Timeout"):
        _provider()._connect_started(_started(), "p-1")
    assert slept == []


def test_stop_button_ends_the_wait(monkeypatch, slept):
    monkeypatch.setattr(browser, "_connect_playwright",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError(REFUSED)))
    prov = _provider()
    prov.cancel_check = lambda: True
    with pytest.raises(pt.ProfileStop):
        prov._connect_started(_started(), "p-1")
    assert slept == []


def test_the_stop_is_blocking_and_known_to_triage():
    import json
    assert pt.CDP_REFUSED in pt.ACCOUNT_STOPS
    assert pt.CDP_REFUSED in retry_policy.BLOCKING_REVIEW_STEPS
    assert failure_policy.severity(pt.CDP_REFUSED) == failure_policy.BLOCKING
    assert pt.CDP_REFUSED in failure_policy.GUIDE
    known = json.loads((ROOT / "config" / "sentry_triage_map.json").read_text(encoding="utf-8"))["known"]
    assert any(r["step"] == pt.CDP_REFUSED and r["task"] == "ACL-1403" for r in known)
    assert acl_sentry._reason_kind(str(pt.ProfileStop(pt.CDP_REFUSED))) == "cdp-refused"


def test_job_exception_event_carries_the_machine_tag(monkeypatch):
    import sentry_sdk

    tags: dict = {}
    scope = SimpleNamespace(set_tag=lambda k, v: tags.__setitem__(k, v), set_context=lambda *a: None)

    class _Ctx:
        def __enter__(self):
            return scope

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    monkeypatch.setitem(acl_sentry._machine_tags, "machine", "comp38339")
    monkeypatch.setattr(sentry_sdk, "new_scope", lambda: _Ctx())
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda e: "evt")
    assert acl_sentry.report_job_exception(RuntimeError("x"), phase="publish") == "evt"
    assert tags["machine"] == "comp38339"
