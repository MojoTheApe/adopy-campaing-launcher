"""ACL-1443: lifetime-бюджет и расписание при СОЗДАНИИ кампании на пути API.

Ни Meta, ни Multilogin не трогаются: Graph - `FakeMeta` в памяти. Проверяется по слоям:
политика записи, форма -> слоты -> черновик, план и его ошибки, параметры записи
(смещения по датам, дыра перевода часов), сверка, повтор с другим типом бюджета, предпроверка.
Daily остаётся как был: ключей нет в слотах, черновике, плане и параметрах.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
WINDOW = {"days": [1, 2, 3, 4, 5], "start_minute": 540, "end_minute": 1080}


class ZoneMeta(FakeMeta):
    """FakeMeta + пояс, валюта и минимум дневного бюджета кабинета."""

    def __init__(self, *, tz="America/New_York", min_daily="100", currency="USD", **kw):
        super().__init__(**kw)
        self.tz, self.min_daily, self.currency = tz, min_daily, currency

    def _get(self, path, params):
        body = super()._get(path, params)
        if path.strip("/") == ACT and "account_id" in body:
            body["currency"] = self.currency
            if self.tz is not None:
                body["timezone_name"] = self.tz
                body["timezone_offset_hours_utc"] = -5
            if self.min_daily is not None:
                body["min_daily_budget"] = self.min_daily
        return body


def _creator(meta, now=NOW):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=300)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                              now=lambda: now)


def _life(tmp_path, *, budget=50.0, start="2026-10-20T09:00", end="2026-10-25T09:00", schedule=None, **over):
    d = _draft(tmp_path, ads=1)
    s = d.ad_sets[0]
    s.daily_budget = budget
    s.budget_type = "lifetime"
    s.start_time, s.end_time = start, end
    # ACL-1448: сроки вводят по поясу команды; эти тесты считают в поясе кабинета (разница 0), Израиль - в test_acl1448.
    s.time_zone = "America/New_York"
    s.schedule = [dict(w) for w in schedule] if schedule else []
    for k, v in over.items():
        setattr(s, k, v)
    return d


def _plan(draft):
    return cr.plan_from_draft(draft)


def _run(meta, draft, **kw):
    return _creator(meta, **kw).run(_plan(draft))


# ───────────────────────── политика записи ─────────────────────────

def _camp(**over):
    p = {"name": "C", "objective": "OUTCOME_LEADS", "status": "PAUSED", "special_ad_categories": [],
         "buying_type": "AUCTION", "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "lifetime_budget": 5000}
    p.update(over)
    return p


def _check(edge, params):
    return wp.check(ACT, "POST", f"/{ACT}/{edge}", params)


def test_policy_accepts_a_lifetime_campaign_with_day_parting():
    assert _check("campaigns", _camp()) == "campaigns"
    assert _check("campaigns", _camp(pacing_type=["day_parting"])) == "campaigns"
    assert _check("campaigns", _camp(pacing_type=["standard"])) == "campaigns"


@pytest.mark.parametrize("over,why", [
    ({"daily_budget": 2000}, "not both"),
    ({"lifetime_budget": 0}, "positive whole number"),
    ({"lifetime_budget": "12.5"}, "positive whole number"),
    ({"lifetime_budget": True}, "positive whole number"),
    ({"pacing_type": ["day_parting", "standard"]}, "pacing_type may only be"),
    ({"pacing_type": "day_parting"}, "pacing_type may only be"),
    ({"pacing_type": ["custom"]}, "pacing_type may only be"),
])
def test_policy_refuses_bad_campaign_budget_values(over, why):
    with pytest.raises(wp.WriteRefused, match=why):
        _check("campaigns", _camp(**over))


def test_policy_refuses_day_parting_on_a_daily_campaign():
    p = _camp(pacing_type=["day_parting"])
    del p["lifetime_budget"]
    p["daily_budget"] = 2000
    with pytest.raises(wp.WriteRefused, match="day_parting needs a lifetime budget"):
        _check("campaigns", p)


def _adset(**over):
    p = {"name": "S", "campaign_id": "1", "status": "PAUSED", "optimization_goal": "OFFSITE_CONVERSIONS",
         "billing_event": "IMPRESSIONS", "end_time": "2026-10-25T09:00:00-0400",
         "adset_schedule": [{**WINDOW, "timezone_type": "ADVERTISER"}]}
    p.update(over)
    return p


def test_policy_accepts_an_ad_set_with_end_time_and_schedule():
    assert _check("adsets", _adset()) == "adsets"
    assert _check("adsets", _adset(adset_schedule=[
        {"days": [0, 6], "start_minute": 0, "end_minute": 60, "timezone_type": "ADVERTISER"},
        {"days": [1], "start_minute": 900, "end_minute": 1440, "timezone_type": "ADVERTISER"}])) == "adsets"


@pytest.mark.parametrize("window", [
    {**WINDOW, "timezone_type": "USER"},                                  # пояс только кабинета
    {**WINDOW, "start_minute": 30, "timezone_type": "ADVERTISER"},          # не на границе часа
    {**WINDOW, "end_minute": 1500, "timezone_type": "ADVERTISER"},          # за сутки
    {**WINDOW, "start_minute": 600, "end_minute": 600, "timezone_type": "ADVERTISER"},   # start == end
    {**WINDOW, "days": [], "timezone_type": "ADVERTISER"},
    {**WINDOW, "days": [1, 1], "timezone_type": "ADVERTISER"},
    {**WINDOW, "days": [7], "timezone_type": "ADVERTISER"},
    {**WINDOW, "days": ["1"], "timezone_type": "ADVERTISER"},
    {**WINDOW, "extra": 1, "timezone_type": "ADVERTISER"},
    {"days": [1], "start_minute": 0, "end_minute": 60},                    # нет timezone_type
])
def test_policy_refuses_malformed_schedule_windows(window):
    with pytest.raises(wp.WriteRefused, match="adset_schedule"):
        _check("adsets", _adset(adset_schedule=[window]))


def test_policy_refuses_an_empty_or_endless_schedule_and_a_naive_end_time():
    with pytest.raises(wp.WriteRefused, match="adset_schedule"):
        _check("adsets", _adset(adset_schedule=[]))
    p = _adset()
    del p["end_time"]
    with pytest.raises(wp.WriteRefused, match="needs end_time"):
        _check("adsets", p)
    with pytest.raises(wp.WriteRefused, match="explicit UTC offset"):
        _check("adsets", _adset(end_time="2026-10-25T09:00"))


# ───────────────────────── форма -> слоты -> черновик ─────────────────────────

def _fields(**over):
    f = {"country": "Mexico", "account_label": "Acc", "tracking_link": "https://t.invalid/x", "camp_count": "1",
         "camp_0_name": "MX_test", "camp_0_ad_count": "1", "camp_0_budget": "120.50",
         "camp_0_ad_0_creative_file": "a.png", "camp_0_ad_0_copy": "T1"}
    f.update(over)
    return f


def test_daily_form_and_explicit_daily_give_identical_slots_without_new_keys():
    from src.web import app as web

    plain = web._parse_build_upload_form(_fields())
    explicit = web._parse_build_upload_form(_fields(camp_0_budget_type="daily", camp_0_start_time="",
                                                    camp_0_end_time="", camp_0_schedule=""))
    assert plain == explicit
    assert not {"budget_type", "end_time", "schedule", "start_time"} & set(plain[2][0])
    # daily: срок кампании в слоты не попадает, даже если поля пришли в форме.
    ignored = web._parse_build_upload_form(_fields(camp_0_budget_type="daily", camp_0_end_time="2026-10-25T09:00"))
    assert "end_time" not in ignored[2][0]


def test_lifetime_form_reaches_the_slots_and_the_draft_and_beats_the_set_start():
    import json

    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields(
        camp_0_budget_type="Lifetime", camp_0_start_time="2026-10-20T09:00", camp_0_end_time="2026-10-25T18:00",
        camp_0_start_time_ignored="x", camp_0_schedule=json.dumps([WINDOW]),
        camp_0_adset_count="2", camp_0_adset_1_start_time="2026-10-19T00:00"))
    assert slots[0]["budget_type"] == "lifetime" and slots[0]["budget"] == "120.50"
    assert slots[0]["start_time"] == "2026-10-20T09:00" and slots[0]["end_time"] == "2026-10-25T18:00"
    assert slots[0]["schedule"] == [WINDOW]
    assert slots and all(s["start_time"] == "2026-10-20T09:00" for s in slots)   # кампанийный старт главнее
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    s = draft.ad_sets[0]
    assert (s.budget_type, s.end_time, s.schedule, s.daily_budget) == ("lifetime", "2026-10-25T18:00", [WINDOW], 120.5)
    prev = s.to_preview_dict()
    assert prev["budget_type"] == "lifetime" and prev["end_time"] == "2026-10-25T18:00" and prev["schedule"] == [WINDOW]


def test_daily_draft_previews_without_the_new_keys():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields())
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert set(draft.ad_sets[0].to_preview_dict()) == {"name", "targeting", "os", "age", "daily_budget", "ads"}


def test_a_broken_schedule_or_budget_type_is_an_error_not_a_silent_daily():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields(camp_0_schedule="{not json"))
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert any("schedule could not be read" in e for e in draft.errors)
    _c, _l, slots = web._parse_build_upload_form(_fields(camp_0_budget_type="weekly"))
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert any("Unknown budget type" in e for e in draft.errors)


def test_rpa_refuses_lifetime_and_schedule_fields(monkeypatch):
    from src.web import app as web
    from src.web import publish_api

    monkeypatch.setattr(publish_api, "api_enabled", lambda: False)
    life = [{"budget_type": "lifetime", "end_time": "2026-10-25T09:00", "schedule": [WINDOW], "start_time": "x"}]
    refusal = web._api_only_fields_refusal(life)
    assert refusal is not None and refusal.status_code == 409
    text = refusal.body.decode()
    for title in ("Lifetime budget", "End time", "Ad schedule", "Start time"):
        assert title in text
    assert web._api_only_fields_refusal([{"name": "daily"}]) is None


def test_account_facts_endpoint_reads_only_the_cache(monkeypatch):
    import json

    from src.analysis_agent import account_spend
    from src.web import app as web

    reading = account_spend.SpendReading(account="Acc", date_from="2026-10-01", date_to="2026-10-01",
                                         currency_code="USD", timezone="America/New_York", finished_at="t")
    monkeypatch.setattr(account_spend, "latest_readings", lambda: {"Acc": reading})
    got = json.loads(web.api_account_facts("Acc").body)
    assert got["known"] and got["timezone_name"] == "America/New_York" and got["currency"] == "USD"
    assert got["timezone_offset_hours_utc"] in (-4.0, -5.0) and got["zero_decimal"] is False
    unknown = json.loads(web.api_account_facts("Nobody").body)
    assert unknown["known"] is False and unknown["timezone_name"] == ""


# ───────────────────────── план ─────────────────────────

def test_the_plan_of_a_lifetime_draft_carries_budget_end_and_schedule(tmp_path):
    plan = _plan(_life(tmp_path, schedule=[WINDOW]))
    assert plan.lifetime and plan.lifetime_budget == 50.0 and plan.daily_budget == 0.0
    s = plan.ad_sets[0]
    assert s.start_time == "2026-10-20T09:00" and s.end_time == "2026-10-25T09:00"
    assert s.schedule == [WINDOW]


def test_a_daily_plan_has_no_lifetime_state(tmp_path):
    plan = _plan(_draft(tmp_path, ads=1))
    assert not plan.lifetime and plan.daily_budget == 20.0 and plan.lifetime_budget == 0.0
    assert plan.ad_sets[0].end_time == "" and plan.ad_sets[0].schedule == [] and not plan.has_schedule


@pytest.mark.parametrize("over,message", [
    ({"end": ""}, "needs an end time"),
    ({"start": ""}, "needs a start time"),
    ({"end": "2026-10-20T09:00"}, "must be after the start time"),
    ({"end": "2026-10-19T09:00"}, "must be after the start time"),
    ({"end": "25.10.2026"}, "is not a date and time"),
    ({"schedule": [{"days": [], "start_minute": 0, "end_minute": 60}]}, "schedule is empty"),
    ({"schedule": [{"days": [1], "start_minute": 30, "end_minute": 90}]}, "full hour"),
    ({"schedule": [{"days": [1], "start_minute": 600, "end_minute": 600}]}, "0 <= start < end <= 1440"),
    ({"budget": 0}, "Lifetime budget missing"),
])
def test_lifetime_plan_errors(tmp_path, over, message):
    kw = {k: v for k, v in over.items() if k in ("end", "start", "schedule", "budget")}
    with pytest.raises(cr.PlanError, match=message):
        _plan(_life(tmp_path, **kw))


def test_a_schedule_or_an_end_on_a_daily_budget_is_an_error(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].schedule = [WINDOW]
    with pytest.raises(cr.PlanError, match="a schedule needs a lifetime budget"):
        _plan(d)
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].end_time = "2026-10-25T09:00"
    with pytest.raises(cr.PlanError, match="an end time needs a lifetime budget"):
        _plan(d)


# ───────────────────────── параметры записи и время ─────────────────────────

def test_daily_campaign_and_ad_set_params_are_unchanged(tmp_path):
    creator = _creator(ZoneMeta())
    plan = _plan(_draft(tmp_path, ads=1))
    assert list(creator.campaign_params(plan)) == ["name", "objective", "status", "special_ad_categories",
                                                   "buying_type", "daily_budget", "bid_strategy", "adlabels"]
    params = creator.adset_params(plan, plan.ad_sets[0], "1", {"geo_locations": {}})
    assert not {"end_time", "adset_schedule", "start_time", "pacing_type"} & set(params)


def test_lifetime_campaign_params_use_lifetime_budget_and_day_parting_only_with_a_schedule(tmp_path):
    creator = _creator(ZoneMeta())
    plain = creator.campaign_params(_plan(_life(tmp_path)))
    assert plain["lifetime_budget"] == 5000 and "daily_budget" not in plain and "pacing_type" not in plain
    assert plain["bid_strategy"] == "LOWEST_COST_WITHOUT_CAP"
    timed = creator.campaign_params(_plan(_life(tmp_path, schedule=[WINDOW])))
    assert timed["pacing_type"] == ["day_parting"] and "stop_time" not in timed


def test_lifetime_ad_set_params_carry_offsets_of_their_own_dates(tmp_path):
    """Начало до перевода часов в Нью-Йорке (EDT -0400), конец после (EST -0500)."""
    creator = _creator(ZoneMeta())
    plan = _plan(_life(tmp_path, start="2026-10-30T09:00", end="2026-11-05T09:00", schedule=[WINDOW]))
    params = creator.adset_params(plan, plan.ad_sets[0], "1", {"geo_locations": {}})
    assert params["start_time"] == "2026-10-30T09:00:00-0400" and params["end_time"] == "2026-11-05T09:00:00-0500"
    assert params["adset_schedule"] == [{**WINDOW, "timezone_type": "ADVERTISER"}]
    assert "daily_budget" not in params and "lifetime_budget" not in params


def test_a_time_in_the_dst_gap_is_refused_and_an_ambiguous_one_takes_the_first(tmp_path):
    creator = _creator(ZoneMeta())
    gap = _plan(_life(tmp_path, start="2027-03-14T02:30", end="2027-03-20T09:00"))
    with pytest.raises(cr.PlanError, match="does not exist"):
        creator.adset_params(gap, gap.ad_sets[0], "1", {})
    fold = _plan(_life(tmp_path, start="2026-10-30T09:00", end="2026-11-01T01:30"))
    assert creator.end_iso(fold.ad_sets[0]) == "2026-11-01T01:30:00-0400"


def test_the_end_must_be_in_the_future_at_write_time_and_nothing_is_written(tmp_path):
    meta = ZoneMeta()
    out = _run(meta, _life(tmp_path, start="2026-10-10T09:00", end="2026-10-11T09:00"), now=NOW)
    assert "not in the future" in out.error and meta.posts == []
    out = _run(meta, _life(tmp_path, start="2026-10-20T09:00", end="2026-10-30T09:00"), now=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert "not in the future" in out.error and meta.posts == []


def test_a_lifetime_below_the_account_minimum_times_the_days_is_refused(tmp_path):
    meta = ZoneMeta(min_daily="100")
    out = _run(meta, _life(tmp_path, budget=4.0))       # 5 суток x $1.00 = $5.00
    assert "below the ad account minimum" in out.error and "120 hours" in out.error and meta.posts == []
    ok = _run(ZoneMeta(min_daily="100"), _life(tmp_path, budget=5.0))
    assert ok.error == ""
    no_fact = _run(ZoneMeta(min_daily=None), _life(tmp_path, budget=1.0))
    assert "below the ad account minimum" not in no_fact.error      # нет факта - Meta решит сама


def test_the_minimum_of_a_zero_decimal_currency_is_read_in_whole_units(tmp_path):
    meta = ZoneMeta(min_daily="1000", currency="JPY")
    out = _run(meta, _life(tmp_path, budget=4000))      # 5 x 1000 = 5000 иен
    assert "5000 JPY" in out.error and meta.posts == []


# ───────────────────────── запись и сверка ─────────────────────────

def test_a_lifetime_campaign_is_written_and_read_back(tmp_path):
    meta = ZoneMeta()
    out = _run(meta, _life(tmp_path, schedule=[WINDOW]))
    assert out.error == "" and out.readback["ok"], out.mismatches
    camp = next(p for path, p in meta.posts if path.endswith("/campaigns"))
    assert camp["lifetime_budget"] == 5000 and camp["pacing_type"] == ["day_parting"] and "daily_budget" not in camp
    aset = next(p for path, p in meta.posts if path.endswith("/adsets"))
    assert aset["start_time"] == "2026-10-20T09:00:00-0400" and aset["end_time"] == "2026-10-25T09:00:00-0400"
    fields = {c["field"] for c in out.readback["checks"]}
    assert {"lifetime_budget", "pacing_type", "schedule", "adset_schedule"} <= fields
    assert next(c for c in out.readback["checks"] if c["field"] == "schedule")["expected"] == "ends 2026-10-25T13:00:00Z"


def test_the_daily_readback_has_no_lifetime_checks(tmp_path):
    out = _run(ZoneMeta(), _draft(tmp_path, ads=1))
    assert out.error == "" and out.readback["ok"]
    assert not {"lifetime_budget", "pacing_type", "adset_schedule"} & {c["field"] for c in out.readback["checks"]}
    assert next(c for c in out.readback["checks"] if c["field"] == "schedule")["expected"] == \
        "starts on activation, no end date"


@pytest.mark.parametrize("kind,key,value,field", [
    ("campaign", "lifetime_budget", "9999", "lifetime_budget"),
    ("adset", "end_time", "2026-10-26T09:00:00-0400", "schedule"),
    ("adset", "adset_schedule", [{"days": [1], "start_minute": 540, "end_minute": 1080, "timezone_type": "ADVERTISER"}],
     "adset_schedule"),
])
def test_the_readback_catches_a_changed_budget_end_or_schedule(tmp_path, kind, key, value, field):
    meta = ZoneMeta()
    meta.mutate_on_read[(kind, key)] = value
    out = _run(meta, _life(tmp_path, schedule=[WINDOW]))
    assert field in [m["field"] for m in out.mismatches]


def test_the_schedule_is_compared_as_a_set_of_windows_without_order(tmp_path):
    meta = ZoneMeta()
    split = [{"days": [d], "start_minute": 540, "end_minute": 1080, "timezone_type": "ADVERTISER"}
             for d in (5, 4, 3, 2, 1)]          # те же часы, другой порядок и разбивка по дням
    meta.mutate_on_read[("adset", "adset_schedule")] = split
    out = _run(meta, _life(tmp_path, schedule=[WINDOW]))
    assert out.error == "" and out.readback["ok"], out.mismatches


def test_meta_returning_the_end_in_another_offset_is_the_same_moment(tmp_path):
    meta = ZoneMeta()
    meta.mutate_on_read[("adset", "end_time")] = "2026-10-25T13:00:00+0000"
    out = _run(meta, _life(tmp_path))
    assert out.error == "" and out.readback["ok"], out.mismatches


# ───────────────────────── повтор ─────────────────────────

def test_a_repeat_with_the_other_budget_type_is_refused_with_a_rename_hint(tmp_path):
    meta = ZoneMeta()
    assert _run(meta, _draft(tmp_path, ads=1)).error == ""
    before = len(meta.posts)
    out = _run(meta, _life(tmp_path))
    assert "already exists with a daily budget — rename the campaign" in out.error
    assert out.stop_kind == "budget_type_taken" and len(meta.posts) == before      # новых записей нет

    meta2 = ZoneMeta()
    assert _run(meta2, _life(tmp_path)).error == ""
    back = _run(meta2, _draft(tmp_path, ads=1))
    assert "already exists with a lifetime budget — rename the campaign" in back.error


def test_a_repeat_of_the_same_lifetime_campaign_reuses_it(tmp_path):
    meta = ZoneMeta()
    assert _run(meta, _life(tmp_path, schedule=[WINDOW])).error == ""
    again = _run(meta, _life(tmp_path, schedule=[WINDOW]))
    assert again.error == "" and again.readback["ok"], again.mismatches


def test_a_repeat_with_a_changed_end_is_a_difference_and_gets_a_new_name(tmp_path):
    meta = ZoneMeta()
    assert _run(meta, _life(tmp_path)).error == ""
    plan = _plan(_life(tmp_path, end="2026-10-27T09:00"))
    out = _creator(meta).plan_only(plan)
    assert out.error == "" and out.published_as == plan.name + "_2"
    assert any("end time" in w for w in out.warnings), out.warnings


# ───────────────────────── Check plan ─────────────────────────

def test_the_plan_summary_names_the_lifetime_budget_and_the_schedule(tmp_path):
    out = _creator(ZoneMeta()).plan_only(_plan(_life(tmp_path, schedule=[WINDOW])))
    assert out.error == ""
    summary = out.plan["summary"]
    assert summary["budget_type"] == "lifetime" and summary["lifetime_budget"] == 50.0
    assert summary["budget_minor"] == 5000 and "daily_budget" not in summary
    assert summary["timezone_name"] == "America/New_York" and summary["min_daily_budget"] == 100
    assert summary["timezone_offset_hours_utc"] == -5
    row = summary["ad_sets"][0]
    assert row["start_time"] == "2026-10-20T09:00:00-0400" and row["end_time"] == "2026-10-25T09:00:00-0400"
    assert "Mon 09:00-18:00" in row["schedule"] and len(row["schedule"]) == 5


def test_the_daily_plan_summary_is_unchanged(tmp_path):
    out = _creator(ZoneMeta()).plan_only(_plan(_draft(tmp_path, ads=1)))
    summary = out.plan["summary"]
    assert summary["daily_budget"] == 20.0 and not {"budget_type", "lifetime_budget"} & set(summary)
    assert not {"end_time", "schedule"} & set(summary["ad_sets"][0])
    # Daily не получает ни одного нового ключа: пояс и минимум кабинета только у lifetime-плана.
    assert not {"timezone_name", "timezone_offset_hours_utc", "min_daily_budget"} & set(summary)
    assert set(summary) == {
        "campaign", "status", "children_status", "status_note", "objective", "daily_budget", "currency",
        "budget_minor", "page_id", "pixel_id", "page_source", "pixel_source", "existing",
        "existing_differences", "tag", "ad_sets"} | (set(summary) & {"requested_name", "forms", "manual_required"})


def test_server_budget_min_counts_the_days_of_a_lifetime(tmp_path):
    # Без факта кабинета минимум берётся из таблицы ($1 в день): 3 суток -> $3.
    out = _creator(ZoneMeta(min_daily=None)).plan_only(_plan(_life(tmp_path, budget=2.0, end="2026-10-23T09:00")))
    verdict = {v["rule"]: v for v in out.audit}["budget_min"]
    assert verdict["status"] == "error" and "3 days" in verdict["detail"]
    ok = _creator(ZoneMeta(min_daily=None)).plan_only(_plan(_life(tmp_path, budget=3.0, end="2026-10-23T09:00")))
    assert {v["rule"]: v for v in ok.audit}["budget_min"]["status"] == "ok"


def test_the_daily_path_ignores_the_account_minimum_fact(tmp_path):
    """ACL-2443: min_daily_budget кабинета ($5) не поднимает порог дневного бюджета; таблица валюты ($1) остаётся."""
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].daily_budget = 3.0
    out = _creator(ZoneMeta(min_daily="500")).plan_only(_plan(d))
    assert {v["rule"]: v for v in out.audit}["budget_min"]["status"] == "ok"
    d.ad_sets[0].daily_budget = 0.5
    low = _creator(ZoneMeta(min_daily="500")).plan_only(_plan(d))
    assert {v["rule"]: v for v in low.audit}["budget_min"]["status"] == "error"


def test_the_new_client_rules_are_in_the_table_with_english_texts():
    ids = {r["id"]: r for r in pf.RULES}
    assert ids["end_time_valid"]["side"] == "client" and ids["schedule_valid"]["side"] == "client"
    assert set(pf.export_rules()["consts"]["lifetime_text"]) == {"budget_set", "budget_min"}
    assert pf.JSON_PATH.read_text(encoding="utf-8") == pf.export_json()


def test_the_plan_cache_key_follows_the_lifetime_fields(tmp_path):
    from src.web import plan_cache as pc

    a, b = _life(tmp_path), _life(tmp_path, end="2026-10-26T09:00")
    c = _life(tmp_path, schedule=[WINDOW])
    daily = _draft(tmp_path, ads=1)
    digests = {pc.draft_digest(x) for x in (a, b, c, daily)}
    assert len(digests) == 4


# ───────────────────────── правки после ревью ─────────────────────────

def test_the_write_time_minimum_counts_whole_hours_the_check_plan_one_counts_days(tmp_path):
    """5 суток + 1 минута при бюджете минимум x 5: запись проходит (120 целых часов), Check plan просит 6 суток."""
    kw = dict(start="2026-10-20T09:00", end="2026-10-25T09:01")
    ok = _run(ZoneMeta(min_daily="100"), _life(tmp_path, budget=5.0, **kw))
    assert ok.error == ""
    low = _run(ZoneMeta(min_daily="100"), _life(tmp_path, budget=4.99, **kw))
    assert "below the ad account minimum" in low.error and "120 hours" in low.error
    verdict = {v["rule"]: v for v in _creator(ZoneMeta(min_daily="100")).plan_only(
        _plan(_life(tmp_path, budget=5.0, **kw))).audit}["budget_min"]
    assert verdict["status"] == "error" and "6 days" in verdict["detail"]


def test_a_repeat_after_the_start_passed_reuses_the_campaign_without_a_new_name(tmp_path):
    meta = ZoneMeta()
    first = _run(meta, _life(tmp_path))
    assert first.error == ""
    later = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)      # старт 10-20 09:00 уже в прошлом
    plan = _plan(_life(tmp_path))
    again = _creator(meta, now=later).run(plan)
    assert again.error == "" and again.published_as == plan.name, again.error
    # новый (не найденный) набор с прошедшим стартом по-прежнему отказ
    fresh = _run(ZoneMeta(), _life(tmp_path), now=later)
    assert "not in the future" in fresh.error
    # конец в прошлом отказ и для найденного набора
    after_end = datetime(2026, 10, 26, tzinfo=timezone.utc)
    assert "end time" in _creator(meta, now=after_end).run(_plan(_life(tmp_path))).error


def test_the_event_spec_has_lifetime_keys_only_for_lifetime(tmp_path):
    from src.acl_event_log.clean import canonical_hash
    from src.acl_event_log.events import spec_payload

    facts = _creator(ZoneMeta()).facts
    daily = spec_payload(_plan(_draft(tmp_path, ads=1)), facts)
    assert not {"budget_type", "lifetime_budget"} & set(daily)
    assert not {"end_time", "schedule"} & set(daily["adsets"][0])
    body = {k: v for k, v in daily.items() if k != "spec_hash"}
    assert daily["spec_hash"] == canonical_hash(body)           # хэш daily считается как прежде
    life = spec_payload(_plan(_life(tmp_path, schedule=[WINDOW])), facts)
    assert life["budget_type"] == "lifetime" and life["lifetime_budget"] == 50.0
    assert life["adsets"][0]["end_time"] == "2026-10-25T09:00" and life["adsets"][0]["schedule"] == [WINDOW]
    assert life["spec_hash"] != daily["spec_hash"]


def test_a_mixed_budget_type_warns_in_both_directions():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    def slot(**kw):
        return {"name": "C", "targeting": "t", "budget": "10", "creative_type": "Image", "creative_file": "a.png",
                "copy": "x", **kw}
    for first, second in (({"budget_type": "lifetime"}, {}), ({}, {"budget_type": "lifetime"})):
        [d] = slots_to_campaign_drafts([slot(**first), slot(**second)], account_label="A", country="MX")
        assert any("Mixed budget type" in w for w in d.warnings)
