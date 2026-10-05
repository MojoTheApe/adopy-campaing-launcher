"""ACL-1593: конкретный вариант креатива — в имени объявления и в разбивке."""
from __future__ import annotations

import pandas as pd
import pytest

from src.analysis_agent.analyze_fb_stats import (
    compute_metrics,
    enrich_structure,
    split_ad_title,
    split_ad_title_variant,
)
from src.analysis_agent.morning_report import build_creative_breakdown
from src.fb_ads_rpa.draft import AdDraft
from src import creative_variant as cv


# --- имя объявления ---------------------------------------------------------

def test_ad_name_carries_the_variant_from_the_file_name():
    ad = AdDraft(creative_filename="1_es_2403_post.mp4", copy_name="Pro Bancolombia")
    assert ad.ad_name() == "2403 1 - Pro Bancolombia"


def test_ad_name_variant_with_builder_creative_id():
    ad = AdDraft(creative_id="2403", creative_filename="4_es_2403_post.mp4", copy_name="Text6")
    assert ad.ad_name() == "2403 4 - Text6"


def test_ad_name_without_leading_number_is_unchanged():
    ad = AdDraft(creative_filename="es_2403_post.mp4", copy_name="Pro Bancolombia")
    assert ad.ad_name() == "2403 - Pro Bancolombia"
    ad = AdDraft(creative_filename="2403_es_post.mp4", copy_name="Text6")
    assert ad.ad_name() == "2403 - Text6"


def test_ad_name_without_file_is_unchanged():
    assert AdDraft(creative_id="2046", copy_name="Text6").ad_name() == "2046 - Text6"


def test_file_variant():
    assert cv.file_variant("1_es_2403_post.mp4") == "1"
    assert cv.file_variant("C:" + chr(92) + "x" + chr(92) + "12_es_2403_story.mp4") == "12"
    assert cv.file_variant("2403_es_post.mp4") == ""
    assert cv.file_variant("coffee.mp4") == ""


# --- split_ad_title ---------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("2046 1 - Story14", ("2046", "1", "Story14")),
    ("2307_2- 6", ("2307", "2", "6")),
    ("2257 Story3", ("2257", "", "Story3")),
    ("2046 - 6", ("2046", "", "6")),
    ("Blue - modern", ("Blue", "", "modern")),
    ("", ("", "", "")),
])
def test_split_ad_title_variant(title, expected):
    assert split_ad_title_variant(title) == expected


@pytest.mark.parametrize("title,expected", [
    ("2046 1 - Story14", ("2046", "Story14")),
    ("2307_2- 6", ("2307", "6")),
    ("2257 Story3", ("2257", "Story3")),
    ("Vid 2257 - Story13", ("2257", "Story13")),
    ("2046- Powered", ("2046", "Powered")),
])
def test_split_ad_title_keeps_the_group_contract(title, expected):
    assert split_ad_title(title) == expected


# --- разбивка ---------------------------------------------------------------

def _frame(rows):
    df = pd.DataFrame(rows, columns=["Ad Title", "Adset Name", "# Leads", "# FTDs", "$ Spend"])
    df["Country"] = "Colombia"
    df["Country Final"] = "Colombia"
    df["Account Alias"] = "A1"
    df["CM"] = "x"
    df["Ad Asset"] = "o"
    df["$ Net"] = 0.0
    return enrich_structure(compute_metrics(df))


ROWS = [
    ("1962 1 - Text6", "CAMP_A", 10, 2, 100.0),
    ("1962 1 - Text6", "CAMP_B", 5, 1, 40.0),
    ("1962 2 - Text6", "CAMP_A", 8, 0, 70.0),
    ("1962 - Text6", "CAMP_C", 4, 1, 30.0),
    ("2403 Story3", "CAMP_A", 3, 0, 10.0),
]


def test_breakdown_expands_group_into_variants_that_add_up():
    out = build_creative_breakdown(_frame(ROWS), "Colombia")
    group = next(r for r in out if r["creative_id"] == "1962")
    assert (group["deposits"], group["leads"], group["spend"]) == (4, 27, 240.0)
    labels = [v["label"] for v in group["variants"]]
    assert labels == ["1962 · 1", "1962 · 2", "1962 · no variant"]
    for key in ("deposits", "leads", "spend"):
        assert sum(v[key] for v in group["variants"]) == pytest.approx(group[key])
    v1 = group["variants"][0]
    assert (v1["deposits"], v1["leads"], v1["spend"]) == (3, 15, 140.0)
    assert v1["cpa"] == pytest.approx(140.0 / 3)


def test_breakdown_group_without_any_variant_has_no_variant_rows():
    out = build_creative_breakdown(_frame(ROWS), "Colombia")
    plain = next(r for r in out if r["creative_id"] == "2403")
    assert [v["label"] for v in plain["variants"]] == ["2403 · no variant"]
    assert plain["has_variants"] is False
    assert next(r for r in out if r["creative_id"] == "1962")["has_variants"] is True


# --- подстановка из записей робота -----------------------------------------

def _pair(campaign, square, number="2403"):
    return {"step": "ad_creative_pair", "campaign": campaign,
            "diag": {"square": square, "vertical": square.replace("post", "story"), "number": number}}


def test_backfill_takes_the_variant_from_the_robot_record():
    vmap = cv.variant_map([_pair("CAMP_A", "3_es_2403_post.mp4"),
                           _pair("CAMP_A", "3_es_2403_post.mp4")])  # повтор при ретрае
    df = _frame([("2403 - Pro Bancolombia", "CAMP_A", 3, 1, 10.0)])
    out = cv.apply_backfill(df, vmap)
    assert out["VariantId"].tolist() == ["3"]
    row = build_creative_breakdown(out, "Colombia")[0]
    assert [v["label"] for v in row["variants"]] == ["2403 · 3"]


def test_backfill_never_guesses():
    # Два варианта одного креатива в одной кампании — по названию не различить.
    vmap = cv.variant_map([_pair("CAMP_A", "1_es_2403_post.mp4"),
                           _pair("CAMP_A", "4_es_2403_post.mp4")])
    assert ("CAMP_A", "2403") not in vmap
    df = _frame([("2403 - Pro", "CAMP_A", 3, 1, 10.0),   # неоднозначно
                 ("2403 - Pro", "CAMP_Z", 3, 1, 10.0)])  # кампании нет в записях
    out = cv.apply_backfill(df, vmap)
    assert out["VariantId"].tolist() == ["", ""]


def test_backfill_does_not_override_a_variant_in_the_title():
    vmap = cv.variant_map([_pair("CAMP_A", "3_es_2403_post.mp4")])
    out = cv.apply_backfill(_frame([("2403 1 - Pro", "CAMP_A", 3, 1, 10.0)]), vmap)
    assert out["VariantId"].tolist() == ["1"]


def test_record_without_a_leading_number_gives_no_variant():
    assert cv.variant_map([_pair("CAMP_A", "es_2403_post.mp4")]) == {}
