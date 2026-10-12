"""ACL-1799: Country Workbench — данные (KPI «Вы / Другие», версии креатива) и разметка.

Формулы проверяются на фикстуре с известным ответом. Среднее арифметическое
CPA байеров в фикстуре намеренно отличается от взвешенного, чтобы тест падал на
неправильной формуле, а не проходил случайно.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import country_workbench as cw  # noqa: E402
from src.analysis_agent.analyze_fb_stats import (  # noqa: E402
    creative_identity,
    split_ad_title,
)

COLS = ["Country Final", "Offer", "CM", "Account Alias", "Adset Name", "Device", "Age",
        "Interest", "Ad Title", "Variant", "$ Spend", "# Leads", "# FTDs"]


def _rows(rows):
    return pd.DataFrame(rows, columns=COLS)


def fixture_df():
    r = lambda offer, cm, spend, leads, ftds, **kw: [  # noqa: E731
        "Colombia", offer, cm, kw.get("acc", "A1"), kw.get("adset", "CO_Alpha_FX"),
        kw.get("device", "All"), kw.get("age", "35+"), kw.get("interest", "FX"),
        kw.get("title", "1121 1 - Story14"), kw.get("variant", "LP1"), spend, leads, ftds]
    return _rows([
        r("Alpha", "Eva", 600, 60, 4),
        r("Alpha", "Bob", 400, 50, 2),                      # пир: CPA 200
        r("Alpha", "Carl", 200, 30, 4, title="1121 - 2"),   # пир: CPA 50; среднее 125, взвешенное 100
        r("Beta", "Eva", 50, 10, 1),                        # мало данных
        r("Beta", "Bob", 40, 8, 0),
        r("Alpha", "", 999, 99, 9),                         # без CM: ни «Вы», ни «Другие»
    ])


# ── формулы ───────────────────────────────────────────────────────────────

def test_weighted_kpis_use_the_stated_formulas():
    k = cw.weighted_kpis(600, 60, 4)
    assert k["cpl"] == pytest.approx(10.0)       # Spend / Leads
    assert k["cpa"] == pytest.approx(150.0)      # Spend / FTDs
    assert k["cr"] == pytest.approx(4 / 60 * 100)  # FTDs / Leads, %


def test_a_zero_denominator_is_none_not_zero():
    k = cw.weighted_kpis(100, 0, 0)
    assert k["cpl"] is None and k["cpa"] is None and k["cr"] is None
    assert cw.weighted_kpis(100, 20, 0)["cpa"] is None


def test_peers_exclude_the_selected_buyer_and_are_weighted():
    cube = cw.build_cube(fixture_df(), "Colombia")
    cmp_ = cw.you_vs_others(cube[cube["Asset"] == "Alpha"], "Eva")
    assert cmp_["status"] == "ok"
    assert cmp_["you"]["spend"] == 600 and cmp_["you"]["ftds"] == 4
    # Пиры: Bob + Carl, без Eva и без строк без CM.
    assert cmp_["others"]["spend"] == 600
    assert cmp_["others"]["leads"] == 80
    assert cmp_["others"]["ftds"] == 6
    assert cmp_["others"]["buyers"] == 2
    assert cmp_["metrics"]["cpa"]["others"] == pytest.approx(100.0)   # не 125
    assert cmp_["metrics"]["cpl"]["others"] == pytest.approx(7.5)
    assert cmp_["metrics"]["cr"]["others"] == pytest.approx(7.5)
    # строки без CM видны отдельно и ни к кому не приписаны
    assert cmp_["unattributed"]["spend"] == 999


def test_the_delta_and_verdict_follow_the_metric_direction():
    cube = cw.build_cube(fixture_df(), "Colombia")
    m = cw.you_vs_others(cube[cube["Asset"] == "Alpha"], "Eva")["metrics"]
    assert m["cpa"]["delta_pct"] == pytest.approx(50.0)     # 150 против 100
    assert m["cpa"]["verdict"] == "worse"                   # дороже — хуже
    assert m["cpl"]["verdict"] == "worse"
    assert m["cr"]["verdict"] == "worse"                    # 6.7% против 7.5%: ниже — хуже


def test_an_insufficient_cohort_is_marked_and_not_compared():
    cube = cw.build_cube(fixture_df(), "Colombia")
    cmp_ = cw.you_vs_others(cube[cube["Asset"] == "Beta"], "Eva")
    for name in ("cpl", "cr"):
        assert cmp_["metrics"][name]["you_state"] == "insufficient"
        assert cmp_["metrics"][name]["delta_pct"] is None
        assert cmp_["metrics"][name]["verdict"] is None
    # у пира нет FTD: CPA недоступен, а не «ноль»
    assert cmp_["metrics"]["cpa"]["others_state"] == "unavailable"
    assert cmp_["metrics"]["cpa"]["others"] is None


def test_no_peers_and_no_buyer_are_distinct_states():
    cube = cw.build_cube(fixture_df(), "Colombia")
    alone = cube[(cube["Asset"] == "Alpha") & (cube["CM"] == "Eva")]
    assert cw.you_vs_others(alone, "Eva")["status"] == "no_peers"
    assert cw.you_vs_others(cube, "")["status"] == "no_buyer"
    assert cw.you_vs_others(cube, "Zed")["status"] == "no_you"


def test_the_buyer_name_match_ignores_case_and_spaces():
    cube = cw.build_cube(fixture_df(), "Colombia")
    assert cw.you_vs_others(cube, "  eva ")["you"]["spend"] == 650


# ── версия креатива ───────────────────────────────────────────────────────

def test_creative_versions_of_one_pack_stay_distinct():
    a, b = creative_identity("1121 1"), creative_identity("1121 - 2")
    assert a["pack_id"] == b["pack_id"] == "1121"
    assert (a["version_id"], b["version_id"]) == ("1", "2")
    assert a["key"] != b["key"]
    assert a["ambiguous"] and b["ambiguous"]   # голая цифра может быть и текстом


def test_a_version_with_text_is_exact_and_leading_zero_is_the_same_version():
    a, b = creative_identity("2046 1 - Story14"), creative_identity("2046 01 - Story14")
    assert a["status"] == "exact" and not a["ambiguous"]
    assert a["key"] == b["key"]
    assert creative_identity("2046 2 - Story14")["key"] != a["key"]


def test_unparsed_titles_stay_unresolved_with_the_raw_string():
    i = creative_identity("New Engagement Ad")
    assert i["status"] == "unresolved" and i["pack_id"] == "" and i["raw"] == "New Engagement Ad"
    assert creative_identity("")["key"] == ""


def test_the_old_pack_split_is_unchanged():
    assert split_ad_title("2046 1 - Story14") == ("2046", "Story14")
    assert split_ad_title("2307_2- 6") == ("2307", "6")


def test_the_creatives_tab_lists_versions_under_the_pack_and_they_sum_up():
    df = fixture_df()
    sl = cw.build_country_workbench(df, "Colombia", "Eva")["slices"]["Alpha"]
    packs = {p["pack"]: p for p in sl["creatives"] if p["pack"]}
    assert set(packs) == {"1121"}
    versions = {v["version"]: v for v in packs["1121"]["versions"]}
    assert set(versions) == {"1", "2"}            # 1121 1 и 1121 - 2 не слиплись
    assert sum(v["spend"] for v in versions.values()) == pytest.approx(packs["1121"]["spend"])
    assert versions["2"]["spend"] == 200 and versions["1"]["spend"] == 600 + 400 + 999


# ── срезы страны ──────────────────────────────────────────────────────────

def test_the_workbench_has_an_all_assets_slice_and_one_per_asset():
    wb = cw.build_country_workbench(fixture_df(), "Colombia", "Eva")
    assert [a["name"] for a in wb["assets"]] == ["", "Alpha", "Beta"]
    assert wb["assets"][1]["you_ftds"] == 4
    assert set(wb["slices"]) == {"", "Alpha", "Beta"}
    for tab in ("campaigns", "setups", "landings", "targets", "creatives", "buyers"):
        assert tab in wb["slices"]["Alpha"]


def test_goal_benchmark_and_peer_are_separate_fields():
    ref = cw.build_country_workbench(fixture_df(), "Colombia", "Eva")["slices"]["Alpha"]["reference"]
    assert set(ref) == {"benchmark", "peer"}   # цель (My Goals) приходит со страницы отдельно
    assert ref["peer"]["cpa"] == pytest.approx(100.0)
    assert {"max_cpl", "max_cpa", "min_cr"} <= set(ref["benchmark"])


def test_buyers_tab_marks_you_and_skips_unattributed_rows():
    buyers = cw.build_country_workbench(fixture_df(), "Colombia", "Eva")["slices"]["Alpha"]["buyers"]
    assert {b["cm"] for b in buyers} == {"Eva", "Bob", "Carl"}
    assert [b["cm"] for b in buyers if b["is_you"]] == ["Eva"]


def test_an_empty_country_gives_an_empty_workbench():
    assert cw.build_country_workbench(fixture_df(), "Nowhere", "Eva") == {}


# ── страница ──────────────────────────────────────────────────────────────

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

TPL = ROOT / "src" / "web" / "templates" / "report.html"
sys.path.insert(0, str(ROOT / "tests"))


def render_page(country="Colombia", with_workbench=True):
    """Страница страны с Workbench и со всеми прежними разделами."""
    from test_web_drill import _MOCK_REPORT, _render_html

    wb = cw.build_country_workbench(fixture_df().assign(**{"Country Final": country}), country, "Eva")
    base = _MOCK_REPORT["countries"][0]
    row = {"spend": 10.0, "leads": 5, "deposits": 1, "net": 5.0, "cpl": 2.0, "cpa": 10.0, "roi": 1.5}
    c = {
        **base, "country": country, "open_recs": {"Demo": []}, "close_recs": {},
        "asset_summary": [{"offer": "Alpha", **row}],
        "asset_variants": [{"offer": "Alpha", "variant": "LP1", "spend": 10.0, "leads": 5, "deposits": 1, "net": 5.0}],
        "variants": [{"variant": "LP1", "is_untagged": False, **row}],
        "trend": {"has_date": True, "period_label": "x", "trending_up": [], "stable": [], "trending_down": []},
        "top_combos": [{"offer": "Alpha", "device": "All", "age": "35", "interest": "FX", "creative": "1121",
                        "category": "Strong", **row}],
        "targeting": [{"interest": "FX", **row}],
        "creatives": [{"creative_id": "1121", "text_name": "x", "ad_title": "1121", "variants": [], "has_variants": False, **row}],
    }
    if with_workbench:
        c["workbench"] = wb
    return _render_html({**_MOCK_REPORT, "countries": [c]})


def test_every_existing_analytics_view_is_still_rendered_and_linked():
    html = render_page()
    for view in ("manage", "assets", "landings", "trend", "combos", "targeting", "creatives", "optimization"):
        assert f'id="view-country-colombia-{view}"' in html, view
        assert f'data-view="country-colombia-{view}"' in html, view


def test_the_workbench_view_is_added_with_its_data():
    html = render_page()
    assert 'id="view-country-colombia-workbench"' in html
    assert 'data-view="country-colombia-workbench"' in html
    m = re.search(r'<script type="application/json" id="cw-data-colombia">(.*?)</script>', html, re.S)
    data = json.loads(m.group(1))
    assert [a["name"] for a in data["assets"]] == ["", "Alpha", "Beta"]


def test_a_report_without_workbench_data_renders_the_old_views_only():
    html = render_page(with_workbench=False)
    assert "view-country-colombia-workbench" not in html
    assert 'id="view-country-colombia-manage"' in html


def test_workbench_styles_are_one_block_and_every_selector_is_cw_prefixed():
    page = TPL.read_text(encoding="utf-8")
    blocks = re.findall(r'<style id="cw-styles">(.*?)</style>', page, re.S)
    assert len(blocks) == 1
    css = re.sub(r"/\*.*?\*/", "", blocks[0], flags=re.S)
    css = re.sub(r"@media[^{]*\{", "", css)          # заголовок медиа-запроса
    for selectors in re.findall(r"([^{}]+)\{", css):
        for sel in selectors.split(","):
            sel = sel.strip()
            if sel:
                assert sel.startswith(".cw-"), f"селектор без префикса .cw-: {sel!r}"
    # только токены страницы: никаких литеральных цветов
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css), "литеральный цвет в стилях Workbench"


def test_the_ui_text_of_the_workbench_is_english():
    page = TPL.read_text(encoding="utf-8")
    script = page[page.index('<script id="cw-script">'):]
    script = script[: script.index("</script>")]
    # комментарии на русском, но строки интерфейса — нет
    literals = re.findall(r"'([^'\n]{4,})'", re.sub(r"/\*.*?\*/", "", script, flags=re.S))
    assert not [s for s in literals if re.search(r"[А-Яа-яЁё]", s)]


def test_the_pure_page_functions_behave():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node")
    r = subprocess.run([node, str(ROOT / "tests" / "cw_harness.cjs"), str(TPL)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
