"""ACL-1766: пиксель и страница из карты кабинетов доходят до черновика и плана (только подделки)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402


class TwoPixelMeta(FakeMeta):
    """Кабинет с заданным списком пикселей."""

    def __init__(self, pixels, **kw):
        super().__init__(**kw)
        self.pixels = pixels

    def _get(self, path, params):
        if path.strip("/") == f"{ACT}/adspixels":
            return {"data": [{"id": p} for p in self.pixels]}
        return super()._get(path, params)


# ───────────────────────── enrich_draft ─────────────────────────

def _enrich(tmp_path, *, map_pixel="", map_page="", draft_pixel="", draft_page=None):
    from src.fb_ads_rpa import enrich_draft as en
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    (tmp_path / "map.csv").write_text(
        "account_name,ad_account_id,page_id,pixel_id,profile_id\n"
        f"Acc,{ACT},{map_page},{map_pixel},prof\n", encoding="utf-8")
    d = CampaignDraft("C", "MX", "Acc", pixel_id=draft_pixel, page_id=draft_page,
                      ad_sets=[AdSetDraft("C", ads=[AdDraft()])])
    en.enrich_campaign_drafts([d], account_label="Acc", upload_fb_path=str(tmp_path / "none.xlsx"),
                              ad_account_map_path=str(tmp_path / "map.csv"),
                              creatives_root=str(tmp_path), preview_mode=True)
    return d


def test_enrich_carries_map_pixel_and_page(tmp_path):
    d = _enrich(tmp_path, map_pixel="777", map_page="555")
    assert (d.pixel_id, d.page_id, d.pixel_source, d.page_source) == ("777", "555", "map", "map")


def test_enrich_keeps_what_the_draft_already_has(tmp_path):
    d = _enrich(tmp_path, map_pixel="777", map_page="555", draft_pixel="888", draft_page="666")
    assert (d.pixel_id, d.page_id, d.pixel_source, d.page_source) == ("888", "666", "draft", "draft")


def test_enrich_without_map_values_leaves_the_draft_empty(tmp_path):
    d = _enrich(tmp_path)
    assert not d.pixel_id and not d.page_id and d.pixel_source == "" and d.page_source == ""


# ───────────────────────── creator ─────────────────────────

def _plan(tmp_path, pixel="", source=""):
    d = _draft(tmp_path)
    d.pixel_id = pixel
    if source:
        d.pixel_source = source
    return cr.plan_from_draft(d)


def _facts(meta):
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    return cr.preflight_account(client, ACT)


def test_map_pixel_is_used_when_the_account_has_two(tmp_path):
    plan = _plan(tmp_path, "333", "map")
    cr.resolve_page_pixel(plan, _facts(TwoPixelMeta(["222", "333"])))
    assert (plan.pixel_id, plan.pixel_source) == ("333", "map")


def test_map_pixel_not_on_the_account_is_a_clear_error(tmp_path):
    plan = _plan(tmp_path, "999", "map")
    with pytest.raises(cr.PlanError) as e:
        cr.resolve_page_pixel(plan, _facts(TwoPixelMeta(["222", "333"])))
    assert "pixel from the map (999) is not on this ad account" in str(e.value)
    assert plan.pixel_id == "999"          # другой пиксель молча не подставлен


def test_no_map_pixel_and_one_account_pixel_works_as_before(tmp_path):
    plan = _plan(tmp_path)
    cr.resolve_page_pixel(plan, _facts(FakeMeta()))
    assert (plan.pixel_id, plan.pixel_source) == ("222", "account default")


def test_no_map_pixel_and_two_account_pixels_keeps_the_previous_error(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(cr.PlanError, match="2 pixels/datasets; the API backend needs exactly one"):
        cr.resolve_page_pixel(plan, _facts(TwoPixelMeta(["222", "333"])))


def test_plan_summary_shows_pixel_and_page_source(tmp_path):
    from tests.test_acl1761_api_create import _publisher

    meta = TwoPixelMeta(["222", "333"])
    draft = _draft(tmp_path)
    draft.pixel_id, draft.pixel_source = "333", "map"
    pub = _publisher(meta, dry_run=True)
    res = pub.publish_draft(draft)
    s = res["plan"]["summary"]
    assert (s["pixel_id"], s["pixel_source"], s["page_source"]) == ("333", "map", "draft")
    assert meta.posts == []
    # страницы в черновике нет, в кабинете одна → источник «account default»
    draft2 = _draft(tmp_path)
    draft2.page_id = None
    s2 = _publisher(FakeMeta(), dry_run=True).publish_draft(draft2)["plan"]["summary"]
    assert (s2["page_source"], s2["pixel_source"]) == ("account default", "account default")


# ───────────────────────── readback ─────────────────────────

def test_readback_covers_cta_and_locales_and_marks_not_read(tmp_path):
    out, _ = _run(FakeMeta(), _draft(tmp_path))
    rb = out.readback
    fields = {c["field"] for c in rb["checks"]}
    assert {"call_to_action", "languages"} <= fields and rb["ok"] is True
    assert not any(c["not_read"] for c in rb["checks"])
    meta = FakeMeta()
    meta.mutate_ads = {"creative.object_story_spec.link_data.call_to_action": None}
    out, _ = _run(meta, _draft(tmp_path))
    cta = [c for c in out.readback["checks"] if c["field"] == "call_to_action" and not c["ok"]]
    assert cta and all(c["not_read"] for c in cta)


# ───────────────────────── ревью #794 ─────────────────────────

class NoPagesMeta(FakeMeta):
    def _get(self, path, params):
        if path.strip("/") == f"{ACT}/promote_pages":
            return {"data": []}
        return super()._get(path, params)


@pytest.mark.parametrize("pixels", [[], ["222", "333"]])
def test_foreign_map_pixel_writes_nothing_end_to_end(tmp_path, pixels):
    meta = TwoPixelMeta(pixels)
    draft = _draft(tmp_path)
    draft.pixel_id, draft.pixel_source = "999", "map"
    out, _ = _run(meta, draft)
    assert meta.posts == [] and "pixel from the map (999) is not on this ad account" in out.error


def test_empty_pixel_list_with_a_set_pixel_is_an_error(tmp_path):
    plan = _plan(tmp_path, "999", "map")
    with pytest.raises(cr.PlanError, match="0 found"):
        cr.resolve_page_pixel(plan, _facts(TwoPixelMeta([])))


@pytest.mark.parametrize("meta_cls", [FakeMeta, NoPagesMeta])
def test_foreign_or_unlisted_map_page_writes_nothing(tmp_path, meta_cls):
    meta = meta_cls()
    draft = _draft(tmp_path)
    draft.page_id, draft.page_source = "555", "map"
    out, _ = _run(meta, draft)
    assert meta.posts == [] and "page from the map (555)" in out.error


def test_error_text_does_not_repeat_nothing_was_created(tmp_path):
    meta = TwoPixelMeta(["222", "333"])
    draft = _draft(tmp_path)
    draft.pixel_id, draft.pixel_source = "999", "map"
    out, _ = _run(meta, draft)
    assert "Nothing was created. Nothing more" not in out.error


def test_a_campaign_without_language_reads_back_ok(tmp_path):
    draft = _draft(tmp_path)
    draft.language = ""
    out, _ = _run(FakeMeta(), draft)
    assert out.error == "" and out.readback["ok"] is True


def test_dropped_locales_read_back_as_not_read(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path))
    assert out.readback["ok"] is True
    # Meta не вернула locales у группы, хотя их ждали
    meta2 = FakeMeta()
    orig = meta2._get

    def drop(path, params):
        body = orig(path, params)
        if isinstance(body, dict) and isinstance(body.get("targeting"), dict):
            body = {**body, "targeting": {k: v for k, v in body["targeting"].items() if k != "locales"}}
        return body

    meta2._get = drop
    out2, _ = _run(meta2, _draft(tmp_path))
    lang = [c for c in out2.readback["checks"] if c["field"] == "languages"]
    assert lang and all(c["ok"] is False and c["not_read"] is True for c in lang)
