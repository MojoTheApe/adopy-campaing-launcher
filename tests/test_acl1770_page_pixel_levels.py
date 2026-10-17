"""ACL-1770: Страница и ссылка на объявлении, пиксель на наборе, выбор уходит в карту (только подделки)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402


class AccountMeta(FakeMeta):
    """Кабинет с заданными Страницами и пикселями."""

    def __init__(self, pages=("111", "333"), pixels=("222", "444"), **kw):
        super().__init__(**kw)
        self.pages, self.pixels = list(pages), list(pixels)

    def _get(self, path, params):
        edge = path.strip("/")
        if edge == f"{ACT}/promote_pages":
            return {"data": [{"id": p, "name": f"Page {p}"} for p in self.pages]}
        if edge == f"{ACT}/adspixels":
            return {"data": [{"id": p, "name": f"Pixel {p}"} for p in self.pixels]}
        return super()._get(path, params)


def _spec_pages(meta):
    """Страницы, с которыми создавались креативы (по порядку)."""
    out = []
    for path, body in meta.posts:
        if path.endswith("/adcreatives"):
            spec = body.get("object_story_spec")
            if isinstance(spec, str):
                import json
                spec = json.loads(spec)
            out.append(spec["page_id"])
    return out


def _refused(meta, draft, pattern):
    """Создатель не пишет ничего и называет причину (ошибка плана приходит в `out.error`)."""
    import re

    try:
        out, _ = _run(meta, draft)
    except cr.PlanError as exc:
        text = str(exc)
    else:
        text = out.error
    assert re.search(pattern, text), text
    assert meta.posts == []


# ───────────────────────── Страница на объявлении ─────────────────────────

def test_second_page_on_the_second_ad_is_created_and_read_back(tmp_path):
    draft = _draft(tmp_path)
    draft.pixel_id = "222"
    draft.ad_sets[0].ads[1].page_id = "333"
    meta = AccountMeta()
    out, _ = _run(meta, draft)
    assert out.error == "", out.error
    assert _spec_pages(meta) == ["111", "333"]
    pages = [c for c in out.readback["checks"] if c["field"] == "page"]
    assert [(c["expected"], c["actual"], c["ok"]) for c in pages] == [("111", "111", True), ("333", "333", True)]
    assert out.readback["ok"] is True


def test_a_page_of_an_ad_that_is_not_on_the_account_names_the_ad(tmp_path):
    draft = _draft(tmp_path)
    draft.pixel_id = "222"
    draft.ad_sets[0].ads[1].page_id = "999"
    meta = AccountMeta()
    _refused(meta, draft, r"page of ad 2 \(.*\) is not among the pages")


def test_every_ad_with_its_own_page_needs_no_campaign_page(tmp_path):
    draft = _draft(tmp_path)
    draft.page_id = None
    draft.pixel_id = "222"
    for ad, page in zip(draft.ad_sets[0].ads, ("111", "333")):
        ad.page_id = page
    out, _ = _run(AccountMeta(), draft)
    assert out.error == "" and out.readback["ok"] is True


def test_without_any_page_and_two_on_the_account_the_old_error_stays(tmp_path):
    draft = _draft(tmp_path)
    draft.page_id = None
    draft.pixel_id = "222"
    _refused(AccountMeta(), draft, "page_id is empty in the account map and the ad account has 2 pages")


def test_an_instant_form_ad_cannot_use_another_page(tmp_path):
    draft = _draft(tmp_path)
    draft.ad_sets[0].destination = "instant_form"
    for ad in draft.ad_sets[0].ads:
        ad.lead_form_id = "F1"
    draft.ad_sets[0].ads[0].page_id = "333"
    _refused(AccountMeta(), draft, "Instant Form, which belongs to the Page of its ad set")


# ───────────────────────── Ссылка на объявлении ─────────────────────────

def test_an_ad_link_overrides_the_campaign_link_in_the_creative_and_the_readback(tmp_path):
    draft = _draft(tmp_path)
    draft.pixel_id = "222"
    draft.ad_sets[0].ads[1].link = "https://example.invalid/b?x={{campaign.id}}&y={{adset.name}}"
    meta = AccountMeta()
    out, _ = _run(meta, draft)
    assert out.error == "", out.error
    links = [c for c in out.readback["checks"] if c["field"] == "link"]
    assert [c["expected"] for c in links] == ["https://example.invalid/l",
                                              "https://example.invalid/b?x={{campaign.id}}&y={{adset.name}}"]
    assert all(c["ok"] for c in links)


def test_an_empty_link_is_refused_and_the_ad_is_named(tmp_path):
    draft = _draft(tmp_path)
    draft.link = ""
    draft.ad_sets[0].ads[0].link = "https://example.invalid/a"
    with pytest.raises(cr.PlanError) as e:
        cr.plan_from_draft(draft)
    text = str(e.value)
    assert "Tracking link is empty for ad 2 (" in text and "for ad 1" not in text


def test_no_link_anywhere_keeps_the_previous_message(tmp_path):
    draft = _draft(tmp_path)
    draft.link = ""
    with pytest.raises(cr.PlanError, match="Tracking link is empty; the API backend needs it for the ads"):
        cr.plan_from_draft(draft)


# ───────────────────────── Пиксель на наборе ─────────────────────────

def test_the_pixel_of_an_ad_set_goes_into_its_promoted_object(tmp_path):
    draft = _draft(tmp_path)
    draft.pixel_id = "222"
    draft.ad_sets[0].pixel_id = "444"
    meta = AccountMeta()
    out, _ = _run(meta, draft)
    assert out.error == "", out.error
    sets = [b for p, b in meta.posts if p.endswith("/adsets")]
    assert sets[0]["promoted_object"]["pixel_id"] == "444"
    px = [c for c in out.readback["checks"] if c["field"] == "pixel"]
    assert [(c["expected"], c["ok"]) for c in px] == [("444", True)]


def test_a_set_pixel_that_is_not_on_the_account_is_refused_and_nothing_is_created(tmp_path):
    draft = _draft(tmp_path)
    draft.pixel_id = "222"
    draft.ad_sets[0].pixel_id = "999"
    meta = AccountMeta()
    _refused(meta, draft, r"pixel of ad set 'MX Leads 1' \(999\) is not on this ad account")


def test_a_set_pixel_removes_the_need_for_a_campaign_pixel(tmp_path):
    draft = _draft(tmp_path)
    draft.pixel_id = ""
    draft.ad_sets[0].pixel_id = "444"
    out, _ = _run(AccountMeta(), draft)
    assert out.error == "" and out.readback["ok"] is True


def test_plan_summary_shows_page_and_pixel_per_level(tmp_path):
    from tests.test_acl1761_api_create import _publisher

    draft = _draft(tmp_path)
    draft.pixel_id = "222"
    draft.ad_sets[0].pixel_id = "444"
    draft.ad_sets[0].ads[1].page_id = "333"
    res = _publisher(AccountMeta(), dry_run=True).publish_draft(draft)
    s = res["plan"]["summary"]["ad_sets"][0]
    assert (s["pixel_id"], s["pixel_source"]) == ("444", "ad set")
    assert "page_id" not in s["ads"][0] and (s["ads"][1]["page_id"], s["ads"][1]["page_source"]) == ("333", "ad")
    assert s["ads"][0]["link"] == "https://example.invalid/l"


# ───────────────────────── форма → слоты → черновик ─────────────────────────

class _Form(dict):
    def getlist(self, k):
        return [self[k]] if k in self else []


def _form(**extra):
    base = {"country": "MX", "account_label": "Acc", "tracking_link": "https://e.invalid/l", "camp_count": "1",
            "camp_0_ad_count": "2", "camp_0_budget": "20", "camp_0_ad_0_copy": "T0", "camp_0_ad_1_copy": "T1"}
    base.update(extra)
    return _Form(base)


def test_form_slots_carry_the_new_keys_only_when_something_is_chosen():
    from src.web.app import _parse_build_upload_form

    _, _, plain = _parse_build_upload_form(_form())
    assert all(not ({"page_id", "link", "pixel_id"} & set(s)) for s in plain)
    _, _, slots = _parse_build_upload_form(_form(camp_0_ad_1_page="333", camp_0_ad_1_link=" https://e.invalid/b ",
                                                 camp_0_pixel="444"))
    assert "page_id" not in slots[0] and slots[1]["page_id"] == "333" and slots[1]["link"] == "https://e.invalid/b"
    assert slots[0]["pixel_id"] == slots[1]["pixel_id"] == "444"


def test_slots_become_ad_level_page_link_and_set_level_pixel():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts

    slots = [{"name": "C", "tracking_link": "https://e.invalid/l", "budget": "20", "pixel_id": "444"},
             {"name": "C", "tracking_link": "https://e.invalid/l", "budget": "20", "pixel_id": "444",
              "page_id": "333", "link": "https://e.invalid/b"}]
    d = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")[0]
    assert d.ad_sets[0].pixel_id == "444"
    assert [(a.page_id, a.link) for a in d.ad_sets[0].ads] == [("", ""), ("333", "https://e.invalid/b")]


def test_the_robot_backend_refuses_the_new_choices_out_loud(monkeypatch):
    from src.web import app as webapp
    from src.web import publish_api

    monkeypatch.setattr(publish_api, "api_enabled", lambda: False)
    res = webapp._instant_form_refusal([{"page_id": "333"}])
    assert res is not None and res.status_code == 409 and b"API backend only" in res.body
    assert webapp._instant_form_refusal([{"name": "x"}]) is None
    monkeypatch.setattr(publish_api, "api_enabled", lambda: True)
    assert webapp._instant_form_refusal([{"page_id": "333"}]) is None


# ───────────────────────── выбор в карту ─────────────────────────

def _map(tmp_path, page="", pixel="", extra_rows=""):
    p = tmp_path / "map.csv"
    p.write_text("account_name,ad_account_id,page_id,pixel_id,profile_id,tracking_link\n"
                 f"Acc,{ACT},{page},{pixel},prof,https://e.invalid/l\n{extra_rows}", encoding="utf-8")
    return p


def test_the_choice_is_saved_to_the_empty_map_cells_and_used_next_time(tmp_path):
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map
    from src.web import page_pixel_options as po

    p = _map(tmp_path)
    assert po.save_choice("Acc", page_id="333", pixel_id="444", map_path=p) == {"page_id": "333", "pixel_id": "444"}
    e = load_ad_account_map(p)["Acc"]
    assert (e.page_id, e.pixel_id, e.tracking_link, e.profile_id) == ("333", "444", "https://e.invalid/l", "prof")


def test_a_different_value_in_the_map_is_never_overwritten(tmp_path):
    from src.web import page_pixel_options as po

    p = _map(tmp_path, page="111")
    with pytest.raises(po.SaveRefused, match="already has a Page \\(111\\)") as e:
        po.save_choice("Acc", page_id="333", map_path=p)
    assert e.value.status == 409 and "111" in p.read_text(encoding="utf-8")
    assert po.save_choice("Acc", page_id="111", map_path=p) == {}      # то же значение - ничего не пишем


def test_save_refuses_bad_ids_unknown_accounts_and_empty_requests(tmp_path):
    from src.web import page_pixel_options as po

    p = _map(tmp_path)
    for kw, status in (({"page_id": "abc"}, 400), ({}, 400)):
        with pytest.raises(po.SaveRefused) as e:
            po.save_choice("Acc", map_path=p, **kw)
        assert e.value.status == status
    with pytest.raises(po.SaveRefused) as e:
        po.save_choice("Nobody", page_id="333", map_path=p)
    assert e.value.status == 404


def test_save_adds_missing_columns_without_dropping_others(tmp_path):
    from src.web import page_pixel_options as po

    p = tmp_path / "map.csv"
    p.write_text("account_name,ad_account_id,profile_id,note\nAcc,act_1,prof,keep me\n", encoding="utf-8")
    po.save_choice("Acc", pixel_id="444", map_path=p)
    head, row = p.read_text(encoding="utf-8").strip().splitlines()[:2]
    assert head.split(",")[:4] == ["account_name", "ad_account_id", "profile_id", "note"] and "pixel_id" in head
    assert "keep me" in row and "444" in row


def test_options_are_read_with_get_only(tmp_path):
    from src.fb_api import graph as g
    from src.web import page_pixel_options as po

    meta = AccountMeta()
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)
    got = po.read_options_with(client, ACT)
    assert [p["id"] for p in got["pages"]] == ["111", "333"] and [p["id"] for p in got["pixels"]] == ["222", "444"]
    assert meta.posts == []


# ───────────────────────── страница ─────────────────────────

def _page() -> str:
    return (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")


def test_the_page_reads_pages_and_pixels_only_on_a_click_and_saves_only_through_the_map_endpoint():
    page = _page()
    i = page.index("window.cbPagePixels = window.cbPagePixels || {};")
    glue = page[i:page.index("/* Место конверсии набора", i)]
    assert glue.count("fetch('/api/page-pixels'") == 1 and glue.count("fetch('/api/account-defaults'") == 1
    assert "setInterval" not in glue and "setTimeout" not in glue and "requestAnimationFrame" not in glue
    assert page.count('onclick="cbRefreshPagePixels(this)"') == 1
    assert "'/api/page-pixels'" not in page.replace(glue, "")


def test_the_levels_are_where_the_spec_puts_them():
    page = _page()
    row = page[page.index("function buildAdRowHTML"):]
    row = row[: row.index("// ── Add / Remove ad within campaign")]
    slot = page[page.index("function buildBlankSlotHTML"):]
    slot = slot[: slot.index("// ── Tracking link save")]
    # Страница и ссылка - в строке объявления, пиксель - в карточке набора.
    assert "pfx + 'page\"" in row and "pfx + 'link\"" in row and "_pixel" not in row
    assert "idx + '_pixel\"" in slot and "_page" not in slot
