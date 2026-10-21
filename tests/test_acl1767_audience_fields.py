"""ACL-1767: пол, языки, устройства, версия ОС, старт, кнопка и Multi-advertiser на пути API.

Ни Meta, ни Multilogin здесь не трогаются: Graph - `FakeMeta` в памяти, помощник
страницы - Node. Проверяется: каждое поле в плане, записи и сверке; закрытый
список политики в ОБЕИХ линиях (Python и помощник страницы) на одной матрице
случаев; RPA отказывает вслух; пустые поля не добавляют ключей в слоты и черновик;
серверное правило языка видит дополнительные языки.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, TOKEN, FakeMeta, _draft  # noqa: E402

LOCALES = {"Spanish": 23, "Portuguese": 31, "English": 6}


class AudienceMeta(FakeMeta):
    """FakeMeta + языки по имени («X (All)») и часовой пояс кабинета +02:00."""

    def __init__(self, *, tz="Europe/Madrid", now_offset=2, **kw):
        super().__init__(**kw)
        self.tz = tz
        self.now_offset = now_offset

    def _get(self, path, params):
        if params.get("type") == "adlocale":
            q = str(params.get("q"))
            if q in LOCALES:
                return {"data": [{"key": LOCALES[q], "name": f"{q} (All)"}, {"key": 999, "name": q + " (US)"}]}
            return {"data": []}
        body = super()._get(path, params)
        if path.strip("/") == ACT and self.tz is not None and "account_id" in body:
            body["timezone_name"] = self.tz
            # Смещение «сейчас» отдельно от имени пояса: на дату старта оно может быть другим.
            body["timezone_offset_hours_utc"] = self.now_offset
        return body


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _creator(meta, now=NOW):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=300)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                              now=lambda: now), client


def _with_fields(tmp_path, ads=1, **over):
    d = _draft(tmp_path, ads=ads)
    s = d.ad_sets[0]
    for k, v in over.items():
        if k == "cta":
            for ad in s.ads:
                ad.cta = v
        else:
            setattr(s, k, v)
    return d


def _run(meta, draft, **kw):
    creator, _ = _creator(meta, **kw)
    return creator.run(cr.plan_from_draft(draft))


def _adset(meta):
    return next(o for o in meta.objects.values() if o["_kind"] == "adset")


def _creatives(meta):
    return [o for o in meta.objects.values() if o["_kind"] == "creative"]


# ───────────────────────── пол ─────────────────────────

@pytest.mark.parametrize("gender,ids,label", [("men", [1], "Men"), ("women", [2], "Women")])
def test_gender_is_in_the_plan_the_payload_and_the_readback(tmp_path, gender, ids, label):
    meta = AudienceMeta()
    creator, _ = _creator(meta)
    plan = creator.plan_only(cr.plan_from_draft(_with_fields(tmp_path, gender=gender)))
    assert plan.error == "" and plan.plan["ad_sets"][0]["targeting"]["genders"] == ids
    assert plan.plan["summary"]["ad_sets"][0]["gender"] == label
    out = _run(AudienceMeta(), _with_fields(tmp_path, gender=gender))
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert any(c["field"] == "gender" and c["expected"] == ids for c in out.readback["checks"])


def test_gender_readback_catches_a_changed_audience(tmp_path):
    meta = AudienceMeta()
    meta.mutate_on_read[("adset", "targeting.genders")] = [1, 2]
    out = _run(meta, _with_fields(tmp_path, gender="women"))
    assert [(m["field"], m["expected"], m["actual"]) for m in out.mismatches] == [("gender", [2], [1, 2])]


def test_an_unknown_gender_is_refused_before_any_write(tmp_path):
    meta = AudienceMeta()
    with pytest.raises(cr.PlanError, match="unknown gender"):
        cr.plan_from_draft(_with_fields(tmp_path, gender="other"))
    assert meta.posts == []


# ───────────────────────── языки ─────────────────────────

def test_several_languages_are_resolved_exactly_and_shown_as_picked(tmp_path):
    meta = AudienceMeta()
    creator, _ = _creator(meta)
    d = _with_fields(tmp_path, languages=["Portuguese", "English", "portuguese"])
    d.language = "Spanish"
    plan = creator.plan_only(cr.plan_from_draft(d))
    assert plan.error == ""
    assert plan.plan["ad_sets"][0]["targeting"]["locales"] == [23, 31, 6]          # язык кампании - первый
    row = plan.plan["summary"]["ad_sets"][0]
    assert row["languages_picked"] == ["Spanish (All)", "Portuguese (All)", "English (All)"]
    d2 = _with_fields(tmp_path, languages=["Portuguese"])
    d2.language = "Spanish"
    out = _run(AudienceMeta(), d2)
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert any(c["field"] == "languages" and c["expected"] == [23, 31] for c in out.readback["checks"])


def test_an_unknown_extra_language_stops_the_plan_and_the_audit_names_it(tmp_path):
    meta = AudienceMeta()
    creator, _ = _creator(meta)
    d = _with_fields(tmp_path, languages=["Klingon"])
    d.language = "Spanish"
    out = creator.plan_only(cr.plan_from_draft(d))
    assert out.stop_kind == "plan" and "Klingon" in out.error and meta.posts == []
    verdict = next(v for v in out.audit if v["rule"] == "language_resolves")
    assert verdict["status"] == "error" and "Klingon" in verdict["detail"]


# ───────────────────────── устройства и ОС ─────────────────────────

@pytest.mark.parametrize("over,platforms,user_os", [
    ({}, ["mobile", "desktop"], None),
    ({"device": "mobile"}, ["mobile"], None),
    ({"os": "iOS"}, ["mobile"], ["iOS"]),
    ({"os": "Android"}, ["mobile"], ["Android"]),
    ({"os": "iOS", "os_min": "14"}, ["mobile"], ["iOS_ver_14.0_and_above"]),
    ({"os": "Android", "os_min": "9.1", "device": "mobile"}, ["mobile"], ["Android_ver_9.1_and_above"]),
])
def test_devices_and_os_reach_the_payload_and_the_readback(tmp_path, over, platforms, user_os):
    out = _run(AudienceMeta(), _with_fields(tmp_path, **over))
    assert out.error == "" and out.readback["ok"], out.mismatches
    meta = AudienceMeta()
    creator, _ = _creator(meta)
    plan = creator.plan_only(cr.plan_from_draft(_with_fields(tmp_path, **over)))
    tg = plan.plan["ad_sets"][0]["targeting"]
    assert tg["device_platforms"] == platforms and tg.get("user_os") == user_os
    if over.get("os_min"):
        assert plan.plan["summary"]["ad_sets"][0]["os_min"] == user_os[0]
    if over.get("device"):
        assert plan.plan["summary"]["ad_sets"][0]["devices"] == f"{over['device']} only"


@pytest.mark.parametrize("over,message", [
    ({"os": "iOS", "device": "desktop"}, "mobile system"),
    ({"device": "desktop"}, "Desktop only isn't possible with automatic placements"),
    ({"os": "All", "os_min": "14"}, "needs the operating system"),
    ({"os": "iOS", "os_min": "14.x"}, "not a number"),
    ({"device": "tablet"}, "unknown devices"),
    ({"os": "Windows"}, "unknown operating system"),
])
def test_contradictory_or_unknown_devices_are_refused_out_loud(tmp_path, over, message):
    with pytest.raises(cr.PlanError, match=message):
        cr.plan_from_draft(_with_fields(tmp_path, **over))


DESKTOP_MESSAGE = ("Desktop only isn't possible with automatic placements (Instagram, Stories and Reels "
                   "need mobile). Pick All devices or Mobile, or wait for manual placements.")


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "real"])
def test_desktop_only_is_refused_in_check_plan_with_no_write(tmp_path, dry_run):
    from tests.test_acl1761_api_create import _publisher
    meta = AudienceMeta()
    pub = _publisher(meta, dry_run=dry_run)
    res = pub.publish_draft(_with_fields(tmp_path, device="desktop"))
    assert DESKTOP_MESSAGE in res["error"] and "Nothing was created" in res["error"]
    assert not res.get("success") and meta.posts == [] and pub._client.writes == 0


# ───────────────────────── старт ─────────────────────────

@pytest.mark.parametrize("tz,start,iso", [
    ("Europe/Madrid", "2026-10-20T10:00", "2026-10-20T10:00:00+0200"),
    ("Europe/Madrid", "2026-10-30T10:00", "2026-10-30T10:00:00+0100"),     # летнее время кончилось 25.10
    ("America/New_York", "2026-10-31T10:00", "2026-10-31T10:00:00-0400"),
    ("America/New_York", "2026-11-02T10:00", "2026-11-02T10:00:00-0500"),   # 01.11 в США
])
def test_start_time_uses_the_offset_on_the_start_date_not_todays(tmp_path, tz, start, iso):
    # «Сейчас» у кабинета летнее смещение; на дату старта оно другое - берём по имени пояса.
    meta = AudienceMeta(tz=tz, now_offset=2 if tz == "Europe/Madrid" else -4)
    out = _run(meta, _with_fields(tmp_path, start_time=start))
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert _adset(meta)["start_time"] == iso
    # Сверка идёт с правильным моментом: старый момент по смещению «сейчас» - расхождение.
    other = AudienceMeta(tz=tz, now_offset=2 if tz == "Europe/Madrid" else -4)
    wrong = {"2026-10-30T10:00": "2026-10-30T10:00:00+0200",
             "2026-11-02T10:00": "2026-11-02T10:00:00-0400"}.get(start)
    if wrong:
        other.mutate_on_read[("adset", "start_time")] = wrong
        out2 = _run(other, _with_fields(tmp_path, start_time=start))
        assert [m["field"] for m in out2.mismatches] == ["start_time"]


def test_an_unknown_time_zone_name_is_refused_before_any_write(tmp_path):
    meta = AudienceMeta(tz="Mars/Olympus")
    out = _run(meta, _with_fields(tmp_path, start_time="2026-10-20T10:00"))
    assert out.stop_kind == "plan" and "time zone" in out.error and meta.posts == []


def test_zoneinfo_has_its_data_on_this_machine_and_in_requirements():
    from zoneinfo import ZoneInfo
    ZoneInfo("Europe/Madrid")
    reqs = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "tzdata" in reqs

def test_start_time_is_sent_with_the_account_offset_and_read_back(tmp_path):
    meta = AudienceMeta()
    out = _run(meta, _with_fields(tmp_path, start_time="2026-10-20T10:00"))
    assert out.error == "" and out.readback["ok"], out.mismatches
    assert _adset(meta)["start_time"] == "2026-10-20T10:00:00+0200"
    # Meta отдаёт тот же момент в другом смещении - это не расхождение.
    meta2 = AudienceMeta()
    meta2.mutate_on_read[("adset", "start_time")] = "2026-10-20T08:00:00+0000"
    out2 = _run(meta2, _with_fields(tmp_path, start_time="2026-10-20T10:00"))
    assert out2.readback["ok"], out2.mismatches
    meta3 = AudienceMeta()
    meta3.mutate_on_read[("adset", "start_time")] = "2026-10-21T10:00:00+0200"
    out3 = _run(meta3, _with_fields(tmp_path, start_time="2026-10-20T10:00"))
    assert [m["field"] for m in out3.mismatches] == ["start_time"]


def test_start_time_shows_in_the_plan(tmp_path):
    creator, _ = _creator(AudienceMeta(tz="Asia/Kolkata", now_offset=5.5))
    out = creator.plan_only(cr.plan_from_draft(_with_fields(tmp_path, start_time="2026-10-20T10:00")))
    assert out.plan["ad_sets"][0]["start_time"] == "2026-10-20T10:00:00+0530"
    assert out.plan["summary"]["ad_sets"][0]["start_time"] == "2026-10-20T10:00:00+0530"


def test_a_start_in_the_past_or_an_unknown_time_zone_is_refused_before_any_write(tmp_path):
    meta = AudienceMeta()
    out = _run(meta, _with_fields(tmp_path, start_time="2026-10-12T13:00"))        # 11:00 UTC < 12:00 UTC
    assert out.stop_kind == "plan" and "not in the future" in out.error and meta.posts == []
    meta = AudienceMeta(tz=None)
    out = _run(meta, _with_fields(tmp_path, start_time="2026-10-20T10:00"))
    assert out.stop_kind == "plan" and "time zone" in out.error and meta.posts == []
    with pytest.raises(cr.PlanError, match="not a date and time"):
        cr.plan_from_draft(_with_fields(tmp_path, start_time="tomorrow"))
    with pytest.raises(cr.PlanError, match="not a date and time"):
        cr.plan_from_draft(_with_fields(tmp_path, start_time="2026-13-40T10:00"))


# ───────────────────────── кнопка ─────────────────────────

def test_a_null_cta_is_refused_like_in_the_page_layer():
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives",
                 _cre(object_story_spec={"page_id": "1", "link_data": {"call_to_action": None}}))


def test_the_policy_cta_list_is_per_destination_when_the_destination_is_known():
    params = _cta("ORDER_NOW")
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", params) == "adcreatives"
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", params, on_ad=True)
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", _cta("SIGN_UP"), on_ad=True) == "adcreatives"


def test_the_cta_goes_to_the_creative_the_plan_and_the_readback(tmp_path):
    meta = AudienceMeta()
    out = _run(meta, _with_fields(tmp_path, ads=2, cta="SIGN_UP"))
    assert out.error == "" and out.readback["ok"], out.mismatches
    for cre in _creatives(meta):
        spec = cre["object_story_spec"]
        assert (spec.get("link_data") or spec.get("video_data"))["call_to_action"]["type"] == "SIGN_UP"
    creator, _ = _creator(AudienceMeta())
    plan = creator.plan_only(cr.plan_from_draft(_with_fields(tmp_path, cta="GET_QUOTE")))
    assert plan.plan["summary"]["ad_sets"][0]["ads"][0]["cta"] == "GET_QUOTE"


def test_the_cta_readback_catches_a_changed_button(tmp_path):
    meta = AudienceMeta()
    meta.mutate_ads["creative.object_story_spec.link_data.call_to_action.type"] = "LEARN_MORE"
    out = _run(meta, _with_fields(tmp_path, cta="APPLY_NOW"))
    assert [(m["field"], m["expected"], m["actual"]) for m in out.mismatches] == [
        ("call_to_action", "APPLY_NOW", "LEARN_MORE")]


def test_the_default_cta_is_learn_more_and_a_foreign_one_is_refused(tmp_path):
    d = _with_fields(tmp_path)
    assert cr.plan_from_draft(d).ad_sets[0].ads[0].cta == "LEARN_MORE"
    with pytest.raises(cr.PlanError, match="SHOP_NOW"):
        cr.plan_from_draft(_with_fields(tmp_path, cta="SHOP_NOW"))
    form = _with_fields(tmp_path, cta="ORDER_NOW")
    form.ad_sets[0].destination = "instant_form"
    form.ad_sets[0].ads[0].lead_form_id = "7000001"
    with pytest.raises(cr.PlanError, match="ORDER_NOW"):
        cr.plan_from_draft(form)
    ok = _with_fields(tmp_path, cta="ORDER_NOW")                                # на сайте он допустим
    assert cr.plan_from_draft(ok).ad_sets[0].ads[0].cta == "ORDER_NOW"


def test_every_cta_the_creator_offers_passes_the_write_policy():
    assert set(cr.CTA_WEBSITE) | set(cr.CTA_FORM) == set(wp.CTA_TYPES)
    for cta in wp.CTA_TYPES:
        spec = {"page_id": "1", "link_data": {"call_to_action": {"type": cta, "value": {"link": "https://x.invalid/"}}}}
        assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", {"name": "x", "object_story_spec": spec})
    assert pf.ALLOWED_CTA["leads"] == wp.CTA_WEBSITE and pf.ALLOWED_CTA["leads_instant_form"] == wp.CTA_FORM


# ───────────────────────── Multi-advertiser ─────────────────────────

def test_multi_advertiser_is_explicitly_off_on_every_creative_and_read_back(tmp_path):
    meta = AudienceMeta()
    out = _run(meta, _draft(tmp_path, video=True, ads=3))
    assert out.error == "" and out.readback["ok"]
    cres = _creatives(meta)
    assert len(cres) == 3 and all(c["contextual_multi_ads"] == {"enroll_status": "OPT_OUT"} for c in cres)
    checks = [c for c in out.readback["checks"] if c["field"] == "multi_advertiser"]
    assert len(checks) == 3 and all(c["expected"] == "OPT_OUT" and c["ok"] for c in checks)
    creator, _ = _creator(AudienceMeta())
    plan = creator.plan_only(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert plan.plan["summary"]["ad_sets"][0]["multi_advertiser"] == "off"


def test_multi_advertiser_readback_catches_it_being_switched_on(tmp_path):
    meta = AudienceMeta()
    meta.mutate_ads["creative.contextual_multi_ads.enroll_status"] = "OPT_IN"
    out = _run(meta, _draft(tmp_path, ads=1))
    assert [(m["field"], m["expected"], m["actual"]) for m in out.mismatches] == [
        ("multi_advertiser", "OPT_OUT", "OPT_IN")]
    meta2 = AudienceMeta()
    meta2.mutate_ads["creative.contextual_multi_ads"] = {}
    out2 = _run(meta2, _draft(tmp_path, ads=1))
    assert [m["field"] for m in out2.mismatches] == ["multi_advertiser"]


# ───────────────────────── пустые поля: ключей нет ─────────────────────────

def test_empty_fields_add_nothing_to_the_plan_the_payload_or_the_summary(tmp_path):
    creator, _ = _creator(AudienceMeta())
    out = creator.plan_only(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    s = out.plan["ad_sets"][0]
    assert "start_time" not in s and "genders" not in s["targeting"] and "user_os" not in s["targeting"]
    assert s["targeting"]["device_platforms"] == ["mobile", "desktop"] and s["targeting"]["locales"] == [23]
    row = out.plan["summary"]["ad_sets"][0]
    assert not {"gender", "languages_picked", "devices", "os_min", "start_time"} & set(row)
    assert row["ads"][0]["cta"] == "LEARN_MORE"


# ───────────────────────── политика записи: обе линии ─────────────────────────

def _cre(**over):
    p = {"name": "x", "object_story_spec": {"page_id": "1", "link_data": {
        "link": "https://x.invalid/", "call_to_action": {"type": "LEARN_MORE", "value": {"link": "https://x.invalid/"}}}}}
    p.update(over)
    return p


def _set(targeting):
    return {"name": "s", "status": "PAUSED", "targeting": targeting}


def _cta(t, fmt="link_data"):
    return _cre(object_story_spec={"page_id": "1", fmt: {"call_to_action": {"type": t, "value": {"link": "https://x.invalid/"}}}})


OPT = {"enroll_status": "OPT_OUT"}
#: (имя, ребро, параметры, разрешено)
POLICY_CASES = [
    ("multi_out", "adcreatives", _cre(contextual_multi_ads=OPT), True),
    ("multi_out_json", "adcreatives", _cre(contextual_multi_ads=json.dumps(OPT)), True),
    ("multi_in", "adcreatives", _cre(contextual_multi_ads={"enroll_status": "OPT_IN"}), False),
    ("multi_empty", "adcreatives", _cre(contextual_multi_ads={}), False),
    ("multi_extra_key", "adcreatives", _cre(contextual_multi_ads={"enroll_status": "OPT_OUT", "x": 1}), False),
    ("multi_string", "adcreatives", _cre(contextual_multi_ads="OPT_OUT"), False),
    ("multi_nested", "adcreatives", _cre(object_story_spec={"page_id": "1", "contextual_multi_ads": OPT}), False),
    ("multi_on_adset", "adsets", {"name": "s", "status": "PAUSED", "contextual_multi_ads": OPT}, False),
    ("multi_on_ad", "ads", {"name": "a", "status": "PAUSED", "adset_id": "1", "contextual_multi_ads": OPT}, False),
    ("cta_learn", "adcreatives", _cta("LEARN_MORE"), True),
    ("cta_sign_up_video", "adcreatives", _cta("SIGN_UP", "video_data"), True),
    ("cta_shop_now", "adcreatives", _cta("SHOP_NOW"), False),
    ("cta_lowercase", "adcreatives", _cta("learn_more"), False),
    ("cta_missing_type", "adcreatives", _cre(object_story_spec={"page_id": "1", "link_data": {"call_to_action": {"value": {}}}}), False),
    ("cta_null", "adcreatives", _cre(object_story_spec={"page_id": "1", "link_data": {"call_to_action": None}}), False),
    ("cta_not_object", "adcreatives", _cre(object_story_spec={"page_id": "1", "link_data": {"call_to_action": "LEARN_MORE"}}), False),
    ("t_genders_men", "adsets", _set({"genders": [1]}), True),
    ("t_genders_both", "adsets", _set({"genders": [1, 2]}), True),
    ("t_genders_three", "adsets", _set({"genders": [3]}), False),
    ("t_genders_text", "adsets", _set({"genders": ["1"]}), False),
    ("t_genders_empty", "adsets", _set({"genders": []}), False),
    ("t_genders_bool", "adsets", _set({"genders": [True]}), False),
    ("t_devices", "adsets", _set({"device_platforms": ["mobile", "desktop"]}), True),
    ("t_devices_desktop", "adsets", _set({"device_platforms": ["desktop"]}), True),
    ("t_devices_tablet", "adsets", _set({"device_platforms": ["tablet"]}), False),
    ("t_devices_empty", "adsets", _set({"device_platforms": []}), False),
    ("t_os_ios", "adsets", _set({"user_os": ["iOS"]}), True),
    ("t_os_version", "adsets", _set({"user_os": ["iOS_ver_14.0_and_above"]}), True),
    ("t_os_android_version", "adsets", _set({"user_os": ["Android_ver_9_and_above"]}), True),
    ("t_os_bad_version", "adsets", _set({"user_os": ["iOS_ver_x_and_above"]}), False),
    ("t_os_windows", "adsets", _set({"user_os": ["Windows"]}), False),
    ("t_os_injection", "adsets", _set({"user_os": ["iOS_ver_1.0_and_above; DROP"]}), False),
    ("t_locales", "adsets", _set({"locales": [23, 31]}), True),
    ("t_locales_text", "adsets", _set({"locales": ["23"]}), False),
    ("t_locales_zero", "adsets", _set({"locales": [0]}), False),
    ("t_unknown_key", "adsets", _set({"behaviors": [{"id": "1"}]}), False),
    ("t_custom_audience", "adsets", _set({"custom_audiences": [{"id": "1"}]}), False),
    ("t_not_object", "adsets", {"name": "s", "status": "PAUSED", "targeting": "[1]"}, False),
    ("t_whole_creator_targeting", "adsets", _set({
        "geo_locations": {"countries": ["MX"]}, "age_min": 25, "age_max": 55, "genders": [2],
        "targeting_automation": {"advantage_audience": 0}, "locales": [23],
        "flexible_spec": [{"interests": [{"id": "6", "name": "x"}]}],
        "publisher_platforms": ["facebook"], "facebook_positions": ["feed"], "instagram_positions": ["stream"],
        "device_platforms": ["mobile"], "user_os": ["Android_ver_9.0_and_above"]}), True),
    ("start_time_allowed", "adsets", {"name": "s", "status": "PAUSED", "start_time": "2026-10-20T10:00:00+0200"}, True),
]


#: Те же проверки, когда назначение известно (Instant Form, on_ad): список формы, без «Order now».
ON_AD_CASES = [
    ("form_cta_order_now", "adcreatives", _cta("ORDER_NOW"), False),
    ("form_cta_sign_up", "adcreatives", _cta("SIGN_UP"), True),
]


@pytest.mark.parametrize("name,edge,params,allowed", POLICY_CASES, ids=[c[0] for c in POLICY_CASES])
def test_python_policy_matrix(name, edge, params, allowed):
    if allowed:
        assert wp.check(ACT, "POST", f"/{ACT}/{edge}", params) == edge
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", f"/{ACT}/{edge}", params)


_NODE_MATRIX = r"""
const fakeFetch = async () => ({ status: 200, text: async () => JSON.stringify({ id: '5' }), headers: { get: () => null } });
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams',
             HELPER)(G, loc, doc, { getEntriesByType: () => [] }, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
const cases = CASES;
(async () => {
  const out = {};
  for (const c of cases) {
    const r = await h.call({ method: 'POST', path: '/' + ACTV + '/' + c.edge, params: c.params, on_ad: c.on_ad === true });
    out[c.name] = r.error === 'write_refused' ? 'refused' : (r.body ? 'sent' : JSON.stringify(r));
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_matrix():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_1767"
    cases = [{"name": n, "edge": e, "params": p} for n, e, p, _ in POLICY_CASES]
    cases += [{"name": n, "edge": e, "params": p, "on_ad": True} for n, e, p, _ in ON_AD_CASES]
    script = (_NODE_MATRIX.replace("ACTV", json.dumps(ACT)).replace("TOKENV", json.dumps(TOKEN))
              .replace("NAME", json.dumps(name)).replace("CASES", json.dumps(cases))
              .replace("HELPER", json.dumps(tc.helper_source(name, wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, timeout=90)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_page_helper_gives_the_same_verdicts_as_the_python_policy():
    got = _node_matrix()
    for name, _edge, _params, allowed in POLICY_CASES + ON_AD_CASES:
        assert got[name] == ("sent" if allowed else "refused"), (name, got[name])


def test_the_policy_export_carries_the_new_lists():
    p = wp.policy_for_page(ACT)
    assert "contextual_multi_ads" in p["edges"]["adcreatives"] and "contextual_multi_ads" in p["json_keys"]
    assert p["multi_ads_key"] == "contextual_multi_ads" and "SIGN_UP" in p["cta_types"]
    assert p["targeting"]["genders"] == [1, 2] and p["targeting"]["devices"] == ["desktop", "mobile"]
    assert "contextual_multi_ads" not in p["edges"]["adsets"] and "contextual_multi_ads" not in p["edges"]["ads"]


def test_the_creator_never_sends_what_its_own_policy_would_refuse(tmp_path):
    """Каждый запрос полной кампании с новыми полями проходит политику (FakeMeta зовёт её на записи)."""
    meta = AudienceMeta()
    d = _with_fields(tmp_path, ads=2, gender="men", languages=["Portuguese"], device="mobile", os="iOS",
                     os_min="15", start_time="2026-10-20T10:00", cta="APPLY_NOW")
    d.language = "Spanish"
    out = _run(meta, d)
    assert out.error == "" and out.readback["ok"], out.mismatches


# ───────────────────────── форма → слоты → черновик ─────────────────────────

def _fields(**over):
    f = {"country": "Mexico", "account_label": "Acc", "tracking_link": "https://t.invalid/x", "camp_count": "1",
         "camp_0_name": "MX_test", "camp_0_ad_count": "2", "camp_0_budget": "20",
         "camp_0_ad_0_creative_file": "a.png", "camp_0_ad_0_copy": "T1",
         "camp_0_ad_1_creative_file": "b.png", "camp_0_ad_1_copy": "T2"}
    f.update(over)
    return f


def test_old_forms_give_slots_without_any_new_key():
    from src.web import app as web

    plain = web._parse_build_upload_form(_fields())
    explicit_defaults = web._parse_build_upload_form(_fields(
        camp_0_gender="all", camp_0_device="all", camp_0_os_min="", camp_0_start_time="", camp_0_languages="",
        camp_0_ad_0_cta="LEARN_MORE", camp_0_ad_1_cta=""))
    assert plain == explicit_defaults
    for slot in plain[2]:
        assert not {"gender", "languages", "device", "os_min", "start_time", "cta"} & set(slot)


def test_filled_fields_reach_every_slot_and_the_draft():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields(
        camp_0_gender="Women", camp_0_languages=" Portuguese, English ,", camp_0_device="mobile",
        camp_0_os="iOS", camp_0_os_min="14", camp_0_start_time="2026-10-20T10:00",
        camp_0_ad_0_cta="sign_up", camp_0_ad_1_cta="LEARN_MORE"))
    assert slots[0]["gender"] == "women" and slots[0]["languages"] == ["Portuguese", "English"]
    assert slots[0]["device"] == "mobile" and slots[0]["os_min"] == "14" and slots[0]["start_time"] == "2026-10-20T10:00"
    assert slots[0]["cta"] == "SIGN_UP" and "cta" not in slots[1]
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    s = draft.ad_sets[0]
    assert (s.gender, s.languages, s.device, s.os_min, s.start_time) == (
        "women", ["Portuguese", "English"], "mobile", "14", "2026-10-20T10:00")
    assert [a.cta for a in s.ads] == ["SIGN_UP", ""]


def test_an_empty_draft_previews_exactly_as_before():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields())
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    prev = draft.ad_sets[0].to_preview_dict()
    assert set(prev) == {"name", "targeting", "os", "age", "daily_budget", "ads"}
    assert "cta" not in prev["ads"][0]


def test_an_unknown_gender_from_the_form_is_not_turned_into_a_default(tmp_path):
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    _c, _l, slots = web._parse_build_upload_form(_fields(camp_0_gender="other"))
    assert slots[0]["gender"] == "other"
    [draft] = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")
    assert draft.ad_sets[0].gender == "other"


# ───────────────────────── RPA: отказ вслух и выключенные поля ─────────────────────────

def test_rpa_refuses_filled_api_only_fields_out_loud(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    clean = web._parse_build_upload_form(_fields())[2]
    assert web._api_only_fields_refusal(clean) is None
    cases = {"camp_0_gender": ("women", "Gender"), "camp_0_languages": ("English", "Several languages"),
             "camp_0_device": ("mobile", "Devices"), "camp_0_os_min": ("14", "Minimum OS version"),
             "camp_0_start_time": ("2026-10-20T10:00", "Start time"), "camp_0_ad_0_cta": ("SIGN_UP", "CTA"),
             "camp_0_os": ("Android", "Android only")}
    for key, (value, title) in cases.items():
        slots = web._parse_build_upload_form(_fields(**{key: value}))[2]
        resp = web._api_only_fields_refusal(slots)
        assert resp is not None and resp.status_code == 409, key
        body = json.loads(resp.body)["error"]
        assert title in body and "ACL_FB_BACKEND=api" in body, (key, body)
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    for key, (value, _t) in cases.items():
        assert web._api_only_fields_refusal(web._parse_build_upload_form(_fields(**{key: value}))[2]) is None, key


def test_both_publish_routes_use_the_refusal():
    src = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert src.count("_instant_form_refusal(slots) or _api_only_fields_refusal(slots)") == 2


# ───────────────────────── страница ─────────────────────────

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _page():
    return TPL.read_text(encoding="utf-8")


def test_the_builder_has_the_fields_with_stable_names_and_no_leftover_stubs():
    page = _page()
    for name in ("_gender", "_languages", "_device", "_os_min", "_start_time"):
        assert f"name=\"camp_' + idx + '{name}\"" in page, name
    assert "pfx + 'cta\"" in page
    for key in ("gender", "languages", "cta"):
        assert f"future('{key}'" not in page and f"{key}:" not in page[page.index("var FUTURE = {"):page.index("function esc(v)")]
    # Имена прежних полей не тронуты.
    for name in ("_targeting", "_os", "_age", "_language", "_destination", "_budget", "_asset"):
        assert f"camp_' + idx + '{name}" in page, name


def _extract(page: str, start: str, end: str) -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def test_rpa_shows_empty_fields_disabled_with_a_reason_and_keeps_filled_ones_alive():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    code = _extract(_page(), "window.cbSyncApiOnly = function (slot) {", "window.cbDestinationChanged")
    script = r"""
const mk = (value, def, tip) => ({ value, dataset: { default: def, tip }, disabled: false, title: '' });
const empty = mk('', '', 'Audience gender'), filled = mk('women', '', 'Audience gender');
const cta = mk('LEARN_MORE', 'LEARN_MORE', 'Button of the ad'), cta2 = mk('SIGN_UP', 'LEARN_MORE', 'Button of the ad');
const note = { hidden: true };
const opt = { _a: {}, setAttribute(k, v) { this._a[k] = v; }, getAttribute(k) { return this._a[k]; }, title: '' };
const slot = { querySelectorAll: (s) => s === '.cb-api-field' ? [empty, filled, cta, cta2] : [opt],
               querySelector: () => note };
const win = { ACL_FB_BACKEND: 'rpa' };
%s
win.cbSyncApiOnly(slot);
const rpa = { empty: [empty.disabled, empty.title], filled: [filled.disabled, filled.title], cta: cta.disabled,
              cta2: cta2.disabled, note: note.hidden, opt: [opt._a['aria-disabled'], opt.title] };
win.ACL_FB_BACKEND = 'api';
win.cbSyncApiOnly(slot);
const api = { empty: empty.disabled, cta: cta.disabled, note: note.hidden, opt: opt._a['aria-disabled'] };
process.stdout.write(JSON.stringify({ rpa, api }));
""" % code.replace("window.", "win.").replace("window.ACL_FB_BACKEND", "win.ACL_FB_BACKEND")
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["rpa"]["empty"][0] is True and "API backend only" in out["rpa"]["empty"][1]
    assert out["rpa"]["filled"][0] is False, "заполненное поле нельзя гасить: значение пропало бы молча"
    assert out["rpa"]["cta"] is True and out["rpa"]["cta2"] is False
    assert out["rpa"]["note"] is False and out["rpa"]["opt"][0] == "true" and "API backend only" in out["rpa"]["opt"][1]
    assert out["api"] == {"empty": False, "cta": False, "note": True, "opt": "false"}


def test_the_page_plan_view_shows_the_new_fields():
    page = _page()
    plan = _extract(page, "function cbPlanHtml(data)", "function cbPlanFrame")
    for key in ("a.gender", "a.languages_picked", "a.devices", "a.os_min", "a.start_time", "a.multi_advertiser"):
        assert key in plan, key


def test_the_audit_input_carries_the_new_fields_to_the_rules():
    page = _page()
    audit = _extract(page, "function auditInput(slot)", "function auditLines(slot)")
    for key in ("os:", "device:", "os_min:", "start_time:", "now: cbNowLocal()", "select[name$=\"_cta\"]"):
        assert key in audit, key
    ids = {r["id"] for r in pf.RULES}
    assert {"devices_consistent", "start_time_valid", "cta_allowed", "language_resolves"} <= ids


def test_rule_table_marks_the_new_rules():
    by = pf.RULE_BY_ID
    assert by["devices_consistent"]["side"] == "client" and by["devices_consistent"]["field"]["level"] == "adset"
    assert by["start_time_valid"]["severity"] == "error"
    assert by["language_resolves"]["side"] == "server"
