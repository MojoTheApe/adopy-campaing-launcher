# -*- coding: utf-8 -*-
"""Второй вариант основного текста доезжает от билдера до робота (ACL-1344).

Путь тот же, что у первого текста: поле формы `camp_N_ad_M_copy2` (название
записи библиотеки текстов) → слот `copy2` → `AdDraft.copy2_name` → обогащение
кладёт body записи в `AdDraft.ad_body2` → `AdsManagerFlow` передаёт его в
записанный поток (`ad_body2` у первого объявления, ключ `ad_body2` у
следующих) → `write_ad_copy(body2=...)`. У объявлений без второго текста
ключа нет нигде — они идут ровно как раньше.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import ads_manager_flow as amf  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import config as rpa_config  # noqa: E402
from src.fb_ads_rpa import enrich_draft as en  # noqa: E402
from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from src.fb_ads_rpa import page_capture, screenshots  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts  # noqa: E402
from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft  # noqa: E402
from src.fb_ads_rpa.preview import preview_campaign_drafts  # noqa: E402

LIBRARY = {
    "Text6": {"headline": "Regístrate", "body": "Texto uno\nlínea dos", "description": ""},
    "Text7": {"headline": "Otro", "body": "Texto dos\n\nlínea final", "description": ""},
    "Empty": {"headline": "H", "body": "", "description": ""},
    "Same": {"headline": "H", "body": "Texto  uno línea\ndos", "description": ""},
}


# ── форма → слоты → черновик ───────────────────────────────────────────────

def _form(**ads):
    form = {"country": "Saudi Arabia", "account_label": "Acc", "camp_count": "1",
            "camp_0_name": "SA_x", "camp_0_budget": "35", "camp_0_ad_count": str(len(ads))}
    for i, (copy, copy2) in enumerate(ads.values()):
        form[f"camp_0_ad_{i}_creative_type"] = "Image"
        form[f"camp_0_ad_{i}_creative_file"] = f"{2040 + i}_a.png"
        form[f"camp_0_ad_{i}_copy"] = copy
        if copy2 is not None:
            form[f"camp_0_ad_{i}_copy2"] = copy2
    return form


def test_the_form_field_becomes_a_slot_key_only_when_filled():
    from src.web import app as web

    _c, _a, slots = web._parse_build_upload_form(
        _form(a=("Text6", "Text7"), b=("Text6", ""), c=("Text6", None)))
    assert slots[0]["copy2"] == "Text7"
    assert "copy2" not in slots[1] and "copy2" not in slots[2], "пустое поле — как раньше"


def test_the_slot_becomes_the_ads_copy2_name():
    drafts = slots_to_campaign_drafts(
        [{"name": "SA_x", "copy": "Text6", "copy2": "Text7", "budget": "35"},
         {"name": "SA_x", "copy": "Text6", "budget": "35"}],
        account_label="Acc", country="SA")
    ads = drafts[0].ad_sets[0].ads
    assert [a.copy2_name for a in ads] == ["Text7", ""]


# ── обогащение: body записи библиотеки ─────────────────────────────────────

@pytest.fixture
def enrich(monkeypatch, tmp_path):
    monkeypatch.setattr(en, "load_ad_account_map", lambda p: {})
    monkeypatch.setattr(en, "resolve_account_entry",
                        lambda *a: SimpleNamespace(ad_account_id="act_test", page_id="page", profile_id="profile"))

    def lookup(name):
        from src.analysis_agent.ad_text_registry import AdTextError

        if name not in LIBRARY:
            raise AdTextError(f"{name!r} is not in the ad text library")
        return LIBRARY[name]

    monkeypatch.setattr(en, "lookup_ad_copy_by_title", lookup)

    def run(*ads):
        draft = CampaignDraft("SA_x", "SA", "Acc", ad_sets=[AdSetDraft("SA_x", ads=list(ads))])
        en.enrich_campaign_drafts([draft], account_label="Acc",
                                  upload_fb_path=str(tmp_path / "none.xlsx"),
                                  ad_account_map_path=str(tmp_path / "map.csv"),
                                  creatives_root=str(tmp_path), preview_mode=True)
        return draft

    return run


def test_enrich_takes_the_body_of_the_second_entry(enrich):
    d = enrich(AdDraft(copy_name="Text6", copy2_name="Text7"), AdDraft(copy_name="Text6"))
    first, second = d.ad_sets[0].ads
    assert first.ad_body == "Texto uno\nlínea dos"
    assert first.ad_body2 == "Texto dos\n\nlínea final", "line breaks are kept"
    assert first.ad_title == "Regístrate", "заголовок остаётся от первой записи"
    assert second.ad_body2 == ""
    assert not [e for e in d.errors if "text" in e.lower()], d.errors


def test_an_unknown_second_entry_is_an_error_before_the_browser(enrich):
    d = enrich(AdDraft(copy_name="Text6", copy2_name="Gone"))
    assert any(e.startswith("Primary text 2 'Gone'") for e in d.errors), d.errors


def test_an_empty_or_repeated_second_text_is_dropped_with_a_warning(enrich):
    d = enrich(AdDraft(copy_name="Text6", copy2_name="Empty"), AdDraft(copy_name="Text6", copy2_name="Same"))
    assert [a.ad_body2 for a in d.ad_sets[0].ads] == ["", ""]
    assert any("has no body" in w and "Primary text 2" in w for w in d.warnings), d.warnings
    assert any("repeats the first primary text" in w for w in d.warnings), d.warnings


def test_preview_shows_the_second_text_only_where_it_is_set():
    ad1 = AdDraft(ad_title="T", ad_body="uno", copy2_name="Text7", ad_body2="dos")
    ad2 = AdDraft(ad_title="T", ad_body="uno")
    row = preview_campaign_drafts([CampaignDraft("C", "SA", "Acc", ad_sets=[AdSetDraft("C", ads=[ad1, ad2])])])[0]
    assert row["Ad 1 — Body 2"] == "dos"
    assert "Ad 2 — Body 2" not in row
    assert row["_ads_raw"][0]["ad_body2"] == "dos"
    assert "ad_body2" not in row["_ads_raw"][1]
    assert "copy2_name" not in ad2.to_preview_dict()


# ── AdsManagerFlow → записанный поток ──────────────────────────────────────

def _publish(monkeypatch, draft, soft=()):
    seen: dict = {}

    def fake_flow(page, *, campaign_name, soft_fails_out, **kw):
        seen.update(kw)
        soft_fails_out.extend(soft)
        return ""

    monkeypatch.setattr(amf, "run_recorded_campaign_flow", fake_flow)
    monkeypatch.setattr(rpa_config, "get_use_recorded_flow", lambda: True)
    monkeypatch.setattr(amf.AdsManagerFlow, "_goto_campaigns", lambda self, d, r: None)
    monkeypatch.setattr(amf.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(br, "put_window_back", lambda page: None)
    flow = amf.AdsManagerFlow.__new__(amf.AdsManagerFlow)
    flow.page = None
    return flow.publish_campaign(draft), seen


def _two_ads(body2_first="", body2_second=""):
    ad = AdDraft(creative_type="Image", creative_path="x.png", ad_title="t", ad_body="b", ad_body2=body2_first)
    ad2 = AdDraft(creative_type="Image", creative_path="y.png", ad_title="t2", ad_body="b2", ad_body2=body2_second)
    return CampaignDraft(name="SA_x", country="SA", account_label="Acc",
                         ad_sets=[AdSetDraft(name="SA_x", daily_budget=35.0, ads=[ad, ad2])])


@pytest.fixture
def quiet_telemetry(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    rt.clear_current_run()
    yield
    rt.clear_current_run()


def test_the_flow_hands_the_second_text_to_the_robot(monkeypatch, quiet_telemetry):
    _result, kw = _publish(monkeypatch, _two_ads("B1-2", "B2-2"))
    assert kw["ad_body2"] == "B1-2"
    assert kw["additional_ads"][0]["ad_body2"] == "B2-2"


def test_ads_without_a_second_text_carry_no_new_key(monkeypatch, quiet_telemetry):
    _result, kw = _publish(monkeypatch, _two_ads())
    assert "ad_body2" not in kw
    assert "ad_body2" not in kw["additional_ads"][0]


def test_a_missing_second_text_is_a_warning_on_the_result(monkeypatch, quiet_telemetry):
    line = "ad_second_text:Ad 1 second primary text not added (add_control_not_found)"
    result, _kw = _publish(monkeypatch, _two_ads("B1-2"), soft=[line])
    assert line in result.warnings
    assert not result.blocking_failures, "второй текст не блокирует кампанию"
    assert "DO NOT PUBLISH" not in (result.error or "")


# ── записанный поток → write_ad_copy ───────────────────────────────────────

@pytest.fixture
def recorded(monkeypatch, tmp_path):
    """`run_recorded_campaign_flow` на подставной странице, как в
    test_additional_ad_without_creative_has_no_text.py."""
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    page = MagicMock()
    loc = MagicMock()
    loc.first = loc
    loc.bounding_box.return_value = None
    loc.input_value.return_value = "Example"
    loc.evaluate.return_value = None
    for name in ("get_by_role", "get_by_text", "get_by_placeholder", "locator"):
        getattr(page, name).return_value = loc
        getattr(loc, name).return_value = loc
    page.evaluate.return_value = None
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit"
    monkeypatch.setattr(br, "ensure_editor_window", lambda p, section: True)
    monkeypatch.setattr(dom, "_clear_blocking_overlays", MagicMock(return_value=[]))
    for name in ("_recover_from_browser_error", "_dismiss_floating_banners", "_sleep_h",
                 "wait_editor_saved", "_ensure_campaign_budget", "scroll_to_section",
                 "click_conversion_location_dropdown", "pick_website_conversion_option",
                 "is_website_conversion_selected", "select_conversion_event_lead",
                 "fill_audience_age", "fill_placements"):
        monkeypatch.setattr(dom, name, MagicMock(return_value=True))
    monkeypatch.setattr(dom, "_roots", lambda p: [p])
    monkeypatch.setattr(dom, "_first_visible", lambda *a, **kw: None)
    monkeypatch.setattr(rt, "window_state", lambda p: {})
    monkeypatch.setattr(rt, "page_context", lambda p: {})
    monkeypatch.setattr(screenshots, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(dom, "upload_ad_creative_with_retry", lambda p, ctype, path, **kw: True)
    monkeypatch.setattr(dom, "create_additional_ad", lambda p, name: True)
    monkeypatch.setattr(dom, "_open_tree_ad", lambda p, name: False)
    monkeypatch.setattr(dom, "_tree_shell_ids", lambda p: frozenset())
    monkeypatch.setattr(dom, "set_ad_destination_website", lambda p, url: True)
    monkeypatch.setattr(dom, "verify_and_fix_ad_destinations", lambda p, names, url: [])
    monkeypatch.setattr(dom, "recover_stuck_page", lambda *a, **k: False)
    monkeypatch.setattr(dom, "_loading_visible", lambda p: False)

    state = {"calls": [], "second_ok": {}}

    def write_copy(p, title, body, **kw):
        state["calls"].append((body, kw.get("body2")))
        if "body2" in kw:
            ok = state["second_ok"].get(body, False)
            kw["report"]["second_text"] = {"ok": ok} if ok else {"ok": False, "why": "add_control_not_found"}
        return True

    monkeypatch.setattr(dom, "write_ad_copy", write_copy)

    def run(body2_first="", body2_second=""):
        soft: list[str] = []
        extra = {"ad_name": "Ad 2", "creative_type": "Image", "creative_path": "b.png",
                 "ad_title": "t2", "ad_body": "b2"}
        if body2_second:
            extra["ad_body2"] = body2_second
        kwargs = {"ad_body2": body2_first} if body2_first else {}
        result = dom.run_recorded_campaign_flow(
            page, campaign_name="Example", budget="10", adset_name="Set", ad_name="Ad 1",
            creative_path="a.png", ad_title="t1", ad_body="b1",
            destination_url="https://example.com", additional_ads=[extra],
            soft_fails_out=soft, **kwargs)
        return result, soft

    state["run"] = run
    state["page"] = page
    return state


def test_each_ad_gets_its_own_second_text(recorded):
    recorded["second_ok"] = {"b1": True, "b2": True}
    result, soft = recorded["run"]("b1-second", "b2-second")
    assert result == ""
    assert recorded["calls"] == [("b1", "b1-second"), ("b2", "b2-second")]
    assert not any("second_text" in s for s in soft), soft


def test_a_second_text_that_did_not_take_is_reported_per_ad(recorded):
    recorded["second_ok"] = {"b1": True}
    _result, soft = recorded["run"]("b1-second", "b2-second")
    assert "ad2_second_text:Ad 2 second primary text not added (add_control_not_found)" in soft
    assert not any(s.startswith("ad_second_text") for s in soft), soft


def test_a_second_text_is_not_lost_silently_when_the_copy_step_is_skipped(recorded, monkeypatch):
    """Ревью PR #346: нет медиа у объявления 2 — шаг текста не запускался, а
    выбранный второй текст пропадал молча. Теперь — строка-предупреждение."""
    monkeypatch.setattr(dom, "upload_ad_creative_with_retry",
                        lambda p, ctype, path, **kw: path != "b.png")
    _result, soft = recorded["run"]("", "b2-second")
    assert "ad2_second_text:Ad 2 second primary text not added (no_creative)" in soft, soft


def test_a_second_text_without_a_primary_text_is_reported(recorded):
    soft: list[str] = []
    dom.run_recorded_campaign_flow(
        recorded["page"], campaign_name="Example", budget="10", adset_name="Set", ad_name="Ad 1",
        creative_path="a.png", ad_title="", ad_body="", ad_body2="b1-second",
        destination_url="https://example.com", soft_fails_out=soft)
    assert "ad_second_text:Ad 1 second primary text not added (no_primary_text)" in soft, soft


def test_enrich_warns_when_text_2_has_no_ad_title(enrich):
    d = enrich(AdDraft(copy2_name="Text7"))
    assert d.ad_sets[0].ads[0].ad_body2 == ""
    assert any("needs an Ad Title" in w for w in d.warnings), d.warnings


def test_the_recorded_flow_without_second_texts_is_unchanged(recorded):
    _result, soft = recorded["run"]()
    assert recorded["calls"] == [("b1", None), ("b2", None)]
    assert not any("second_text" in s for s in soft), soft
