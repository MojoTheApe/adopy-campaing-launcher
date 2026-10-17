# -*- coding: utf-8 -*-
"""Publish останавливается понятно, когда приложение Multilogin не запущено (ACL-1401).

Sentry PYTHON-4A (30.09, comp38339, FacebookDoc19): локальный лаунчер
Multilogin (`launcher.mlx.yt:45001`) отказал в соединении — «[WinError 10061]
No connection could be made because the target machine actively refused it».
`requests.ConnectionError` из `multilogin_launcher.start_profile` поднимался
через `_ensure_browser` голым `RuntimeError` (attach-only), и задание падало
как `job_exception` «RuntimeError: unknown».

Теперь это остановка аккаунта `multilogin_not_running` с английским текстом
«start the Multilogin app, then press Publish again», событием Sentry со
своим шагом и строкой в карте разбора. Повторного запуска и простого
браузера нет.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa import profile_trouble as pt  # noqa: E402
from src.fb_ads_rpa import rpa_publisher as rp  # noqa: E402

LAUNCHER_URL = ("https://launcher.mlx.yt:45001/api/v2/profile/f/f-1/p/p-1/start"
                "?automation_type=playwright&headless_mode=false")
REFUSED = ("HTTPSConnectionPool(host='launcher.mlx.yt', port=45001): Max retries exceeded "
           "with url: /api/v2/profile/f/f-1/p/p-1/start?automation_type=playwright "
           "(Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object>: "
           "Failed to establish a new connection: [WinError 10061] No connection could be "
           "made because the target machine actively refused it'))")


def _refused() -> requests.ConnectionError:
    return requests.ConnectionError(REFUSED, request=requests.Request("GET", LAUNCHER_URL).prepare())


class _FakeProvider:
    """Провайдер, чей запуск профиля упирается в выключенный лаунчер."""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.opens = 0
        self.cancel_check = None

    def open(self, entry, account_label):
        self.opens += 1
        raise self.exc

    def close(self, *a, **kw):
        pass


_PID = "550e8400-e29b-41d4-a716-446655440000"


def _publisher(monkeypatch, exc: BaseException, *, attach_only: bool = True):
    pub = rp.RpaPublisher.__new__(rp.RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._entry = None
    pub._built_at = None
    pub._account_stop = None
    pub._provider = _FakeProvider(exc)
    # ACL-1853: публикатор знает провайдера, профиль — настоящий UUID.
    pub.browser_provider_name = "multilogin"
    monkeypatch.setattr(rp, "require_profile_lease", lambda key: None)
    monkeypatch.setattr(rp, "profile_key_for", lambda label, **kw: _PID)
    monkeypatch.setattr(pub, "_resolve_entry", lambda label: SimpleNamespace(
        profile_id=_PID, folder_id="f-1", multilogin_instance="A"))
    monkeypatch.setattr(pub, "_reset_browser", lambda **kw: None)
    monkeypatch.setattr(pub, "_profile_already_running", lambda entry: False)
    monkeypatch.setattr(rp, "get_mlx_attach_only", lambda: attach_only)
    fallbacks: list = []
    monkeypatch.setattr(pub, "_open_multilogin_direct",
                        lambda *a: fallbacks.append("direct") or (_ for _ in ()).throw(AssertionError("direct")))
    monkeypatch.setattr(pub, "_open_playwright_profile",
                        lambda *a: fallbacks.append("plain") or (_ for _ in ()).throw(AssertionError("plain")))
    return pub, fallbacks


def _wrapped_like_browser_open() -> RuntimeError:
    """Как в стеке PYTHON-4A: ConnectionError → RuntimeError выше по цепочке."""
    try:
        try:
            raise _refused()
        except requests.ConnectionError as inner:
            raise RuntimeError(str(inner)) from inner
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize("attach_only", [True, False])
@pytest.mark.parametrize("make_exc", [_refused, _wrapped_like_browser_open])
def test_refused_launcher_is_a_named_stop(monkeypatch, attach_only, make_exc):
    pub, fallbacks = _publisher(monkeypatch, make_exc(), attach_only=attach_only)
    with pytest.raises(pt.ProfileStop) as stop:
        pub._ensure_browser("FacebookDoc19")
    assert stop.value.code == pt.MLX_NOT_RUNNING
    assert "connection_refused" in stop.value.detail
    assert "start the Multilogin app" in str(stop.value)
    assert pub._provider.opens == 1  # no repeated profile starts
    assert fallbacks == []  # no DirectCDP, never a plain browser


def test_connect_timeout_to_the_launcher_is_the_same_stop(monkeypatch):
    exc = requests.ConnectTimeout("HTTPSConnectionPool(host='launcher.mlx.yt', port=45001): "
                                  "Max retries exceeded (connect timeout=120)")
    pub, _ = _publisher(monkeypatch, exc)
    with pytest.raises(pt.ProfileStop) as stop:
        pub._ensure_browser("FacebookDoc19")
    assert stop.value.code == pt.MLX_NOT_RUNNING
    assert "connect_timeout" in stop.value.detail


@pytest.mark.parametrize("exc", [
    requests.ReadTimeout("HTTPSConnectionPool(host='launcher.mlx.yt', port=45001): Read timed out."),
    requests.ConnectionError("HTTPSConnectionPool(host='api.multilogin.com', port=443): refused"),
    RuntimeError("MLX API: HTTP 500 internal error"),
])
def test_other_failures_are_not_called_multilogin_not_running(exc):
    assert pt.launcher_unreachable(exc) == ""


def test_publish_ends_with_the_message_and_reports_the_stop(monkeypatch):
    from src.fb_ads_rpa import sentry_signal
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    pub, _ = _publisher(monkeypatch, _wrapped_like_browser_open())
    sent: list = []
    monkeypatch.setattr(sentry_signal, "report_campaign_stop",
                        lambda step, **kw: sent.append((step, kw)) or "evt")
    drafts = [CampaignDraft(n, "MX", "FacebookDoc19",
                            ad_sets=[AdSetDraft("Set", ads=[AdDraft(ad_title="t", ad_body="b")])])
              for n in ("A", "B")]
    results = [pub.publish_draft(d) for d in drafts]

    assert pub._provider.opens == 1  # the second campaign does not start the profile again
    for got in results:
        assert got.success is False
        assert got.blocking_failures == [pt.MLX_NOT_RUNNING]
        assert pt.MESSAGES[pt.MLX_NOT_RUNNING] in got.error
    assert [s for s, _ in sent] == [pt.MLX_NOT_RUNNING]
    assert sent[0][1]["account"] == "FacebookDoc19"


def test_sentry_tags_step_and_reason(monkeypatch):
    """Событие идёт со своим шагом и видом причины, не «RuntimeError: unknown»."""
    from src.fb_ads_rpa import sentry_signal

    captured: dict = {}
    monkeypatch.setattr(acl_sentry, "report_step_failure",
                        lambda step, why, **kw: captured.update(step=step, why=why, **kw) or "evt")
    monkeypatch.setattr(sentry_signal, "_seen", lambda step, run_id: {})
    assert sentry_signal.report_campaign_stop(pt.MLX_NOT_RUNNING, campaign="A", account="FacebookDoc19",
                                              attempt=1, attach_section=False) == "evt"
    assert captured["step"] == pt.MLX_NOT_RUNNING
    assert captured["kind"] == sentry_signal.CAMPAIGN_STOP
    assert captured["account"] == "FacebookDoc19"
    assert acl_sentry._reason_kind(str(pt.ProfileStop(pt.MLX_NOT_RUNNING))) == "mlx-not-running"


def test_the_stop_is_blocking_and_known_to_triage():
    assert pt.MLX_NOT_RUNNING == "multilogin_not_running"
    assert pt.MLX_NOT_RUNNING in pt.ACCOUNT_STOPS
    assert pt.MLX_NOT_RUNNING in retry_policy.BLOCKING_REVIEW_STEPS
    assert retry_policy.disposition(pt.MLX_NOT_RUNNING).action == retry_policy.BLOCK_AND_REVIEW
    assert failure_policy.severity(pt.MLX_NOT_RUNNING) == failure_policy.BLOCKING
    assert pt.MESSAGES[pt.MLX_NOT_RUNNING] in failure_policy.reason(pt.MLX_NOT_RUNNING)
    assert pt.MLX_NOT_RUNNING in failure_policy.GUIDE
    known = json.loads((ROOT / "config" / "sentry_triage_map.json").read_text(encoding="utf-8"))["known"]
    row = next(r for r in known if r["step"] == pt.MLX_NOT_RUNNING)
    assert row["task"] == "ACL-1401" and "buyer action" in row["note"]


def test_the_dashboard_shows_the_message():
    from src.web import publish_progress

    assert publish_progress._BLOCKERS[pt.MLX_NOT_RUNNING] == pt.MESSAGES[pt.MLX_NOT_RUNNING]
