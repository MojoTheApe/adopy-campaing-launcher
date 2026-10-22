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


# ACL-2090: вариант в конце имени («2175_ARM22.png»), ведущий — в приоритете.
@pytest.mark.parametrize("name,variant", [
    ("2175_ARM22.png", "22"), ("2175_ARM21.png", "21"), ("2175_ARM16.png", "16"),
    ("2175_ADN3.png", "3"), ("2403_story_3.mp4", "3"), ("2175_ARM07.png", "7"),
    ("2175_ARM22_1080x1920.png", "22"), ("2175_ARM22_1080" + chr(0x445) + "1920_9x16.png", "22"),
    ("2046 1.jpg", "1"),
    ("1_es_2408_post.mp4", "1"), ("1_workers_1962_ar_facebook_1080x1080.jpg", "1"),
    ("12_2175_ARM22.png", "12"),
    ("2175_post_1x1.png", ""), ("2175_post_4x5.jpg", ""), ("2175_post_9x16.mp4", ""),
    ("2175_post_9:16.mp4", ""), ("2175_square_1080x1080.jpg", ""), ("cac-2285.png", ""),
    ("2403x1080_3011.png", ""), ("1328 - 26s.mp4", ""), ("2175_ARM_story.png", ""),
    ("2175_ARM22_Story.png", ""), ("coffee.mp4", ""),
])
def test_file_variant_from_the_end_of_the_name(name, variant):
    assert cv.file_variant(name) == variant


def test_b97_ads_with_the_variant_at_the_end_get_different_names():
    names = [AdDraft(creative_filename=f, copy_name="Text1").ad_name()
             for f in ("2175_ARM22.png", "2175_ARM21.png", "2175_ARM16.png")]
    assert names == ["2175 22 - Text1", "2175 21 - Text1", "2175 16 - Text1"]
    # Отчёты разбирают новые имена так же, как «2046 1 - Text6».
    assert [split_ad_title_variant(n) for n in names] == [
        ("2175", "22", "Text1"), ("2175", "21", "Text1"), ("2175", "16", "Text1")]


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


# --- ревью #597: аккаунты, скорость, нормализация, защита от удаления --------

def _frame2(rows):
    """rows: (title, adset, account, country, leads, ftds, spend)"""
    df = pd.DataFrame(rows, columns=["Ad Title", "Adset Name", "Account Alias", "Country",
                                     "# Leads", "# FTDs", "$ Spend"])
    df["CM"] = "x"
    df["Ad Asset"] = "o"
    df["$ Net"] = 0.0
    return enrich_structure(compute_metrics(df))


def test_backfill_skips_a_campaign_name_shared_by_two_accounts():
    vmap = cv.variant_map([_pair("CAMP_A", "3_es_2403_post.mp4")])
    df = _frame2([("2403 - Pro", "CAMP_A", "AC1", "Colombia", 3, 1, 10.0),
                  ("2403 - Pro", "CAMP_A", "AC2", "Colombia", 3, 1, 10.0),
                  ("2403 - Pro", "CAMP_B", "AC1", "Colombia", 3, 1, 10.0)])
    out = cv.apply_backfill(df, vmap)
    assert out["VariantId"].tolist() == ["", "", ""]


def test_backfill_skips_a_campaign_name_shared_by_two_countries():
    vmap = cv.variant_map([_pair("CAMP_A", "3_es_2403_post.mp4")])
    df = _frame2([("2403 - Pro", "CAMP_A", "AC1", "Colombia", 3, 1, 10.0),
                  ("2403 - Pro", "CAMP_A", "AC1", "Mexico", 3, 1, 10.0)])
    assert cv.apply_backfill(df, vmap)["VariantId"].tolist() == ["", ""]


def test_backfill_still_fills_a_campaign_of_one_account_over_many_rows():
    vmap = cv.variant_map([_pair("CAMP_A", "3_es_2403_post.mp4")])
    df = _frame2([("2403 - Pro", "CAMP_A", "AC1", "Colombia", 3, 1, 10.0),
                  ("2403 - Pro", "CAMP_A", "AC1", "Colombia", 4, 0, 12.0),
                  ("2403 - Pro", "CAMP_B", "AC2", "Colombia", 3, 1, 10.0)])
    assert cv.apply_backfill(df, vmap)["VariantId"].tolist() == ["3", "3", ""]


