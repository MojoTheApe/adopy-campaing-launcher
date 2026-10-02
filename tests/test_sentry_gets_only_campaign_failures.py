# -*- coding: utf-8 -*-
"""В Sentry уходят только отказы, которые портят кампанию (ACL-1318).

25.09 список тикетов Sentry состоял из диагностики: `advantage_plus_off:
still-on` ×36, `click_target_covered: silent` ×14, `create_took`, строки
промежуточных попыток, `ad_level_summary: silent` — а настоящий отказ
`ad2_copy: copy-fields-not-revealed` в нём терялся. Проверки гоняют
`publish_campaign` с подменённым записанным потоком через **настоящий**
`acl_sentry.report_step_failure` и приёмник-двойник на месте `sentry_sdk`:
что попало в двойник, то уехало бы в Sentry.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import ads_manager_flow as amf  # noqa: E402
from src.fb_ads_rpa import config as rpa_config  # noqa: E402
from src.fb_ads_rpa import failure_policy  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import sentry_signal  # noqa: E402
from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft  # noqa: E402

JOB = "550e8400-e29b-41d4-a716-446655440000"

#: Строки, которые 25.09 заполнили Sentry и отказом кампании не являются.
#: Список закреплён: новая «диагностика» сюда добавляется осознанно.
DIAGNOSTIC_ROWS = [
    ("advantage_plus_off", "still_on"),
    ("click_target_covered", ""),
    ("create_took", "slow"),
    ("conversion_website_pick", "not_selected"),
    ("locations_open", "nothing_to_click"),
    ("name_suggestions", ""),
    ("pick_further_limit_radio", ""),
    ("ad_creative_reuse", "retreat_left_selection"),
    ("ad_copy_fields", "not_revealed"),
    ("ad_level_summary", ""),
    ("job_phase", "open_campaigns"),
]


@pytest.fixture
def events(monkeypatch, tmp_path):
    """Приёмник-двойник на месте `sentry_sdk`: событие после `before_send`."""
    import sentry_sdk

    out: list[dict] = []

    class Scope:
        def __init__(self):
            self.fingerprint = None
            self.tags: dict = {}
            self.contexts: dict = {}

        def set_tag(self, key, value):
            self.tags[key] = value

        def set_context(self, key, value):
            self.contexts[key] = value

    holder: dict = {}

    @contextmanager
    def new_scope():
        holder["scope"] = Scope()
        yield holder["scope"]

    def capture_message(message, **kwargs):
        scope = holder["scope"]
        event = acl_sentry._before_send({
            "message": message,
            "fingerprint": scope.fingerprint,
            "tags": scope.tags,
            "contexts": scope.contexts,
        }, {})
        out.append(event)
        return f"event-{len(out)}"

    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    monkeypatch.setattr(sentry_sdk, "new_scope", new_scope)
    monkeypatch.setattr(sentry_sdk, "capture_message", capture_message)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    rt.clear_current_run()
    rt.set_current_job(JOB)
    yield out
    rt.clear_current_run()


def _draft(name="GT_ait_All_Video_FX_35_25.09") -> CampaignDraft:
    ad = AdDraft(creative_type="Image", creative_path="x.png", ad_title="t", ad_body="b")
    ad2 = AdDraft(creative_type="Image", creative_path="y.png", ad_title="t2", ad_body="b2")
    return CampaignDraft(
        name=name, country="SA", account_label="FacebookDoc19",
        ad_sets=[AdSetDraft(name="adset", daily_budget=35.0, ads=[ad, ad2])],
    )


def _publish(monkeypatch, attempts):
    """`publish_campaign` над подменённым потоком.

    `attempts` — список функций, по одной на попытку: каждая пишет свои строки
    телеметрии и возвращает `(fail_step, soft_fails)`.
    """
    queue = list(attempts)
    calls = {"n": 0}

    def fake_flow(page, *, campaign_name, soft_fails_out, **_kw):
        calls["n"] += 1
        rt.set_current_run(f"run{calls['n']:08d}", campaign_name)
        fail_step, soft = queue.pop(0)(campaign_name)
        soft_fails_out.extend(soft)
        return fail_step

    monkeypatch.setattr(amf, "run_recorded_campaign_flow", fake_flow)
    monkeypatch.setattr(rpa_config, "get_use_recorded_flow", lambda: True)
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, d, r: None)
    monkeypatch.setattr(amf.time, "sleep", lambda *_a, **_k: None)
    from src.fb_ads_rpa import browser

    monkeypatch.setattr(browser, "put_window_back", lambda page: None)
    flow = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    flow.page = None
    return flow.publish_campaign(_draft())


def _diagnostics(campaign):
    for step, note in DIAGNOSTIC_ROWS:
        rt.record(step, False, note=note, diag={"url": "https://x/?act=1234567890"})
    rt.record("ad_level_summary", True)
    return "", []


# ── (1) чистый прогон ──────────────────────────────────────────────────────

def test_a_clean_campaign_sends_nothing_even_with_diagnostic_rows(monkeypatch, events):
    result = _publish(monkeypatch, [_diagnostics])
    assert result.success and not result.blocking_failures
    assert events == [], [e["message"] for e in events]


@pytest.mark.parametrize("step, note", DIAGNOSTIC_ROWS)
def test_a_diagnostic_row_stays_in_local_telemetry(events, tmp_path, step, note):
    rt.record(step, False, run_id="r1", note=note)
    assert events == []
    assert step in (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")


# ── (2) черновик «не публиковать» ──────────────────────────────────────────

COPY_FAIL = "ad2_copy:1962 - AR Text 1 has no text (copy_fields_not_revealed)"


def _do_not_publish(campaign):
    _diagnostics(campaign)
    # Шаг пишет две строки: первая с причиной, вторая — со снимком страницы.
    rt.record("audience_location", False, note="location_rows_not_read", diag={"keep": "Saudi Arabia"})
    rt.record("audience_location", False,
              diag={"url": "https://adsmanager.facebook.com/x?act=1234567890&token=secret"})
    rt.record("ad2_copy", False, diag={"fields": 0, "url": "https://x/?act=1234567890123456"})
    rt.record("ad_level_summary", False, diag={"soft_fails": [COPY_FAIL]})
    return "", ["audience_location", COPY_FAIL]


def test_a_do_not_publish_campaign_sends_one_event_per_blocking_failure(monkeypatch, events):
    result = _publish(monkeypatch, [_do_not_publish])
    assert result.blocking_failures == ["audience_location", COPY_FAIL]
    assert [e["message"] for e in events] == [
        "audience_location: location-rows-not-read",
        "ad2_copy: copy-fields-not-revealed",
    ]
    loc, copy = events
    # ACL-1447: каждое событие подписано именем менеджера машины.
    assert loc["tags"].pop("manager")
    assert loc["tags"] == {"step": "audience_location", "reason": "location-rows-not-read",
                           "failure_kind": "blocking", "account": "FacebookDoc19"}
    assert copy["fingerprint"] == ["acl-step", "ad_copy", "copy-fields-not-revealed"]
    run = copy["contexts"]["run"]
    assert run["run_id"] == "run00000001"
    assert run["campaign"] == "GT_ait_All_Video_FX_35_25.09"
    assert run["job"] == JOB.replace("-", "")
    # Контекст шага — вычищенный diag его последней строки телеметрии.
    assert loc["contexts"]["diag"]["url"].count("act=<hidden>") == 1
    assert "secret" not in json.dumps(loc)
    assert copy["contexts"]["diag"]["fields"] == 0
    assert copy["contexts"]["diag"]["failure"] == COPY_FAIL
    assert copy["contexts"]["diag"]["policy"] == failure_policy.reason(COPY_FAIL)


def test_the_fingerprint_does_not_depend_on_the_campaign(events):
    for campaign, failure in (("SA_sa_All_Image_Mix_35_25.09", COPY_FAIL),
                              ("CL_ML_All_Video_FX_35_25.09",
                               "ad3_copy:1808 - ES Text 2 has no text (copy_fields_not_revealed)")):
        rt.set_current_run(f"r-{campaign[:2]}", campaign)
        sentry_signal.report_blocking_failures([failure], campaign=campaign, account="A")
    a, b = events
    assert a["fingerprint"] == b["fingerprint"]
    assert not any(ch.isdigit() for part in a["fingerprint"] for ch in part)


@pytest.mark.parametrize("failure, why, reason", [
    (COPY_FAIL, "", "copy-fields-not-revealed"),
    ("destination_sweep:2292 - ES Text 5 Ait:row_not_found", "", "row-not-found"),
    ("ad2_name:not_the_new_ad_after_reload", "", "not-the-new-ad-after-reload"),
    ("ad_copy:Ad 1 has no text", "", "no-text"),
    ("audience_age:Timeout 30000ms exceeded.", "", "timeout"),
    ("audience_location", "location_rows_not_read", "location-rows-not-read"),
    ("ad3_create:Page.evaluate: Target page, context or browser has been clos", "", "page-closed"),
    ("placements", "", "silent"),
])
def test_the_title_names_the_reason_when_there_is_one(failure, why, reason):
    assert acl_sentry.failure_reason(failure, why) == reason


# ── (3) остановка попытки ──────────────────────────────────────────────────

def _stops_at(step):
    def attempt(campaign):
        rt.record("create_took", False, note="slow")  # диагностика попытки
        rt.record(step, False, diag={"dialogs": 1})
        return step, []
    return attempt


def test_each_attempt_that_ended_sends_one_event(monkeypatch, events):
    result = _publish(monkeypatch, [_stops_at("campaign_name"), _stops_at("campaign_name"), _diagnostics])
    assert result.success
    assert [e["message"] for e in events] == ["campaign_name: silent", "campaign_name: silent"]
    assert [e["contexts"]["run"]["attempt"] for e in events] == [1, 2]
    assert [e["contexts"]["run"]["run_id"] for e in events] == ["run00000001", "run00000002"]
    assert all(e["tags"]["failure_kind"] == "campaign_stop" for e in events)
    assert events[0]["contexts"]["diag"] == {"dialogs": 1}


def test_an_exhausted_retry_sends_one_event_per_attempt(monkeypatch, events):
    result = _publish(monkeypatch, [_stops_at("objective_leads")] * 3)
    assert not result.success
    assert len(events) == 3


def test_a_non_retryable_stop_is_sent_once(monkeypatch, events):
    def blocked(campaign):
        rt.record("account_blocked", False, note="Verification required to advertise")
        return "account_blocked", []

    result = _publish(monkeypatch, [blocked])
    assert result.blocking_failures == ["account_blocked"]
    assert len(events) == 1 and events[0]["tags"]["step"] == "account_blocked"


def test_a_broken_collector_does_not_break_the_campaign(monkeypatch, events):
    def boom(*_a, **_k):
        raise RuntimeError("collector down")

    monkeypatch.setattr(acl_sentry, "report_step_failure", boom)
    result = _publish(monkeypatch, [_do_not_publish])
    assert result.blocking_failures == ["audience_location", COPY_FAIL]


def test_a_silent_stop_takes_the_reason_its_helper_wrote(monkeypatch, events):
    """Реплей 25.09: `conversion_website` останавливал попытку молча, а
    строкой раньше `conversion_website_pick` писал `not_selected`."""
    def attempt(campaign):
        rt.record("conversion_website_pick", False, note="not_selected", diag={"menu": "open"})
        rt.record("conversion_website", False, diag={"dialogs": 1})
        return "conversion_website", []

    _publish(monkeypatch, [attempt, _diagnostics])
    assert [e["message"] for e in events] == ["conversion_website: not-selected"]
    assert events[0]["contexts"]["diag"] == {"dialogs": 1, "reason_from_step": "conversion_website_pick"}


def test_a_helper_of_another_step_lends_no_reason(events):
    """Чужую причину берём только у помощника из явной таблицы."""
    rt.set_current_run("r-other", "C")
    rt.record("conversion_website_pick", False, note="not_selected")
    rt.record("campaign_name", False)
    sentry_signal.report_campaign_stop("campaign_name")
    assert [e["message"] for e in events] == ["campaign_name: silent"]


# ── повторное ревью v3 PR #314 (находка 3) ────────────────────────────────

def test_a_warning_of_the_final_result_is_not_sent(monkeypatch, events):
    """Y3: предупреждения (черновик с ними публикуем) в Sentry не уходят."""
    monkeypatch.setitem(failure_policy.POLICY, "test_only_warning",
                        (failure_policy.WARNING, "test-only warning"))
    from src.fb_ads_rpa import sentry_signal

    sent = sentry_signal.report_blocking_failures(["audience_location", "test_only_warning"],
                                                  campaign="C", account="FacebookDoc19")
    assert len(sent) == 1
    assert [e["tags"]["step"] for e in events] == ["audience_location"]


def test_the_remembered_failures_are_bounded():
    """Y9: память о последних отказах шагов не растёт без конца."""
    rt.clear_current_run()
    for i in range(rt._FAILURES_MAX + 50):
        rt._remember_failure(f"run{i}", "placements", "not_found", None)
    assert len(rt._FAILURES) <= rt._FAILURES_MAX


def test_an_exception_that_ends_a_job_reaches_sentry(monkeypatch, events):
    """Исключение, которым кончилась работа задания, отправляется со стеком
    и через тот же вычиститель; тег аккаунта — короткий ярлык."""
    import sentry_sdk

    from src.web import publish_jobs as pj

    captured = []

    def capture_exception(exc, **kw):
        captured.append(exc)
        return "event-exc"

    monkeypatch.setattr(sentry_sdk, "capture_exception", capture_exception)

    class _Draft:
        name, errors = "C", []

    class _Publisher:
        def __init__(self, **kw):
            pass

        def build_drafts(self, *a, **k):
            return [_Draft()]

        def publish_draft(self, draft):
            raise RuntimeError("reattach failed act=1234567890")

        def close(self, force=False):
            pass

    monkeypatch.setattr(pj, "RpaPublisher", _Publisher)
    monkeypatch.setattr(pj, "_finish", lambda *a, **k: None)
    job_id = "acl-1318-exc"
    with pj._jobs_lock:
        pj._jobs[job_id] = {"job_id": job_id, "status": "running", "results": [], "errors": [],
                            "cancelled": False, "creative_notices": [], "done_count": 0}
    try:
        pj._run_rpa(job_id, [], account_label="FacebookDoc19 - Persona Name", country="SA", activate=False)
    finally:
        with pj._jobs_lock:
            pj._jobs.pop(job_id, None)
    assert len(captured) == 1 and "reattach failed" in str(captured[0])
    # Событие исключения проходит тот же вычиститель, что и прочие.
    event = acl_sentry._before_send({"exception": {"values": [{"value": str(captured[0])}]}}, {})
    assert "1234567890" not in json.dumps(event)


def test_the_account_tag_is_the_short_label():
    assert acl_sentry.account_tag("FacebookDoc19 - Persona Name") == "FacebookDoc19"
    assert acl_sentry.account_tag("FacebookDQ204 CR") == "FacebookDQ204"
    assert acl_sentry.account_tag("") == ""


# ── повторное ревью v3 PR #314 (находка 4): отпечаток и тег аккаунта ───────

@pytest.mark.parametrize("failure,why,reason", [
    # Скобки посреди текста — часть имени объявления, не причина.
    ("ad2_copy:My (summer_sale) ad has no text", "", "no-text"),
    # Целиком «метка» после двоеточия — имя объявления.
    ("destination_sweep:summer_sale_ad", "", "other"),
    # Наша собственная метка после имени и двоеточия.
    ("destination_sweep:2292 - ES Text 5 Ait:row_not_found", "", "row-not-found"),
    ("ad2_copy:My (summer_sale) ad has no text (copy_fields_not_revealed)", "", "copy-fields-not-revealed"),
])
def test_an_ad_name_never_becomes_the_reason(failure, why, reason):
    assert acl_sentry.failure_reason(failure, why) == reason


@pytest.mark.parametrize("where", ["ad_copy", "ad3_create", "ad2_copy", "destination_sweep"])
def test_browser_closed_has_one_reason_wherever_it_happened(where):
    """browser_closed пишет note=page_closed, место — в diag: один тикет."""
    rt.clear_current_run()
    rt.set_current_run("runbc0001", "C")
    rt.record("browser_closed", False, note="page_closed", diag={"where": where})
    seen = rt.last_failure("browser_closed", "runbc0001") or {}
    assert acl_sentry.failure_reason("browser_closed", seen.get("why", "")) == "page-closed"
    rt.clear_current_run()


def test_the_flow_writes_browser_closed_with_a_stable_note():
    import inspect

    from src.fb_ads_rpa import meta_dom as md

    src = inspect.getsource(md.run_recorded_campaign_flow)
    i = src.index("def _browser_closed(")
    block = src[i:i + 1200]
    assert 'note="page_closed"' in block and 'diag={"where": where' in block


def test_the_account_tag_on_events_is_the_short_label(monkeypatch, events):
    acl_sentry.report_step_failure("audience_age", "x", account="FacebookDoc19 - Persona Name")
    assert events[-1]["tags"]["account"] == "FacebookDoc19"
