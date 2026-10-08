# -*- coding: utf-8 -*-
"""Второе объявление без медиа: текст не пишем и называем причину сразу (ACL-1313).

Прогон 24.09 (SA Mix, da86e97cbf8b): креатив второго объявления не встал, а
шаг текста всё равно искал поля — без медиа Meta их не рисует вовсе. Минуты
уходили на поиск, а в итоге стояло «has no text» без причины. Теперь при
отказе заливки текст не пробуется, а отказ читается
`ad2_copy:<имя> has no text (no_creative)`.

`run_recorded_campaign_flow` гоняется целиком на подставной странице
(`MagicMock`, как в test_editor_window_guards.py); шаги объявления подменены,
чтобы проверять именно развилку «нет медиа — нет текста».
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as dom  # noqa: E402
from src.fb_ads_rpa import page_capture, screenshots  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402


@pytest.fixture
def flow(monkeypatch, tmp_path):
    # Телеметрия — во временный файл, не в живой output/.
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

    state = {"uploads": [], "copies": [], "fail_upload": {"b.png"}, "raise_upload": set()}

    def upload(p, ctype, path, **kw):
        state["uploads"].append(path)
        if path in state["raise_upload"]:
            raise RuntimeError("chooser closed")
        return path not in state["fail_upload"]

    def write_copy(p, title, body, **kw):
        state["copies"].append((kw.get("ad_name"), title, body))
        return True

    monkeypatch.setattr(dom, "upload_ad_creative_with_retry", upload)
    monkeypatch.setattr(dom, "write_ad_copy", write_copy)
    monkeypatch.setattr(dom, "create_additional_ad", lambda p, name: True)
    monkeypatch.setattr(dom, "_open_tree_ad", lambda p, name: False)
    monkeypatch.setattr(dom, "_tree_shell_ids", lambda p: frozenset())
    monkeypatch.setattr(dom, "set_ad_destination_website", lambda p, url: True)
    monkeypatch.setattr(dom, "verify_and_fix_ad_destinations", lambda p, names, url: [])
    monkeypatch.setattr(dom, "recover_stuck_page", lambda *a, **k: False)
    monkeypatch.setattr(dom, "_loading_visible", lambda p: False)

    def run():
        soft: list[str] = []
        result = dom.run_recorded_campaign_flow(
            page, campaign_name="Example", budget="10", adset_name="Set",
            creative_path="a.png", ad_title="t1", ad_body="b1",
            destination_url="https://example.com",
            additional_ads=[{"ad_name": "Ad 2", "creative_type": "Image", "creative_path": "b.png",
                             "ad_title": "t2", "ad_body": "b2"}],
            soft_fails_out=soft,
        )
        return result, soft

    state["run"] = run
    state["page"] = page
    return state


def test_an_ad_whose_creative_failed_gets_no_text_attempt(flow):
    result, soft = flow["run"]()
    assert result == ""
    assert flow["uploads"] == ["a.png", "b.png"]
    assert [c[1:] for c in flow["copies"]] == [("t1", "b1")], "текст второго объявления писали без медиа"
    assert "ad2_creative_upload" in soft
    assert "ad2_copy:Ad 2 has no text (no_creative)" in soft


def test_an_upload_that_raised_counts_as_no_creative(flow):
    flow["fail_upload"] = set()
    flow["raise_upload"] = {"b.png"}
    _result, soft = flow["run"]()
    assert any(s.startswith("ad2_creative_upload:") for s in soft), soft
    assert "ad2_copy:Ad 2 has no text (no_creative)" in soft
    assert all(c[1:] != ("t2", "b2") for c in flow["copies"])


def test_an_ad_with_its_creative_in_place_gets_its_text(flow):
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert ("Ad 2", "t2", "b2") in flow["copies"]
    assert not any("no_creative" in s for s in soft), soft