def test_numeric_variants_are_normalised():
    assert cv.file_variant("01_es_2403_post.mp4") == "1"
    assert split_ad_title_variant("2046 01 - Story14") == ("2046", "1", "Story14")
    assert split_ad_title_variant("2046 1 - Story14")[1] == "1"
    df = _frame([("1962 01 - Text6", "CAMP_A", 5, 1, 40.0), ("1962 1 - Text6", "CAMP_B", 5, 1, 40.0)])
    group = build_creative_breakdown(df, "Colombia")[0]
    assert [v["label"] for v in group["variants"]] == ["1962 · 1"]
    assert group["variants"][0]["leads"] == 10


def test_journal_is_prefiltered_and_cached(tmp_path, monkeypatch):
    import json as _json
    j = tmp_path / "rpa_telemetry.jsonl"
    other = _json.dumps({"step": "ad_name", "campaign": "CAMP_A"})
    j.write_text("\n".join([other] * 50 + ["not json ad_creative_pair {",
                                           _json.dumps(_pair("CAMP_A", "3_es_2403_post.mp4"))]) + "\n",
                 encoding="utf-8")
    parsed = []
    real = _json.loads
    monkeypatch.setattr(cv.json, "loads", lambda s, *a, **k: (parsed.append(s), real(s, *a, **k))[1])
    cv._cache["sig"] = None
    assert cv.load_variant_map(j) == {("CAMP_A", "2403"): "3"}
    assert len(parsed) == 2  # только строки с ad_creative_pair, не 52
    assert cv.load_variant_map(j) == {("CAMP_A", "2403"): "3"}
    assert len(parsed) == 2  # из кэша
    j.write_text(j.read_text(encoding="utf-8") + _json.dumps(_pair("CAMP_B", "5_es_2403_post.mp4")) + "\n",
                 encoding="utf-8")
    assert ("CAMP_B", "2403") in cv.load_variant_map(j)  # журнал изменился — кэш сброшен


def test_ad_name_guard_free_text_creative_id_gets_no_variant():
    ad = AdDraft(creative_id="coffee", creative_filename="1_es_2403_post.mp4", copy_name="Text6")
    assert ad.ad_name() == "coffee - Text6"
    ad = AdDraft(creative_id="2046", creative_filename="1_es_2403_post.mp4", copy_name="Text6")
    assert ad.ad_name() == "2046 - Text6"


def test_template_renders_variant_rows():
    from pathlib import Path
    import jinja2
    html = (Path(__file__).resolve().parents[1] / "src/web/templates/report.html").read_text(encoding="utf-8")
    start = html.index("{% for row in c.creatives %}")
    block = html[start:html.index("</tbody>", start)]
    # ACL-1652: строки берут ячейки из `_metrics.html`, поэтому рисуем окружением
    # самого дашборда (оно знает макросы и `metric_values`), а не голым Jinja.
    from src.web.app import templates
    env = templates.env
    rows = build_creative_breakdown(_frame(ROWS), "Colombia")
    out = env.from_string('{% import "_metrics.html" as mx %}' + block).render(c={"creatives": rows})
    for label in ("1962 · 1", "1962 · 2", "1962 · no variant", "2403"):
        assert label in out
    assert out.count('class="creative-variant"') == 3  # у 2403 вариантов нет — строк нет


def test_the_dashboard_loader_runs_the_backfill(tmp_path, monkeypatch):
    from src.analysis_agent import morning_service
    xlsx = tmp_path / "stats.xlsx"
    pd.DataFrame([{"Country": "Colombia", "Account Alias": "AC1", "Adset Name": "CAMP_A",
                   "Ad Title": "2403 - Pro", "# Leads": 3, "# FTDs": 1, "$ Spend": 10.0,
                   "CM": "x", "Ad Asset": "o", "$ Net": 0.0}]).to_excel(xlsx, index=False)
    monkeypatch.setattr(cv, "load_variant_map", lambda *a, **k: {("CAMP_A", "2403"): "3"})
    assert morning_service._load_enriched_df(str(xlsx))["VariantId"].tolist() == ["3"]
