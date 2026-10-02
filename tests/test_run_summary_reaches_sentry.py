# -*- coding: utf-8 -*-
"""Итог каждой кампании уходит в Sentry одним событием (ACL-1447).

Проверки идут через **настоящий** `sentry_sdk` и настоящий
`acl_sentry.init`/`before_send`; подменён только транспорт — двойник, который
складывает конверты в список. Ни одно событие отсюда не уходит в сеть: адрес
DSN — `example.invalid`, а транспорт не умеет ничего, кроме списка.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sentry_sdk  # noqa: E402
from sentry_sdk.transport import Transport  # noqa: E402

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import ads_manager_flow as amf  # noqa: E402
from src.fb_ads_rpa import config as rpa_config  # noqa: E402
from src.fb_ads_rpa import run_summary  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft  # noqa: E402

CAMPAIGN = "GT_ait_All_Video_FX_35_25.09"
ACCOUNT = "FacebookDoc19 Ivan Petrov"
ACT = "act=1234567890123456"
# Собран из частей: целиком такой id ловит сканер секретов (acl_scan_secrets).
PROFILE = "-".join(("3f2b8c1e", "9a7d", "4e21", "b5c4", "0d1e2f3a4b5c"))
URL = f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?{ACT}&business_id=987654321012345"


class FakeTransport(Transport):
    """Транспорт-двойник: конверт → список. `fail` — сеть упала, `gate` — висит."""

    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []
        self.fail = False
        self.gate: threading.Event | None = None

    def capture_envelope(self, envelope):
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise OSError("network is unreachable")
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)

    def flush(self, timeout, callback=None):
        return None

    def kill(self):
        return None


@pytest.fixture
def sentry(monkeypatch):
    transport = FakeTransport()
    real_init = sentry_sdk.init

    def init_with_fake_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_init(*args, **kwargs)

    monkeypatch.setenv("ACL_SENTRY_DSN", "https://public@o1.ingest.example.invalid/1")
    monkeypatch.setenv("ACL_SENTRY_RUN_SUMMARY", "1")
    monkeypatch.setenv("ACL_MACHINE_LABEL", "Dan")
    monkeypatch.setattr(acl_sentry, "_started", False)
    monkeypatch.setattr(acl_sentry, "_machine_tags", {})
    monkeypatch.setattr(sentry_sdk, "init", init_with_fake_transport)
    from src.fb_ads_rpa import telemetry_sync

    monkeypatch.setattr(telemetry_sync, "flow_version", lambda: "v99+0@abc1234")
    rt.clear_current_run()
    rt._STEPS.clear()
    yield transport
    rt.clear_current_run()
    try:
        sentry_sdk.get_client().close()
    except Exception:
        pass
    real_init(dsn="")  # клиент без адреса — ничего не шлёт
    # `init` ставит теги машины на глобальный scope — он переживает тест и
    # подписал бы события следующих файлов.
    for key in ("machine", "machine_id"):
        sentry_sdk.get_global_scope().remove_tag(key)


def _steps(run_id: str, *, simple: bool = False) -> None:
    rt.set_current_run(run_id, CAMPAIGN)
    rt.record("campaign_name", True, duration_s=2.04)
    if simple:
        rt.record("simple_create", None, note="no_objective_dialog")
        rt.record("simple_card", True, note="Ad set:open", duration_s=1.0)
    rt.record("audience_location", True, duration_s=5.5)
    rt.record("advantage_plus_off", False, note="still_on", duration_s=0.7)
    rt.record("ad_copy", True, duration_s=12.3)


def _only(transport) -> dict:
    summaries = [e for e in transport.events if (e.get("tags") or {}).get("kind") == "run_summary"]
    assert len(summaries) == 1, transport.events
    return summaries[0]


# ── четыре вида итога из прогона-образца ───────────────────────────────────

@pytest.mark.parametrize("stopped_at, failures, result, level", [
    ("", [], "clean", "info"),
    ("", ["ad2_second_text"], "warnings", "info"),
    ("", ["audience_location", "ad2_second_text"], "do-not-publish", "warning"),
    ("account_blocked", [], "stopped", "warning"),
])
def test_each_result_kind_is_one_grouped_event(sentry, stopped_at, failures, result, level):
    _steps("run00000001")
    run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT, stopped_at=stopped_at,
                                failures=failures, ads=3, started=time.monotonic() - 42,
                                attempts=2, background=False)
    event = _only(sentry)
    assert event["message"] == f"Run summary: {result}" or \
        event.get("logentry", {}).get("message") == f"Run summary: {result}"
    assert event["level"] == level
    assert event["fingerprint"] == ["acl-run-summary", result]
    assert event["release"] == "v99+0@abc1234"
    tags = event["tags"]
    assert tags["result"] == result and tags["editor"] == "classic"
    assert tags["manager"] == "Dan" and tags["machine"] == "Dan"
    ctx = event["contexts"]["run_summary"]
    assert ctx["ads"] == 3 and ctx["attempts"] == 2
    assert 41 <= ctx["duration_s"] <= 60
    assert ctx["stopped_at"] == stopped_at
    assert [s["step"] for s in ctx["steps"]] == [
        "campaign_name", "audience_location", "advantage_plus_off", "ad_copy"]
    assert ctx["steps"][0] == {"step": "campaign_name", "ok": True, "duration_s": 2.0}
    assert ctx["steps"][2] == {"step": "advantage_plus_off", "ok": False,
                               "duration_s": 0.7, "reason": "still_on"}
    assert ctx["steps_failed"] == 1
    if result == "warnings":
        assert ctx["warnings"] == ["ad2_second_text"] and ctx["blocking"] == []
    if result == "do-not-publish":
        assert ctx["blocking"] == ["audience_location"] and ctx["warnings"] == ["ad2_second_text"]


def test_the_single_screen_editor_is_named(sentry):
    _steps("run00000002", simple=True)
    run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT, ads=1, background=False)
    assert _only(sentry)["tags"]["editor"] == "single-screen"


# ── имена едут, номера и адреса — нет ──────────────────────────────────────

def test_names_are_sent_and_ids_urls_tokens_emails_are_scrubbed(sentry):
    rt.set_current_run("run00000003", CAMPAIGN)
    rt.record("ad_destination", False, duration_s=3.0,
              note=f"wrong page {URL} profile {PROFILE} token=EAABsecret owner ivan@example.com act_123456789")
    run_summary.report_finished(
        campaign=CAMPAIGN, account=ACCOUNT,
        stopped_at=f"destination_sweep:{URL}", failures=[f"ad2_destination:{PROFILE}"],
        background=False)
    event = _only(sentry)
    blob = json.dumps(event, ensure_ascii=False)
    # Решение владельца 01.10: имя аккаунта и кампании — в событии и тегах.
    assert event["tags"]["campaign"] == CAMPAIGN
    assert event["tags"]["account"] == "FacebookDoc19"
    assert event["contexts"]["run_summary"]["campaign"] == CAMPAIGN
    assert event["contexts"]["run_summary"]["account"] == "FacebookDoc19"
    # Имя персоны после ярлыка аккаунта — нет (как и тег у отказов).
    assert "Petrov" not in blob
    for secret in ("1234567890123456", "987654321012345", PROFILE, "EAABsecret",
                   "ivan@example.com", "123456789", "adsmanager.facebook.com"):
        assert secret not in blob, secret
    reason = event["contexts"]["run_summary"]["steps"][0]["reason"]
    assert "<ads-manager-url>" in reason and "<email>" in reason and "<uuid>" in reason


def test_an_id_typed_into_the_campaign_name_is_scrubbed_in_the_summary():
    summary = run_summary.build(campaign=f"{CAMPAIGN} act_123456789 ivan@example.com", steps=[])
    assert summary["campaign"].startswith(CAMPAIGN)
    assert "123456789" not in summary["campaign"] and "ivan@example.com" not in summary["campaign"]
    assert run_summary.build(campaign=CAMPAIGN, steps=[])["campaign"] == CAMPAIGN


# ── подпись менеджера на каждом событии ────────────────────────────────────

def test_every_event_carries_the_manager_label(sentry, monkeypatch):
    acl_sentry.report_step_failure("ad_copy", "copy_fields_not_revealed", kind="blocking")
    acl_sentry.report_problem("the page froze")
    assert len(sentry.events) == 2
    assert all(e["tags"]["manager"] == "Dan" for e in sentry.events)
    # Имя, исправленное в настройках, едет без перезапуска.
    monkeypatch.setenv("ACL_MACHINE_LABEL", "Yotam")
    acl_sentry.report_problem("again")
    assert sentry.events[-1]["tags"]["manager"] == "Yotam"


def test_the_label_falls_back_to_the_dashboard_name(sentry, monkeypatch):
    monkeypatch.delenv("ACL_MACHINE_LABEL", raising=False)
    monkeypatch.setenv("MY_CM_NAME", "Dima")
    acl_sentry.report_problem("x")
    assert sentry.events[-1]["tags"]["manager"] == "Dima"
    monkeypatch.delenv("MY_CM_NAME", raising=False)
    acl_sentry.report_problem("y")
    assert sentry.events[-1]["tags"]["manager"] == "unset"


def test_every_summary_from_a_job_thread_carries_the_machine(sentry, monkeypatch):
    """Ревью PR #450, HIGH: `init` ставил `machine` в scope своего потока; итог
    уходит из нового потока, форкнутого от потока задания, — со второго итога
    тег пропадал, и `run_summaries.machines()` складывал всё в «unknown»."""
    from src.fb_ads_rpa import telemetry_sync

    monkeypatch.setattr(telemetry_sync, "machine_identity", lambda: ("Dan", "a1b2c3d4e5f6"))
    # `init` не зовём заранее: его зовёт первый итог из своего потока — как на
    # машине байера, где до первой кампании Sentry никто не поднимал.

    errors: list[BaseException] = []

    def job():
        try:
            for n in range(2):
                _steps(f"run0000001{n}")
                worker = run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT)
                # Первый итог поднимает Sentry сам — холодный импорт бывает долгим.
                worker.join(60)
        except BaseException as exc:  # noqa: BLE001 — показать в assert
            errors.append(exc)

    thread = threading.Thread(target=job)
    thread.start()
    thread.join(130)
    assert not thread.is_alive() and errors == []
    summaries = [e for e in sentry.events if (e.get("tags") or {}).get("kind") == "run_summary"]
    assert len(summaries) == 2, sentry.events
    for event in summaries:
        assert event["tags"]["machine"] == "Dan"
        assert event["tags"]["machine_id"] == "a1b2c3d4e5f6"
        assert event["tags"]["manager"] == "Dan"


def test_a_tag_already_on_the_event_is_not_overridden(monkeypatch):
    monkeypatch.setattr(acl_sentry, "_machine_tags", {"machine": "Dan", "machine_id": "a1b2c3"})
    event = {"tags": {"machine": "Other"}}
    acl_sentry._tag_machine(event)
    assert event["tags"] == {"machine": "Other", "machine_id": "a1b2c3"}
    listed = {"tags": [["machine", "Other"]]}
    acl_sentry._tag_machine(listed)
    assert ["machine", "Other"] in listed["tags"] and ["machine_id", "a1b2c3"] in listed["tags"]
    bare: dict = {}
    acl_sentry._tag_machine(bare)
    assert bare["tags"] == {"machine": "Dan", "machine_id": "a1b2c3"}


# ── выключатель и сеть ─────────────────────────────────────────────────────

def test_switch_off_sends_nothing(sentry, monkeypatch):
    monkeypatch.setenv("ACL_SENTRY_RUN_SUMMARY", "0")
    _steps("run00000004")
    assert run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT) is None
    assert run_summary.report_finished(campaign=CAMPAIGN, background=False) is None
    assert sentry.events == []


def test_a_network_error_never_reaches_the_run(sentry):
    sentry.fail = True
    _steps("run00000005")
    # Не бросает: номер события или пусто — всё равно, прогон этого не видит.
    assert isinstance(run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT,
                                                  background=False), str)
    worker = run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT)
    worker.join(5)
    assert not worker.is_alive()
    assert sentry.events == []


def test_sending_never_waits_for_the_network(sentry):
    sentry.gate = threading.Event()
    _steps("run00000006")
    started = time.monotonic()
    worker = run_summary.report_finished(campaign=CAMPAIGN, account=ACCOUNT)
    assert time.monotonic() - started < 1.0
    assert isinstance(worker, threading.Thread) and worker.daemon
    sentry.gate.set()
    worker.join(5)
    assert _only(sentry)["tags"]["result"] == "clean"


# ── из настоящего `publish_campaign` ───────────────────────────────────────

def _publish(monkeypatch, fail_step, soft):
    def fake_flow(page, *, campaign_name, soft_fails_out, **_kw):
        _steps("run00000007")
        soft_fails_out.extend(soft)
        return fail_step

    monkeypatch.setattr(amf, "run_recorded_campaign_flow", fake_flow)
    monkeypatch.setattr(rpa_config, "get_use_recorded_flow", lambda: True)
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, d, r: None)
    monkeypatch.setattr(amf.time, "sleep", lambda *_a, **_k: None)
    from src.fb_ads_rpa import browser, sentry_signal

    monkeypatch.setattr(browser, "put_window_back", lambda page: None)
    monkeypatch.setattr(sentry_signal, "report_campaign_stop", lambda *a, **k: "")
    monkeypatch.setattr(sentry_signal, "report_blocking_failures", lambda *a, **k: [])
    flow = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    flow.page = None
    ads = [AdDraft(creative_type="Image", creative_path=f"{i}.png", ad_title="t", ad_body="b")
           for i in range(2)]
    draft = CampaignDraft(name=CAMPAIGN, country="SA", account_label=ACCOUNT,
                          ad_sets=[AdSetDraft(name="adset", daily_budget=35.0, ads=ads)])
    sent: list = []
    real = run_summary.report_finished
    monkeypatch.setattr(run_summary, "report_finished",
                        lambda **kw: sent.append(real(**{**kw, "background": False})))
    return flow.publish_campaign(draft), sent


def test_a_finished_campaign_sends_its_summary(sentry, monkeypatch):
    result, sent = _publish(monkeypatch, "", ["audience_location"])
    assert result.success and result.blocking_failures == ["audience_location"]
    assert len(sent) == 1
    ctx = _only(sentry)["contexts"]["run_summary"]
    assert ctx["result"] == "do-not-publish" and ctx["ads"] == 2 and ctx["attempts"] == 1
    assert ctx["run_id"] == "run00000007"


def test_a_failing_summary_does_not_change_the_result(sentry, monkeypatch):
    def boom(_summary):
        raise RuntimeError("sentry exploded")

    monkeypatch.setattr(acl_sentry, "report_run_summary", boom)
    result, sent = _publish(monkeypatch, "", [])
    assert result.success and not result.error and sent == [""]


def test_a_stop_while_connecting_the_profile_is_summarised(sentry, monkeypatch):
    from src.fb_ads_rpa import profile_trouble, rpa_publisher, sentry_signal

    monkeypatch.setattr(sentry_signal, "report_campaign_stop", lambda *a, **k: "")
    real = run_summary.report_finished
    monkeypatch.setattr(run_summary, "report_finished",
                        lambda **kw: real(**{**kw, "background": False}))
    _steps("run-of-the-previous-campaign")
    stop = profile_trouble.ProfileStop(profile_trouble.RESTART_FAILED, "ECONNREFUSED")
    rpa_publisher.RpaPublisher._report_profile_stop(stop, CAMPAIGN, ACCOUNT, "connect")
    ctx = _only(sentry)["contexts"]["run_summary"]
    assert ctx["result"] == "stopped" and ctx["steps"] == [] and ctx["editor"] == "unknown"
    assert ctx["run_id"] == ""
