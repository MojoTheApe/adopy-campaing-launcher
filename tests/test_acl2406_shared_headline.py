"""ACL-2406: объявления 2+ берут заголовок и описание у Ad 1; у них только свои Primary text.

Без Meta и браузера: план из черновика, зеркальные правила аудита (Python и JS на коде шаблона),
текст шаблона страницы для UI.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa.draft import share_first_headline  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from tests.test_acl1761_api_create import _draft  # noqa: E402
from tests.test_acl1819_preflight import TPL, _harness, _node, _ok_input  # noqa: E402,F401


def _plan_ads(draft):
    return cr.plan_from_draft(draft).ad_sets[0].ads


def test_one_ad_keeps_its_own_headline_and_description(tmp_path):
    ad, = _plan_ads(_draft(tmp_path, ads=1))
    assert (ad.title, ad.description, ad.titles, ad.descriptions) == ("Head 0", "Desc", ["Head 0"], ["Desc"])


def test_ad_two_takes_headline_and_description_from_ad_one_and_keeps_its_own_body(tmp_path):
    d = _draft(tmp_path, ads=3)
    d.ad_sets[0].ads[0].ad_link_description = "Desc one"
    d.ad_sets[0].ads[1].ad_title = ""          # no headline of its own
    d.ad_sets[0].ads[2].ad_title = "Own headline that must be ignored"
    one, two, three = _plan_ads(d)
    assert (one.title, one.description) == ("Head 0", "Desc one")
    for ad, body in ((two, "Body 1"), (three, "Body 2")):
        assert (ad.title, ad.description) == ("Head 0", "Desc one")
        assert ad.titles == ["Head 0"] and ad.descriptions == ["Desc one"]
        assert ad.body == body and ad.bodies == [body]


def test_several_texts_of_ad_two_share_the_headline_and_description_of_ad_one(tmp_path):
    from src.fb_ads_rpa.draft import AdSetDraft
    d = _draft(tmp_path, ads=2)
    ad2 = d.ad_sets[0].ads.pop()
    ad2.text_variants = [
        {"name": "T2", "body": "Body 1 second", "headline": "Variant headline", "description": "Variant desc"},
        {"name": "T3", "body": "Body 1 third", "headline": "Another", "description": "Another desc"}]
    d.ad_sets.append(AdSetDraft(name="MX Leads 2", targeting_label="Swing trading", os="All", age_min=25,
                                age_max=55, daily_budget=20.0, ads=[ad2]))
    plan = cr.plan_from_draft(d)
    two = plan.ad_sets[1].ads[0]
    assert two.bodies == ["Body 1", "Body 1 second", "Body 1 third"]
    assert two.titles == ["Head 0"] and two.descriptions == ["Desc"]


def test_ad_one_keeps_several_own_headlines(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].ads[0].text_variants = [{"name": "T2", "body": "B2", "headline": "H2", "description": "D2"}]
    one, = _plan_ads(d)
    assert one.titles == ["Head 0", "H2"] and one.descriptions == ["Desc", "D2"]


def test_removing_ad_one_makes_the_next_ad_the_source(tmp_path):
    d = _draft(tmp_path, ads=3)
    del d.ad_sets[0].ads[0]
    first, second = _plan_ads(d)
    assert first.title == "Head 1" and second.title == "Head 1" and second.body == "Body 2"


def test_ad_one_is_the_first_ad_of_the_campaign_across_ad_sets(tmp_path):
    from src.fb_ads_rpa.draft import AdSetDraft
    d = _draft(tmp_path, ads=2)
    second_set = AdSetDraft(name="MX Leads 2", targeting_label="Swing trading", os="All", age_min=25, age_max=55,
                            daily_budget=20.0, ads=[d.ad_sets[0].ads.pop()])
    d.ad_sets.append(second_set)
    plan = cr.plan_from_draft(d)
    assert [a.title for s in plan.ad_sets for a in s.ads] == ["Head 0", "Head 0"]


def test_share_first_headline_is_idempotent_and_leaves_ad_one_alone(tmp_path):
    d = _draft(tmp_path, ads=2)
    share_first_headline(d)
    share_first_headline(d)
    one, two = d.ad_sets[0].ads
    assert (one.ad_title, two.ad_title, two.ad_link_description) == ("Head 0", "Head 0", "Desc")
    assert two.ad_body == "Body 1"


# ───────────────────────────── pre-flight audit ─────────────────────────────

def _two_ads(**second):
    inp = _ok_input()
    inp["ads"][1].update(second)
    return inp


def _verdict(inp, rule):
    return next(v for v in pf.evaluate_client(inp) if v["rule"] == rule)


def test_missing_headline_of_ad_two_does_not_warn_but_ad_one_does():
    inp = _two_ads(headline="", headlines=[])
    assert _verdict(inp, "headline")["status"] == "ok"
    inp = _ok_input()
    inp["ads"][0].update(headline="", headlines=[])
    v = _verdict(inp, "headline")
    assert v["status"] == "warning" and v["ads"] == [1]


def test_long_own_headline_of_ad_two_is_ignored_by_the_audit():
    assert _verdict(_two_ads(headline="x" * 60), "headline_len")["status"] == "ok"


def test_several_texts_of_ad_two_need_no_headline_of_its_own():
    inp = _two_ads(bodies=["a", "b"], headline="", headlines=[])
    inp["ads"][0].update(headline="Head", headlines=["Head"])
    assert _verdict(inp, "feed_texts")["status"] == "ok"
    inp["ads"][0].update(headline="", headlines=[])
    assert _verdict(inp, "feed_texts")["ads"] == [2]


def test_python_and_page_audit_agree_on_shared_headlines():
    cases = []
    for ad1, ad2 in (({}, {"headline": "", "headlines": []}),
                     ({"headline": "", "headlines": []}, {"headline": "", "headlines": []}),
                     ({"headline": "Head", "headlines": ["Head"]}, {"bodies": ["a", "b"], "headline": "", "headlines": []}),
                     ({"headline": "Head", "headlines": ["Head"]}, {"headline": "z" * 60, "headlines": ["z" * 60]})):
        inp = _ok_input()
        inp["ads"][0].update(ad1)
        inp["ads"][1].update(ad2)
        cases.append(inp)
    got = _harness("--evaluate", {str(i): c for i, c in enumerate(cases)})
    for i, inp in enumerate(cases):
        want = pf.evaluate_client(inp)
        assert [(v["rule"], v["status"], v.get("ads", [])) for v in got[str(i)]] ==                [(v["rule"], v["status"], v.get("ads", [])) for v in want]


# ───────────────────────────── builder UI ─────────────────────────────

def test_builder_hides_headline_of_ads_two_plus_and_preview_uses_ad_one():
    page = TPL.read_text(encoding="utf-8")
    assert "var shared = i > 0;" in page and "cb-hl-shared" in page
    assert "!shared && pick && pick.value" in page            # no headline line under ads 2+
    assert "The headline and description come from Ad 1." in page
    assert "hlCopy" in page                                    # the card preview shows Ad 1's headline


def test_duplicate_ad_copies_primary_texts_and_no_headline_or_description_field():
    page = TPL.read_text(encoding="utf-8")
    start = page.index("function _copyAdRowValues")
    body = page[start:page.index("window.cbDuplicateAd", start)]
    # Headline and description are not fields of an ad row: they come from the library entry of Ad 1.
    assert "headline" not in body.lower() and "description" not in body.lower()
    for sel in (".cb-adtitle-sel-wrap", ".cb-text2-sel-wrap", ".cb-textx-sel-wrap"):
        assert sel in body
