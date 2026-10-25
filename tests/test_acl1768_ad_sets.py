"""ACL-1768: несколько наборов объявлений в кампании (кампания -> наборы -> объявления).

Ни Meta, ни Multilogin, ни браузер здесь не трогаются: Graph - `FakeMeta` в памяти.
Проверяется:
* модель: явный `adset_index` в слоте, `CampaignDraft.ad_sets[n]`, имя склеивает слоты
  только в кампанию (мутационная проверка склейки ниже);
* старые черновики и старые формы читаются как один набор, без новых ключей в слотах;
* план, создание, сверка и повторный запуск для 1 кампании x 2 наборов x 3 объявлений;
* имена наборов уникальны; пиксель и язык набора;
* RPA-бэкенд отказывает вслух: «Several ad sets need the API backend»;
* pre-flight считает правила уровня набора на каждый набор (клиентские фикстуры - в
  test_acl1819_preflight.py, паритет с JS там же).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta  # noqa: E402
from tests.test_acl1775_instant_forms import _Req  # noqa: E402


# ───────────────────────────── черновик и план ─────────────────────────────

def _ad_set(tmp_path, name: str, first: int, ads: int = 3, **over):
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft

    items = []
    for i in range(first, first + ads):
        f = tmp_path / f"{1000 + i}_img.png"
        f.write_bytes(b"creative-%d" % i)
        items.append(AdDraft(creative_type="Image", creative_id=str(1000 + i), creative_filename=f.name,
                             creative_path=str(f), copy_name=f"Text{i}", ad_title=f"Head {i}",
                             ad_body=f"Body {i}", ad_link_description="Desc"))
    base = dict(name=name, targeting_label="Day trading, Swing trading", os="All", age_min=25, age_max=55,
                daily_budget=20.0, ads=items)
    base.update(over)
    return AdSetDraft(**base)


def _draft(tmp_path, *, sets=2, ads=3, **over):
    from src.fb_ads_rpa.draft import CampaignDraft

    ad_sets = [_ad_set(tmp_path, "MX Leads 1" if i == 0 else f"MX Leads 1 - Ad set {i + 1}", i * ads, ads,
                       adset_index=i) for i in range(sets)]
    ad_sets[1].age_min, ad_sets[1].age_max = 30, 45              # у наборов разный таргетинг
    d = CampaignDraft(name="MX Leads 1", country="MX", account_label="Test", link="https://example.invalid/l",
                      language="Spanish", page_id="111", ad_account_id=ACT, ad_sets=ad_sets)
    for k, v in over.items():
        setattr(d, k, v)
    return d


def _creator(meta, *, writes=True):
    client = g.GraphClient(meta, allow_writes=writes, sleep=lambda s: None, min_interval_s=0.0, max_calls=400)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None), client


def _run(meta, draft):
    creator, client = _creator(meta)
    return creator.run(cr.plan_from_draft(draft)), client


def test_the_plan_has_every_ad_set_and_numbers_ads_across_the_campaign(tmp_path):
    plan = cr.plan_from_draft(_draft(tmp_path))
    assert [s.name for s in plan.ad_sets] == ["MX Leads 1", "MX Leads 1 - Ad set 2"]
    assert [s.index for s in plan.ad_sets] == [1, 2]
    assert [a.index for s in plan.ad_sets for a in s.ads] == [1, 2, 3, 4, 5, 6]
    assert (plan.ad_sets[1].age_min, plan.ad_sets[1].age_max) == (30, 45)
    assert plan.daily_budget == 20.0


def test_two_ad_sets_three_ads_each_are_created_and_read_back(tmp_path):
    meta = FakeMeta()
    out, _client = _run(meta, _draft(tmp_path))
    assert out.error == "", out.error
    edges = [p.rsplit("/", 1)[1] for p, _ in meta.posts]
    assert edges.count("campaigns") == 1 and edges.count("adsets") == 2 and edges.count("ads") == 6
    for path, params in meta.posts:
        if path.rsplit("/", 1)[1] in ("campaigns", "adsets", "ads"):
            # ACL-2114: только кампания PAUSED, наборы и объявления ACTIVE под ней.
            want = "PAUSED" if path.rsplit("/", 1)[1] == "campaigns" else "ACTIVE"
            assert params["status"] == want
    sets = [o for o in meta.objects.values() if o["_kind"] == "adset"]
    assert len(sets) == 2 and len({o["name"] for o in sets}) == 2
    assert {o["campaign_id"] for o in sets} == {out.campaign_id}
    # каждое объявление лежит в своём наборе: по три
    ads = [o for o in meta.objects.values() if o["_kind"] == "ad"]
    assert sorted(len([a for a in ads if a["adset_id"] == s["id"]]) for s in sets) == [3, 3]
    assert len(out.adset_ids) == 2 and len(out.ad_ids) == 6 and sorted(out.ad_media) == [1, 2, 3, 4, 5, 6]
    assert out.readback["ok"] is True, out.readback["mismatches"]
    # сверка прошла по КАЖДОМУ набору
    objs = {c["object"] for c in out.readback["checks"]}
    assert {"ad set MX Leads 1", "ad set MX Leads 1 - Ad set 2"} <= objs
    second_set = [c for c in out.readback["checks"] if c["object"] == "ad set MX Leads 1 - Ad set 2"
                  and c["field"] in ("age_min", "age_max")]
    assert [(c["expected"], c["ok"]) for c in second_set] == [(30, True), (45, True)]


def test_a_rerun_creates_nothing_with_several_ad_sets(tmp_path):
    meta = FakeMeta()
    first, _ = _run(meta, _draft(tmp_path))
    n_posts, n_objects = len(meta.posts), len(meta.objects)
    second, client = _run(meta, _draft(tmp_path))
    assert second.error == "" and second.created == {}
    assert len(meta.posts) == n_posts and len(meta.objects) == n_objects and client.writes == 0
    assert second.campaign_id == first.campaign_id
    assert sorted(second.adset_ids) == sorted(first.adset_ids) and sorted(second.ad_ids) == sorted(first.ad_ids)
    assert second.readback["ok"] is True


def test_a_rerun_after_adding_a_third_ad_set_only_adds_that_set(tmp_path):
    meta = FakeMeta()
    _run(meta, _draft(tmp_path, sets=2))
    posts_before = len(meta.posts)
    out, _ = _run(meta, _draft(tmp_path, sets=3))
    assert out.error == "", out.error
    new = [p.rsplit("/", 1)[1] for p, _ in meta.posts[posts_before:]]
    assert new.count("adsets") == 1 and new.count("ads") == 3 and "campaigns" not in new
    assert len([o for o in meta.objects.values() if o["_kind"] == "adset"]) == 3


def test_check_plan_shows_each_ad_set_and_writes_nothing(tmp_path):
    meta = FakeMeta()
    creator, client = _creator(meta, writes=False)
    out = creator.plan_only(cr.plan_from_draft(_draft(tmp_path)))
    assert out.error == "", out.error
    assert meta.posts == [] and client.writes == 0
    sets = out.plan["summary"]["ad_sets"]
    assert [s["name"] for s in sets] == ["MX Leads 1", "MX Leads 1 - Ad set 2"]
    assert [s["index"] for s in sets] == [1, 2] and [s["age"] for s in sets] == ["25-55", "30-45"]
    assert [len(s["ads"]) for s in sets] == [3, 3]
    assert [a["index"] for s in sets for a in s["ads"]] == [1, 2, 3, 4, 5, 6]
    assert len(out.plan["ad_sets"]) == 2


# ───────────────────────────── уникальность и поля набора ─────────────────────────────

@pytest.mark.parametrize("second", ["MX Leads 1", "  mx leads 1 "])
def test_ad_set_names_must_differ(tmp_path, second):
    d = _draft(tmp_path)
    d.ad_sets[1].name = second
    with pytest.raises(cr.PlanError, match="Two ad sets are named"):
        cr.plan_from_draft(d)


def test_the_same_ad_name_in_two_ad_sets_is_fine_inside_one_set_it_is_not(tmp_path):
    d = _draft(tmp_path)
    same = d.ad_sets[0].ads[0]
    d.ad_sets[1].ads[0].copy_name, d.ad_sets[1].ads[0].creative_id = same.copy_name, same.creative_id
    d.ad_sets[1].ads[0].creative_filename = same.creative_filename
    plan = cr.plan_from_draft(d)                       # разные наборы - можно
    assert plan.ad_sets[0].ads[0].name == plan.ad_sets[1].ads[0].name
    d.ad_sets[0].ads[1].copy_name, d.ad_sets[0].ads[1].creative_id = same.copy_name, same.creative_id
    d.ad_sets[0].ads[1].creative_filename = same.creative_filename
    with pytest.raises(cr.PlanError, match="would all be named"):
        cr.plan_from_draft(d)


def test_an_ad_set_without_ads_is_refused_not_dropped(tmp_path):
    d = _draft(tmp_path)
    d.ad_sets[1].ads = []
    with pytest.raises(cr.PlanError, match="No ads in ad set 'MX Leads 1 - Ad set 2'"):
        cr.plan_from_draft(d)


def test_ad_set_fields_are_per_ad_set_in_targeting_and_readback(tmp_path):
    meta = FakeMeta()
    d = _draft(tmp_path)
    d.ad_sets[1].gender = "women"
    d.ad_sets[1].os, d.ad_sets[1].os_min, d.ad_sets[1].device = "iOS", "14", "mobile"
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"] is True, out.readback.get("mismatches")
    first, second = sorted((o for o in meta.objects.values() if o["_kind"] == "adset"), key=lambda o: o["name"])
    assert "genders" not in first["targeting"] and second["targeting"]["genders"] == [2]
    assert "user_os" not in first["targeting"] and second["targeting"]["user_os"]


def test_an_ad_set_can_have_its_own_language(tmp_path):
    meta = FakeMeta()
    d = _draft(tmp_path, language="")                 # у кампании языка нет, у второго набора свой
    d.ad_sets[1].language = "Spanish"
    plan = cr.plan_from_draft(d)
    assert plan.ad_sets[0].language == "" and plan.ad_sets[1].language == "Spanish"
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"] is True, out.readback.get("mismatches")
    locales = {o["name"]: o["targeting"].get("locales") for o in meta.objects.values() if o["_kind"] == "adset"}
    assert not locales["MX Leads 1"] and locales["MX Leads 1 - Ad set 2"] == [23]


def test_an_ad_set_can_have_its_own_pixel(tmp_path):
    meta = FakeMeta()
    d = _draft(tmp_path)
    d.ad_sets[1].pixel_id = "222"                      # пиксель кабинета в FakeMeta - 222
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"] is True
    d2 = _draft(tmp_path)
    d2.ad_sets[1].pixel_id = "999"                     # не на кабинете
    creator, _ = _creator(FakeMeta(), writes=False)
    out2 = creator.plan_only(cr.plan_from_draft(d2))
    assert "pixel of ad set" in out2.error


# ───────────────────────────── форма -> слоты -> черновик ─────────────────────────────

def _fields(**over):
    f = {"country": "Mexico", "account_label": "Acc", "tracking_link": "https://t.invalid/x", "camp_count": "1",
         "camp_0_name": "MX_test", "camp_0_ad_count": "2", "camp_0_budget": "20",
         "camp_0_ad_0_creative_file": "a.png", "camp_0_ad_0_copy": "T1",
         "camp_0_ad_1_creative_file": "b.png", "camp_0_ad_1_copy": "T2"}
    f.update(over)
    return f


def _multi_fields(**over):
    """Две группы: набор 0 (прежние имена) с двумя объявлениями и набор 1 (`camp_0_adset_1_*`) с одним."""
    return _fields(**{
        "camp_0_adset_count": "2", "camp_0_ad_count": "3",
        "camp_0_ad_0_adset": "0", "camp_0_ad_1_adset": "0", "camp_0_ad_2_adset": "1",
        "camp_0_ad_2_creative_file": "c.png", "camp_0_ad_2_copy": "T3",
        "camp_0_targeting": "PF", "camp_0_age": "25",
        "camp_0_adset_1_name": "Second", "camp_0_adset_1_targeting": "FX", "camp_0_adset_1_age": "40",
        "camp_0_adset_1_gender": "women", "camp_0_adset_1_language": "English",
        "camp_0_adset_1_os": "iOS", "camp_0_adset_1_device": "mobile", "camp_0_adset_1_os_min": "14",
        "camp_0_adset_1_start_time": "2026-10-20T10:00", "camp_0_adset_1_languages": "Portuguese",
        **over})


def test_old_forms_give_slots_without_any_ad_set_key():
    from src.web import app as web

    for slot in web._parse_build_upload_form(_fields())[2]:
        assert not {"adset_index", "adset_name", "pixel_id", "no_ad"} & set(slot)
    # явное «один набор» от новой страницы - те же слоты
    one = web._parse_build_upload_form(_fields(camp_0_adset_count="1", camp_0_ad_0_adset="0", camp_0_ad_1_adset="0"))
    assert one == web._parse_build_upload_form(_fields())


def test_an_old_draft_loads_as_one_ad_set():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_fields())
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert len(draft.ad_sets) == 1 and draft.ad_sets[0].adset_index == 0
    assert draft.ad_sets[0].name == draft.name and len(draft.ad_sets[0].ads) == 2
    prev = draft.ad_sets[0].to_preview_dict()
    assert set(prev) == {"name", "targeting", "os", "age", "daily_budget", "ads"}      # ни байтом больше
    # слоты, сохранённые ДО появления наборов (в заданиях на диске), читаются так же
    legacy = [{"name": "C", "creative_type": "Image", "creative_file": "a.png", "copy": "T", "targeting": "PF",
               "os": "All", "age": "35", "budget": "20", "tracking_link": "https://t.invalid/x"},
              {"name": "C", "creative_type": "Image", "creative_file": "b.png", "copy": "T2", "targeting": "PF",
               "os": "All", "age": "35", "budget": "20", "tracking_link": "https://t.invalid/x"}]
    [d2] = slots_to_campaign_drafts(legacy, account_label="Acc", country="MX")
    assert len(d2.ad_sets) == 1 and d2.ad_sets[0].name == "C" and len(d2.ad_sets[0].ads) == 2


def test_the_server_reads_every_new_field_of_every_ad_set():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_multi_fields())
    assert [(s["adset_index"], s["copy"]) for s in slots] == [(0, "T1"), (0, "T2"), (1, "T3")]
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    first, second = draft.ad_sets
    assert (first.adset_index, first.name, first.targeting_label, first.age_min, len(first.ads)) == (0, "MX_test", "PF", 25, 2)
    assert (second.adset_index, second.name, second.targeting_label, second.age_min, len(second.ads)) == (1, "Second", "FX", 40, 1)
    assert (second.gender, second.language, second.os, second.device, second.os_min, second.start_time,
            second.languages) == ("women", "English", "iOS", "mobile", "14", "2026-10-20T10:00", ["Portuguese"])
    assert (first.gender, first.language, first.languages) == ("", "", [])
    assert first.daily_budget == second.daily_budget == 20.0           # бюджет один, на кампании
    assert draft.language == "" and draft.errors == []


def test_an_unnamed_second_ad_set_gets_the_default_name_and_destinations_are_per_set():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_multi_fields(
        camp_0_adset_1_name="", camp_0_adset_1_destination="instant_form", camp_0_ad_2_form_id="555"))
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert [s.name for s in draft.ad_sets] == ["MX_test", "MX_test - Ad set 2"]
    assert [s.destination for s in draft.ad_sets] == ["website", "instant_form"]
    assert draft.ad_sets[1].ads[0].lead_form_id == "555" and draft.ad_sets[0].ads[0].lead_form_id == ""


def test_an_ad_set_without_ads_survives_the_form_and_is_refused_by_the_plan():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_multi_fields(camp_0_ad_2_adset="0"))
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert len(draft.ad_sets) == 2 and draft.ad_sets[1].ads == []
    assert any("No ads in ad set" in e for e in draft.errors)


def test_a_second_card_is_a_second_campaign_even_after_the_ad_set_fields_arrive():
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_multi_fields(
        camp_count="2", camp_1_name="Other", camp_1_ad_count="1", camp_1_budget="10",
        camp_1_ad_0_creative_file="z.png", camp_1_ad_0_copy="Z"))
    names = [s["name"] for s in slots]
    assert names.count("MX_test") == 3 and names.count("Other") == 1
    assert "adset_index" not in slots[-1]                  # у кампании с одним набором ключей набора нет


# ───────────────────────────── склейка по имени (мутационная проверка) ─────────────────────────────

def _slot(name, idx=None, **over):
    s = {"name": name, "creative_type": "Image", "creative_file": f"{name}{idx}.png", "copy": "T",
         "targeting": "PF", "os": "All", "age": "35", "budget": "20", "tracking_link": "https://t.invalid/x"}
    if idx is not None:
        s["adset_index"] = idx
    s.update(over)
    return s


def test_name_makes_a_campaign_and_adset_index_makes_ad_sets():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    slots = [_slot("A", 0), _slot("A", 1), _slot("A", 1, copy="T2"), _slot("B", 0)]
    a, b = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")
    assert [(s.adset_index, len(s.ads)) for s in a.ad_sets] == [(0, 1), (1, 2)]
    assert [(s.adset_index, len(s.ads)) for s in b.ad_sets] == [(0, 1)]


def test_slots_without_an_index_still_merge_into_one_ad_set():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    [a] = slots_to_campaign_drafts([_slot("A"), _slot("A", copy="T2"), _slot("A", copy="T3")],
                                   account_label="Acc", country="MX")
    assert len(a.ad_sets) == 1 and len(a.ad_sets[0].ads) == 3


def test_ad_sets_come_out_in_index_order_whatever_the_slot_order():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    [a] = slots_to_campaign_drafts([_slot("A", 2), _slot("A", 0), _slot("A", 1)], account_label="Acc", country="MX")
    assert [s.adset_index for s in a.ad_sets] == [0, 1, 2]
    assert [s.name for s in a.ad_sets] == ["A", "A - Ad set 2", "A - Ad set 3"]


# ───────────────────────────── RPA-бэкенд ─────────────────────────────

def test_the_rpa_backend_refuses_several_ad_sets_out_loud(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    resp = asyncio.run(web.preview_publish(_Req(_multi_fields())))
    body = json.loads(resp.body)
    assert resp.status_code == 409 and body["error"] == web.MULTI_ADSET_RPA_ERROR
    assert "Several ad sets need the API backend" in body["error"] and body["campaigns"] == ["MX_test"]
    resp = asyncio.run(web.publish_jobs_start(_Req({**_multi_fields(), "user_confirmed": "1"})))
    assert resp.status_code == 409 and "Several ad sets" in json.loads(resp.body)["error"]


def test_the_rpa_backend_still_takes_one_ad_set_and_the_api_backend_takes_several(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    assert web._multi_adset_refusal(web._parse_build_upload_form(_fields())[2]) is None
    assert web._multi_adset_refusal(web._parse_build_upload_form(
        _fields(camp_0_adset_count="1", camp_0_ad_0_adset="0", camp_0_ad_1_adset="0"))[2]) is None
    assert web._multi_adset_refusal(web._parse_build_upload_form(_multi_fields())[2]) is not None
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    assert web._multi_adset_refusal(web._parse_build_upload_form(_multi_fields())[2]) is None


def test_the_rpa_backend_does_not_silently_drop_an_ad_set_name(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    slots = web._parse_build_upload_form(_fields(camp_0_adset_0_name="Own name"))[2]
    resp = web._api_only_fields_refusal(slots)
    assert resp is not None and "Ad set name" in json.loads(resp.body)["error"]


def test_the_rpa_publisher_marks_a_draft_with_several_ad_sets_as_an_error(monkeypatch, tmp_path):
    from src.fb_ads_rpa import rpa_publisher as rp

    monkeypatch.setattr(rp, "enrich_campaign_drafts", lambda drafts, **kw: drafts)
    pub = rp.RpaPublisher.__new__(rp.RpaPublisher)
    slots = [_slot("A", 0), _slot("A", 1)]
    [d] = pub.build_drafts(slots, account_label="Acc", country="MX")
    assert any("Several ad sets need the API backend" in e for e in d.errors)
    [one] = pub.build_drafts([_slot("B")], account_label="Acc", country="MX")
    assert one.errors == []


def test_upload_fb_export_refuses_several_ad_sets():
    from src.web import app as web

    assert web._several_adsets(web._parse_build_upload_form(_multi_fields())[2]) == ["MX_test"]
    assert web._several_adsets(web._parse_build_upload_form(_fields())[2]) == []


# ───────────────────────────── pre-flight по наборам ─────────────────────────────

def test_preflight_reports_the_ad_set_that_is_wrong():
    from src.fb_api import preflight_rules as pf

    def ad(n):
        return {"name": f"n{n}", "file": "f.png", "form_id": "", "body": "b", "headline": "h", "cta": "LEARN_MORE"}

    def st(name, **o):
        return {"name": name, "destination": "website", "age_min": 30, "age_max": 40, "os": "All",
                "device": "", "os_min": "", "start_time": "", "ads": [ad(1)], **o}

    inp = {"objective": "leads", "link": "https://t.example.com/c?x=1", "country": "MX", "budget": "20",
           "now": "2026-10-12T10:00", "ad_sets": [st("A"), st("B", age_min=17), st("C", os="iOS", device="desktop")]}
    got = {v["rule"]: v for v in pf.evaluate_client(inp)}
    assert got["age_range"]["status"] == "error" and got["age_range"]["sets"] == [2]
    assert got["devices_consistent"]["sets"] == [3]
    assert got["adset_names_unique"]["status"] == "ok" and got["adset_has_ads"]["status"] == "ok"
    inp["ad_sets"][2]["ads"] = []
    got = {v["rule"]: v for v in pf.evaluate_client(inp)}
    assert got["adset_has_ads"]["sets"] == [3]


def test_preflight_server_side_resolves_every_ad_sets_language_and_pixel(tmp_path):
    from src.fb_api import preflight_rules as pf

    meta = FakeMeta()
    d = _draft(tmp_path)
    d.ad_sets[1].language = "Spanish"
    creator, _client = _creator(meta, writes=False)
    out = creator.plan_only(cr.plan_from_draft(d))
    verdicts = {v["rule"]: v for v in out.audit}
    assert verdicts["language_resolves"]["status"] == "ok" and verdicts["pixel_ok"]["status"] == "ok"
    assert pf.blocks(out.audit) is False


# ───────────────────────────── ACL-1767 (база): часовой пояс и «только десктоп» - по каждому набору ─────────────────────────────

def test_every_ad_set_gets_its_start_time_by_the_zone_of_the_account(tmp_path):
    from tests.test_acl1767_audience_fields import NOW, AudienceMeta
    from tests.test_acl1767_audience_fields import _creator as creator_at

    meta = AudienceMeta(tz="Europe/Madrid", now_offset=2)
    d = _draft(tmp_path)
    d.ad_sets[0].start_time = "2026-10-20T10:00"          # летнее время: +02:00
    d.ad_sets[1].start_time = "2026-10-30T10:00"          # летнее время кончилось 25.10: +01:00
    creator, _client = creator_at(meta, NOW)
    out = creator.run(cr.plan_from_draft(d))
    assert out.error == "" and out.readback["ok"], out.mismatches
    starts = {o["name"]: o["start_time"] for o in meta.objects.values() if o["_kind"] == "adset"}
    assert starts == {"MX Leads 1": "2026-10-20T10:00:00+0200", "MX Leads 1 - Ad set 2": "2026-10-30T10:00:00+0100"}
    checks = [c for c in out.readback["checks"] if c["field"] == "start_time"]
    assert len(checks) == 2 and all(c["ok"] for c in checks)


def test_an_unknown_zone_refuses_the_start_time_of_the_ad_set_that_has_one(tmp_path):
    from tests.test_acl1767_audience_fields import NOW, AudienceMeta
    from tests.test_acl1767_audience_fields import _creator as creator_at

    meta = AudienceMeta(tz="Mars/Olympus", now_offset=2)
    d = _draft(tmp_path)
    d.ad_sets[1].start_time = "2026-10-20T10:00"
    creator, _client = creator_at(meta, NOW)
    out = creator.run(cr.plan_from_draft(d))
    assert "MX Leads 1 - Ad set 2" in out.error and "time zone" in out.error and meta.posts == []


def test_desktop_only_is_refused_for_every_ad_set_not_only_the_first(tmp_path):
    d = _draft(tmp_path)
    d.ad_sets[1].device = "desktop"
    with pytest.raises(cr.PlanError, match=r"Ad set 'MX Leads 1 - Ad set 2': Desktop only"):
        cr.plan_from_draft(d)
    d = _draft(tmp_path)
    d.ad_sets[0].device = d.ad_sets[1].device = "desktop"
    with pytest.raises(cr.PlanError) as err:
        cr.plan_from_draft(d)
    assert str(err.value).count("Desktop only isn't possible") == 2


# ───────────────────── ревью 833: номер набора не угадывается ─────────────────────

BAD_INDEXES = ["abc", "-1", "1.5", "", "7", "99", " ", "1e1"]


@pytest.mark.parametrize("bad", BAD_INDEXES)
def test_a_bad_ad_to_set_value_in_the_form_is_refused_by_name_not_guessed(bad):
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_multi_fields(camp_0_ad_2_adset=bad))
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    shown = int(bad) + 1 if bad.lstrip("-").isdigit() else bad
    assert f"Ad 3 points to ad set {shown}, which does not exist. Nothing was created." in draft.errors
    # ни в набор 0, ни в придуманный набор: у набора 0 только его два объявления
    assert draft.ad_sets[0].ads and len(draft.ad_sets[0].ads) == 2
    assert len(draft.ad_sets) <= 2
    with pytest.raises(cr.PlanError, match="points to ad set"):
        cr.plan_from_draft(draft)


@pytest.mark.parametrize("bad", ["abc", -2, 1.5, "", None, 2, 99, True])
def test_a_bad_slot_adset_index_is_a_draft_error(bad):
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    slots = [_slot("A", 0, adset_count=2), _slot("A", 1, adset_count=2), _slot("A", copy="T2")]
    slots[2]["adset_index"], slots[2]["adset_count"] = bad, 2
    [a] = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")
    assert any("points to ad set" in e and "does not exist" in e for e in a.errors), a.errors
    assert sum(len(s.ads) for s in a.ad_sets) == 2          # плохое объявление нигде не оказалось


def test_a_missing_key_still_means_ad_set_zero_and_valid_values_pass():
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_multi_fields())
    assert [s["adset_index"] for s in slots] == [0, 0, 1] and all("form_errors" not in s for s in slots)
    f = _multi_fields()
    del f["camp_0_ad_1_adset"]                               # ключа нет -> набор 0, как у старых форм
    assert [s["adset_index"] for s in web._parse_build_upload_form(f)[2]] == [0, 0, 1]


# ───────────────────── ревью 833: потолок наборов и бюджет вызовов ─────────────────────

def test_the_form_refuses_more_than_ten_ad_sets():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_multi_fields(camp_0_adset_count="60"))
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert "A campaign can have at most 10 ad sets." in draft.errors
    assert len(draft.ad_sets) <= 10
    ok = web._parse_build_upload_form(_multi_fields(camp_0_adset_count="10"))[2]
    [fine] = slots_to_campaign_drafts(ok, account_label=label, country=country)
    assert "A campaign can have at most 10 ad sets." not in fine.errors


def test_the_plan_refuses_eleven_ad_sets_and_takes_ten(tmp_path):
    assert len(cr.plan_from_draft(_draft(tmp_path, sets=10, ads=1)).ad_sets) == 10
    with pytest.raises(cr.PlanError, match="at most 10 ad sets"):
        cr.plan_from_draft(_draft(tmp_path, sets=11, ads=1))


def test_the_page_disables_add_and_copy_at_ten_ad_sets():
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert "var atMax = sets.length >= 10;" in html and "var CB_MAX_ADSETS = 10;" in html
    assert "if (k >= CB_MAX_ADSETS) return null;" in html
    assert html.count("A campaign can have at most 10 ad sets.") >= 3


def _publisher_for(meta, **kw):
    from tests.test_acl1761_api_create import _publisher
    return _publisher(meta, **kw)


def test_ten_ad_sets_of_three_image_ads_fit_in_one_run_and_the_budget_scales(tmp_path):
    from src.web import publish_api

    draft = _draft(tmp_path, sets=10, ads=3)
    plan = cr.plan_from_draft(draft)
    budget, refusal = publish_api.create_call_budget(plan)
    assert refusal == "" and 184 < budget <= publish_api.DEFAULT_CREATE_MAX_CALLS
    small, _ = publish_api.create_call_budget(cr.plan_from_draft(_draft(tmp_path, sets=2, ads=3)))
    assert small < budget                                    # растёт с планом, а не одно число
    meta = FakeMeta()
    pub = _publisher_for(meta)
    res = pub.publish_draft(draft)
    assert res["success"] is True, res["error"]
    assert pub._client.calls <= budget
    assert len([o for o in meta.objects.values() if o["_kind"] == "adset"]) == 10
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 30


def test_a_campaign_too_big_for_one_run_is_refused_up_front_in_check_plan_and_in_a_run(tmp_path):
    draft = _draft(tmp_path, sets=10, ads=3)
    for s in draft.ad_sets:                                  # те же 30 объявлений, но видео
        for ad in s.ads:
            ad.creative_path = str(Path(ad.creative_path).with_suffix(".mp4"))
    for dry in (True, False):
        meta = FakeMeta()
        pub = _publisher_for(meta, dry_run=dry)
        res = pub.publish_draft(draft)
        assert res["success"] is False
        assert "This campaign needs about" in res["error"] and "more than one run allows — split it" in res["error"]
        assert meta.posts == []


def test_the_env_ceiling_still_clamps_the_budget(tmp_path, monkeypatch):
    from src.web import publish_api

    monkeypatch.setenv("ACL_FB_API_CREATE_MAX_CALLS", "60")
    budget, refusal = publish_api.create_call_budget(cr.plan_from_draft(_draft(tmp_path, sets=10, ads=3)))
    assert (budget, refusal) == (60, "")


# ───────────────────── ревью 833: две карточки с одним именем ─────────────────────

def _twin_fields(**over):
    base = dict(camp_count="2", camp_1_name="MX_test", camp_1_ad_count="1", camp_1_budget="20",
                camp_1_ad_0_creative_file="z.png", camp_1_ad_0_copy="Z", camp_1_targeting="ZZ", camp_1_age="50")
    base.update(over)
    return _multi_fields(**base)


def test_two_cards_with_the_same_name_are_refused_not_merged():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_twin_fields())
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert draft.errors.count("Two campaigns are named MX_test. Give each campaign a distinct name.") == 1
    with pytest.raises(cr.PlanError, match="Two campaigns are named MX_test"):
        cr.plan_from_draft(draft)


def test_two_single_set_cards_with_the_same_name_are_refused_too():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    f = _fields(camp_count="2", camp_1_name="MX_test", camp_1_ad_count="1", camp_1_budget="20",
                camp_1_ad_0_creative_file="z.png", camp_1_ad_0_copy="Z")
    country, label, slots = web._parse_build_upload_form(f)
    [draft] = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert "Two campaigns are named MX_test. Give each campaign a distinct name." in draft.errors


def test_distinct_names_are_not_twins():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    country, label, slots = web._parse_build_upload_form(_twin_fields(camp_1_name="Other"))
    drafts = slots_to_campaign_drafts(slots, account_label=label, country=country)
    assert [d.name for d in drafts] == ["MX_test", "Other"]
    assert not any("Two campaigns" in e for d in drafts for e in d.errors)


def test_the_twin_error_travels_in_form_errors_apart_from_the_1880_page_check():
    # 1880 (ветка facebook-page-selection) добавляет в конце группы свою проверку одинаковых имён с
    # разными Страницами; наша живёт в разборе формы (`form_errors`) и ложится в `draft.errors` отдельной строкой.
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    slots = [_slot("A", form_errors=["Two campaigns are named A. Give each campaign a distinct name."]), _slot("A")]
    [a] = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")
    assert a.errors == ["Two campaigns are named A. Give each campaign a distinct name."]


def test_upload_fb_export_refuses_a_custom_ad_set_name_or_pixel():
    from src.web import app as web

    for key, val in (("camp_0_adset_0_name", "My set"), ("camp_0_adset_0_pixel", "123")):
        slots = web._parse_build_upload_form(_fields(**{key: val}))[2]
        assert any(s.get("adset_name") or s.get("pixel_id") for s in slots)
    assert "Upload FB cannot hold a custom ad set name or pixel" in (ROOT / "src/web/app.py").read_text(encoding="utf-8")


# ───────────────────── ревью 833, раунд 2: бюджет вызовов и медленное видео ─────────────────────

def _video_draft(tmp_path, *, sets, ads=3):
    draft = _draft(tmp_path, sets=2, ads=ads)
    draft.ad_sets = (draft.ad_sets[:1] if sets == 1 else
                     draft.ad_sets + [_ad_set(tmp_path, f"MX Leads 1 - Ad set {i + 1}", i * ads, ads, adset_index=i)
                                      for i in range(2, sets)])
    for s in draft.ad_sets:
        for ad in s.ads:
            ad.creative_type = "Video"
            ad.creative_path = str(Path(ad.creative_path).with_suffix(".mp4"))
            Path(ad.creative_path).write_bytes(b"video-" + ad.creative_id.encode())
    return draft


@pytest.mark.parametrize("sets", [1, 3, 5])
def test_slow_video_processing_never_exhausts_the_call_budget(tmp_path, sets):
    from src.web import publish_api

    draft = _video_draft(tmp_path, sets=sets)
    budget, refusal = publish_api.create_call_budget(cr.plan_from_draft(draft))
    assert refusal == ""
    meta = FakeMeta(video_states=("processing",) * 7 + ("ready",))      # 8 опросов на каждое видео
    pub = _publisher_for(meta)
    res = pub.publish_draft(draft)
    assert res["success"] is True, res["error"]
    assert pub._client.calls < budget


def test_the_call_budget_is_never_below_the_old_fixed_150_for_small_plans(tmp_path):
    from src.web import publish_api

    for sets, ads in ((2, 1), (2, 3), (3, 3)):
        budget, refusal = publish_api.create_call_budget(cr.plan_from_draft(_draft(tmp_path, sets=sets, ads=ads)))
        assert refusal == "" and budget == 150
    one = _draft(tmp_path, sets=2, ads=1)
    one.ad_sets = one.ad_sets[:1]
    assert publish_api.create_call_budget(cr.plan_from_draft(one)) == (150, "")


def test_an_upper_limit_video_plan_is_refused_up_front_not_stopped_midway(tmp_path):
    from src.web import publish_api

    assert publish_api.create_call_budget(cr.plan_from_draft(_video_draft(tmp_path, sets=5)))[1] == ""
    draft = _video_draft(tmp_path, sets=6)                    # 6 x 3 видео: больше 250 при 8 опросах на видео
    assert "split it" in publish_api.create_call_budget(cr.plan_from_draft(draft))[1]
    meta = FakeMeta()
    res = _publisher_for(meta).publish_draft(draft)
    assert res["success"] is False and "split it" in res["error"] and meta.posts == []
