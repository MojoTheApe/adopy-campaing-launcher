"""ACL-1880: «Automatic» никогда не выбирает наугад среди нескольких Страниц.

Решение владельца 2026-10-13: страница карты (если она среди страниц кабинета) →
единственная страница кабинета → отказ до любой записи, если страниц несколько →
оболочка/отказ, если страниц нет. Одно правило для API и RPA; Python и JS зеркальны.
Без сети и без браузера.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.fb_api import creator as cr
from src.fb_api import page_choice as pc
from src.fb_api import preflight_rules as pf

ROOT = Path(__file__).resolve().parents[1]
SEVERAL = "This ad account has 2 Facebook Pages. Choose the Page for this campaign in the builder."


def facts(pages):
    return cr.AccountFacts(act="act_1234567890", currency="USD", page_names={p: p for p in pages}, pages=list(pages),
                           pixels=[], utc_offset_hours=None, timezone_name="")


def plan(page_id="", map_page_id="", instant=False):
    return SimpleNamespace(page_id=page_id, page_source="draft" if page_id else "", map_page_id=map_page_id,
                           ad_sets=[SimpleNamespace(instant=instant)])


# ───────────── 2a-2d: правило на Python ─────────────

def test_2a_map_page_among_the_pages_wins_over_order():
    p = plan(map_page_id="456")
    cr.resolve_page(p, facts(["123", "456"]))
    assert (p.page_id, p.page_source) == ("456", "map")


def test_2b_single_page_is_the_account_default():
    p = plan()
    cr.resolve_page(p, facts(["123"]))
    assert (p.page_id, p.page_source) == ("123", "account default")


def test_2b_map_page_missing_from_the_account_falls_back_to_the_single_page():
    p = plan(map_page_id="999")
    cr.resolve_page(p, facts(["123"]))
    assert (p.page_id, p.page_source) == ("123", "account default")


@pytest.mark.parametrize("map_page", ["", "999"])
def test_2c_several_pages_without_a_usable_map_page_refuse(map_page):
    p = plan(map_page_id=map_page)
    with pytest.raises(cr.PlanError) as err:
        cr.resolve_page(p, facts(["123", "456"]))
    assert str(err.value) == SEVERAL
    assert p.page_id == "", "ничего не подставлено"


def test_2d_zero_pages_keep_the_manual_shell_for_website_and_refuse_instant_forms():
    p = plan()
    notes = cr.resolve_page(p, facts([]))
    assert p.page_id == "" and p.page_source == "manual completion" and notes == [cr.MANUAL_PAGE_NOTICE]
    with pytest.raises(cr.PlanError, match="Instant Forms require a Facebook Page"):
        cr.resolve_page(plan(instant=True), facts([]))


def test_a_page_chosen_in_the_builder_is_used_and_must_be_on_the_account():
    p = plan(page_id="456", map_page_id="123")
    cr.resolve_page(p, facts(["123", "456"]))
    assert p.page_id == "456"
    with pytest.raises(cr.PlanError, match="not among"):
        cr.resolve_page(plan(page_id="789"), facts(["123", "456"]))


def test_enrich_hands_the_map_page_over_as_a_candidate_only(tmp_path):
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts
    mapping = tmp_path / "map.csv"
    mapping.write_text("account_name,ad_account_id,page_id\nDemo,act_1234567890,999\n")
    d = slots_to_campaign_drafts([{"name": "C", "budget": "50", "page_id": ""}], account_label="Demo", country="MX")[0]
    enrich_campaign_drafts([d], account_label="Demo", upload_fb_path=str(tmp_path / "none.xlsx"),
                           ad_account_map_path=str(mapping), creatives_root=str(tmp_path), preview_mode=True)
    assert not d.page_id and d.map_page_id == "999"


# ───────────── RPA: те же ответы, тот же текст ─────────────

PAGES = [{"id": "123", "name": "First"}, {"id": "456", "name": "Second"}]


def test_rpa_selection_follows_the_same_rule():
    from src.web.account_pages import resolve_selection
    assert resolve_selection(PAGES, "", "456")["id"] == "456"
    assert resolve_selection(PAGES[:1], "")["id"] == "123"
    with pytest.raises(ValueError) as err:
        resolve_selection(PAGES, "")
    assert str(err.value) == SEVERAL
    assert resolve_selection(PAGES, "123", "456")["id"] == "123"


def test_rpa_publisher_refuses_before_the_browser_and_uses_the_map_page(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rpa
    from src.fb_ads_rpa.draft import CampaignDraft
    from src.web import account_pages
    for map_page, want_error in [("", True), ("999", True), ("456", False)]:
        draft = CampaignDraft("C", "MX", "Demo", page_id=None, page_provided=True, ad_account_id="act_1234567890",
                              map_page_id=map_page)
        monkeypatch.setattr(rpa, "slots_to_campaign_drafts", lambda *a, **k: [draft])
        monkeypatch.setattr(rpa, "enrich_campaign_drafts", lambda drafts, **k: drafts)
        monkeypatch.setattr(account_pages, "read_pages",
                            lambda account: {"ok": True, "act": "act_1234567890", "pages": PAGES})
        out = rpa.RpaPublisher().build_drafts([], account_label="Demo", country="MX")[0]
        assert bool(out.errors) is want_error, (map_page, out.errors)
        if want_error:
            assert out.errors == [SEVERAL]
        else:
            assert out.page_id == "456" and out.page_source == "map"


# ───────────── JS: правило и текст опции ─────────────

def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    return node


def _ready(pages, map_page=""):
    return {"ok": True, "state": "ready", "pages": pages, "map_page_id": map_page}


SCENARIOS = {
    "several_no_map": {"payload": _ready(PAGES)},
    "several_map_among": {"payload": _ready(PAGES, "456")},
    "several_map_foreign": {"payload": _ready(PAGES, "999")},
    "single": {"payload": _ready(PAGES[:1])},
    "single_with_foreign_map": {"payload": _ready(PAGES[:1], "999")},
    "several_chosen": {"payload": _ready(PAGES), "choose": "456"},
    "zero": {"payload": {"ok": True, "state": "empty", "pages": [], "message": "No Page"}},
}


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    path = tmp_path_factory.mktemp("acl1880") / "scenarios.json"
    path.write_text(json.dumps(SCENARIOS), encoding="utf-8")
    r = subprocess.run([_node(), str(ROOT / "tests" / "acl1880_pages_harness.cjs"),
                        str(ROOT / "src/web/static/account_pages.js"), str(path)],
                       capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_option_text_and_slot_note(js):
    s = js["several_no_map"]
    assert s["options"][0] == "Choose a Page (this account has 2)"
    assert s["note"] == SEVERAL and s["resolved"] == "" and s["needs"] == SEVERAL
    assert js["several_map_among"]["options"][0] == "Automatic: Second (456)"
    assert js["several_map_among"]["resolved"] == "456" and js["several_map_among"]["needs"] == ""
    assert js["several_map_foreign"]["options"][0] == "Choose a Page (this account has 2)"
    assert js["single"]["options"][0] == "Automatic: First (123)" and js["single"]["resolved"] == "123"
    assert js["single_with_foreign_map"]["resolved"] == "123"
    assert js["several_chosen"]["note"] == "Selected: Second (456)" and js["several_chosen"]["needs"] == ""
    assert js["zero"]["options"][0] == "Automatic" and js["zero"]["resolved"] == ""


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_js_and_python_pick_the_same_page(js, name):
    sc = SCENARIOS[name]
    ids = [p["id"] for p in sc["payload"]["pages"]]
    if sc.get("choose"):
        assert js[name]["resolved"] == sc["choose"]
        return
    try:
        want, _ = pc.automatic_page(ids, sc["payload"].get("map_page_id", ""))
    except pc.PageChoiceError:
        want = ""
    assert js[name]["resolved"] == want


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_js_audit_input_gives_the_python_verdict(js, name):
    """Вход аудита со страницы -> правило page_choice на Python: ошибка ровно тогда, когда страница просит выбор."""
    audit = js[name]["audit"]
    py = {v["rule"]: v for v in pf.evaluate_client({"destination": "website", "ads": [], **audit})}["page_choice"]
    assert (py["status"] == "error") == (js[name]["needs"] != "")
