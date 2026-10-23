"""ACL-1827: Home KPI «Вы / Другие», боковая колонка и панель Robot health — данные и разметка.

Числа проверяются на фикстуре с известным ответом (две страны, один байер в
обеих). Среднее по байерам здесь снова отличается от взвешенного, поэтому тест
падает на неправильной формуле. Браузерная часть — в test_acl1827_shell_browser.py.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from src.analysis_agent import country_workbench as cw  # noqa: E402
from test_acl1799_country_workbench import COLS, fixture_df  # noqa: E402

TPL = ROOT / "src" / "web" / "templates" / "report.html"
FIX = ROOT / "tests" / "fixtures" / "robot_health"


def chile_df() -> pd.DataFrame:
    r = lambda cm, spend, leads, ftds: [  # noqa: E731
        "Chile", "Alpha", cm, "A9", "CL_Alpha_FX", "All", "35+", "FX", "1121 1 - Story14", "LP1", spend, leads, ftds]
    return pd.DataFrame([r("Eva", 100, 20, 1), r("Dan", 300, 50, 3)], columns=COLS)


def countries(my_cm="Eva"):
    out = []
    for df, name in ((fixture_df(), "Colombia"), (chile_df(), "Chile")):
        out.append({"country": name, "workbench": cw.build_country_workbench(df, name, my_cm)})
    return out


# ── формулы Home ─────────────────────────────────────────────────────────

def test_home_totals_are_weighted_sums_over_all_countries():
    cmp_ = cw.home_you_vs_others(countries(), "Eva")
    # Eva: Colombia 650/70/5 + Chile 100/20/1
    assert (cmp_["you"]["spend"], cmp_["you"]["leads"], cmp_["you"]["ftds"]) == (750, 90, 6)
    # Другие: Colombia (Bob 440/58/2 + Carl 200/30/4) + Chile (Dan 300/50/3); строки без CM не в счёт
    assert (cmp_["others"]["spend"], cmp_["others"]["leads"], cmp_["others"]["ftds"]) == (940, 138, 9)
    assert cmp_["unattributed"]["spend"] == 999
    m = cmp_["metrics"]
    assert m["cpa"]["you"] == pytest.approx(750 / 6)
    assert m["cpa"]["others"] == pytest.approx(940 / 9)      # взвешенное, а не среднее по байерам
    assert m["cpl"]["others"] == pytest.approx(940 / 138)
    assert m["cr"]["you"] == pytest.approx(6 / 90 * 100)
    assert cmp_["status"] == "ok" and cmp_["countries_used"] == 2


def test_home_equals_the_country_formulas_for_one_country():
    one = cw.home_you_vs_others(countries()[:1], "Eva")
    sl = countries()[0]["workbench"]["slices"][""]["compare"]
    for name in ("cpl", "cpa", "cr"):
        assert one["metrics"][name]["you"] == pytest.approx(sl["metrics"][name]["you"])
        assert one["metrics"][name]["others"] == pytest.approx(sl["metrics"][name]["others"])
        assert one["metrics"][name]["delta_pct"] == pytest.approx(sl["metrics"][name]["delta_pct"])


def test_the_selected_buyer_is_never_in_others():
    cmp_ = cw.home_you_vs_others(countries("Dan"), "Dan")
    assert cmp_["you"]["spend"] == 300
    # Dan есть только в Chile; «Другие» — Eva, Bob, Carl в Colombia (650+440+200) и Eva в Chile (100)
    assert cmp_["others"]["spend"] == 650 + 440 + 200 + 100


def test_no_buyer_no_peers_and_no_you_are_distinct_states():
    assert cw.home_you_vs_others(countries(""), "")["status"] == "no_buyer"
    assert cw.home_you_vs_others(countries("Zed"), "Zed")["status"] == "no_you"
    only_me = [{"country": "Chile", "workbench": cw.build_country_workbench(chile_df().iloc[:1], "Chile", "Eva")}]
    assert cw.home_you_vs_others(only_me, "Eva")["status"] == "no_peers"


def test_countries_without_a_workbench_are_counted_out_not_zeroed():
    mixed = countries() + [{"country": "Peru", "workbench": {}}]
    cmp_ = cw.home_you_vs_others(mixed, "Eva")
    assert cmp_["countries_used"] == 2 and cmp_["countries_total"] == 3
    assert cmp_["you"]["spend"] == 750
    assert cw.home_you_vs_others([{"country": "Peru", "workbench": {}}], "Eva")["countries_used"] == 0


def tiles(my_cm="Eva", cs=None):
    return {t["key"]: t for t in cw.home_kpi_tiles(cw.home_you_vs_others(cs or countries(my_cm), my_cm))}


def test_there_are_six_tiles_in_the_prototype_order():
    got = cw.home_kpi_tiles(cw.home_you_vs_others(countries(), "Eva"))
    assert [t["label"] for t in got] == ["FTDs", "CPA", "Leads", "CPL", "Spend", "Lead → FTD"]


def test_tile_values_come_from_the_real_sums():
    t = tiles()
    assert (t["ftds"]["you"], t["ftds"]["others"]) == ("6", "9")
    assert (t["leads"]["you"], t["leads"]["others"]) == ("90", "138")
    assert (t["spend"]["you"], t["spend"]["others"]) == ("$750", "$940")
    assert (t["cpa"]["you"], t["cpa"]["others"]) == ("$125", "$105")       # 104.4 округляется вверх, как в Workbench
    assert t["cr"]["you"] == "6.7%" and t["cr"]["others"] == "6.5%"


def test_delta_has_a_verdict_only_when_both_cohorts_are_sufficient():
    t = tiles()
    assert t["cpa"]["caption"].endswith("worse") and t["cpa"]["cls"] == "hk-bad"
    assert t["cpl"]["caption"].startswith("+22.3% vs others")
    # объёмные плитки не сравниваются и не красятся
    for key in ("ftds", "leads", "spend"):
        assert t[key]["cls"] == "" and "better" not in t[key]["caption"] and "worse" not in t[key]["caption"]


def test_an_insufficient_cohort_is_labelled_and_not_compared():
    tiny = [{"country": "Colombia", "workbench": cw.build_country_workbench(
        fixture_df()[fixture_df()["Offer"] == "Beta"], "Colombia", "Eva")}]
    t = tiles(cs=tiny)
    assert "insufficient (<30 leads)" in t["cpl"]["you_note"]
    assert "insufficient (<30 leads)" in t["cr"]["you_note"]
    assert t["cpl"]["caption"].startswith("Not compared")
    # у пира нет FTD — «—», а не $0
    assert t["cpa"]["others"].startswith("—")


def test_without_a_buyer_the_tiles_say_so_instead_of_comparing():
    t = tiles("")
    assert t["cpa"]["caption"] == "Pick a buyer in the header to compare"
    assert t["ftds"]["you"] == "0"      # «вас» нет — это ноль объёма, но сравнения нет


def test_the_app_builds_no_tiles_without_workbench_data():
    from src.web.app import _home_kpis

    assert _home_kpis({"countries": [{"country": "Peru", "workbench": {}}], "my_cm": "Eva"}) == []
    assert len(_home_kpis({"countries": countries(), "my_cm": "Eva"})) == 6


def test_the_sidebar_counter_is_you_over_all_including_unattributed_rows():
    assert cw.country_ftd_counts(countries()[0]["workbench"]) == {"you": 5, "all": 20}
    assert cw.country_ftd_counts({}) is None


# ── разметка ─────────────────────────────────────────────────────────────

def render(my_cm="Eva", with_home=True, cs=None):
    from src.web.app import _enrich_country_overview, _home_kpis, _sidebar_countries, mlx_instance_labels, templates
    from test_web_drill import _MOCK_REPORT

    base = _MOCK_REPORT["countries"][0]
    cs = cs or countries(my_cm)
    full = [{**base, **c, "open_recs": {"Demo": []}, "close_recs": {}} for c in cs]
    report = {**_MOCK_REPORT, "my_cm": my_cm, "countries": full}
    _enrich_country_overview(report)
    return templates.env.get_template("report.html").render(
        mlx_labels=mlx_instance_labels(), request=MagicMock(), report=report, warnings=[],
        built_at="2026-06-08", upload_errors={}, account_preflight={}, account_preflight_json={},
        ref_options={}, ref_options_json={}, account_alias_map_json={}, draft_status={},
        sidebar_countries=_sidebar_countries(report, {}),
        home_kpis=_home_kpis(report) if with_home else [],
        period={"date_from": "2026-06-01", "date_to": "2026-06-08", "span": ""})


def block(side: str, name: str) -> str:
    """Блок одной страны в колонке — порядок стран зависит от расхода, поэтому по имени."""
    parts = side.split('<div class="sidebar-country"')[1:]
    return next(p for p in parts if f'data-name="{name}"' in p[:120])


def sidebar(html: str) -> str:
    return html[html.index('<aside class="sidebar"'): html.index("</aside>")]


def test_the_kpi_row_sits_above_the_account_map_on_home():
    html = render()
    home = html[html.index('id="view-home"'):]
    assert home.index('id="home-kpis"') < home.index('id="amap-slot"')
    row = home[home.index('id="home-kpis"'): home.index('id="amap-slot"')]
    assert row.count('class="hk-tile"') == 6
    assert re.findall(r'data-kpi="(\w+)"', row) == ["ftds", "cpa", "leads", "cpl", "spend", "cr"]
    assert "You vs Others" in row and "$750" in row


def test_no_row_without_data_so_no_fake_zeros():
    assert 'id="home-kpis"' not in render(with_home=False)


def test_the_sidebar_has_no_emoji_and_the_new_label():
    side = sidebar(render())
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", side), "эмодзи в боковой колонке"
    assert "Target library" in side
    assert re.search(r'class="sidebar-link" data-view="targets"', side)      # адрес и порядок прежние
    assert ">Targeting<" not in side.replace("Targeting</button>", "")       # у стран пункт «Targeting» остаётся
    page = render()
    assert 'id="view-targets"' in page


def test_every_old_sidebar_link_is_still_there_and_in_the_same_order():
    html = render()
    side = sidebar(html)
    main = re.findall(r'class="sidebar-link[^"]*" data-view="([a-z0-9-]+)"', side)
    assert main[:4] == ["home", "targets", "ad-texts", "accounts"]
    for view in ("workbench", "manage", "assets", "landings", "trend", "combos", "targeting", "creatives", "optimization"):
        # у фикстуры может не быть части разделов; важно, что ни один ранее существовавший не пропал
        had = f'data-view="country-colombia-{view}"' in html
        assert had == (f'data-view="country-colombia-{view}"' in side)
    assert 'data-view="country-colombia-workbench"' in side and 'data-view="country-colombia-manage"' in side


def test_countries_carry_you_over_all_ftd_counters_next_to_the_button():
    side = sidebar(render())
    assert re.search(r'class="sb-ftd sb-ftd-country"[^>]*>5 / 20<', block(side, "colombia"))
    assert re.search(r'class="sb-ftd sb-ftd-country"[^>]*>1 / 4<', block(side, "chile"))
    # имя страны для билдера читается из текста кнопки: счётчик не должен в неё попасть
    names = re.findall(r'<button type="button" class="sidebar-country-btn[^>]*>([^<]*)</button>', side)
    assert sorted(names) == ["Chile", "Colombia"]


def test_asset_rows_sit_under_their_country_with_you_over_all_counts():
    side = sidebar(render())
    colombia = block(side, "colombia")
    assets = re.findall(r'data-asset="([^"]*)"', colombia)
    assert assets == ["", "Alpha", "Beta"]
    assert re.search(r'data-asset="Alpha"[^>]*>\s*<span class="sb-asset-name">Alpha</span><span class="sb-ftd">4 / 19<', colombia)
    assert 'aria-label="Assets in Colombia"' in colombia
    # ACL-2119: «All assets», затем виды страны (не свёрнуты), затем ассеты по брендам
    assert "sb-more" not in colombia and "More country views" not in colombia
    all_at = colombia.index('aria-label="All assets in Colombia"')
    views_at = colombia.index('class="sb-views"')
    brands_at = colombia.index('class="sb-assets sb-assets-brands"')
    assert all_at < views_at < brands_at
    assert views_at < colombia.index('data-view="country-colombia-workbench"') < brands_at
    assert colombia.index('data-view="country-colombia-optimization"') < brands_at


def test_find_country_search_is_there_and_names_are_searchable():
    side = sidebar(render())
    assert 'id="sb-find-country"' in side and 'aria-label="Find country"' in side
    assert 'data-name="colombia"' in side and 'data-name="chile"' in side


def test_the_robot_health_link_stays_a_real_link_and_a_popover_is_added():
    html = render()
    assert re.search(r'<a href="/robot-health" id="robot-health-link"[^>]*aria-haspopup="dialog"', html)
    pop = html[html.index('id="rh-pop"'):]
    assert 'role="dialog"' in html[html.index('id="rh-pop"') - 60: html.index('id="rh-pop"') + 120]
    assert 'href="/robot-health"' in pop[:600] and 'id="rh-close"' in pop[:700]


def test_new_styles_use_tokens_only_and_new_ui_text_is_english():
    page = TPL.read_text(encoding="utf-8")
    css = re.search(r'<style id="shell-1827">(.*?)</style>', page, re.S).group(1)
    code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", code), "литеральный цвет"
    assert not re.search(r"font-size:\s*[\d.]+(rem|px)", code), "размер шрифта не из токена"
    assert not re.search(r"(?<![\w-])(rgb|hsl)a?\(", code.replace("color-mix", "")), "цвет не из токена"
    js = re.search(r'<script id="shell-1827-script">(.*?)</script>', page, re.S).group(1)
    literals = re.findall(r"'([^'\n]{4,})'", re.sub(r"/\*.*?\*/", "", js, flags=re.S))
    assert not [s for s in literals if re.search(r"[А-Яа-яЁё]", s)]


# ── Robot health: данные панели ──────────────────────────────────────────

@pytest.fixture()
def rh_out(tmp_path, monkeypatch):
    from src.web import app as web

    out = tmp_path / "output"
    out.mkdir()
    shutil.copy(FIX / "rpa_telemetry.jsonl", out / "rpa_telemetry.jsonl")
    shutil.copy(FIX / "serve-live.log", out / "serve-live.log")
    monkeypatch.setattr(web, "_robot_health_output_dir", lambda: out)
    monkeypatch.setattr(web, "_robot_health_current_build", lambda: None)
    monkeypatch.setattr(web, "_robot_health_logs_dir", lambda: tmp_path / "no-launcher-logs")
    return web, out


def test_the_summary_lists_each_account_with_its_latest_run(rh_out):
    import json

    web, _ = rh_out
    resp = web.robot_health_summary(n=5)
    data = json.loads(resp.body)
    assert data["error"] == "" and data["n_runs"] > 0 and data["accounts"]
    names = [a["account"] for a in data["accounts"]]
    assert len(names) == len(set(names))
    for a in data["accounts"]:
        assert 0 <= a["clean"] <= a["runs"] <= 5
    # сначала те, у кого последний прогон упал
    flags = [a["last_ok"] for a in data["accounts"]]
    assert flags == sorted(flags)


def test_the_summary_is_honest_when_there_is_no_telemetry(tmp_path, monkeypatch):
    import json

    from src.web import app as web

    monkeypatch.setattr(web, "_robot_health_output_dir", lambda: tmp_path)
    monkeypatch.setattr(web, "_robot_health_current_build", lambda: None)
    monkeypatch.setattr(web, "_robot_health_logs_dir", lambda: tmp_path / "none")
    data = json.loads(web.robot_health_summary(n=5).body)
    assert data["accounts"] == [] and "No robot runs" in data["error"]


def test_the_full_page_still_answers_at_its_old_address(rh_out):
    web, _ = rh_out
    assert web.robot_health_page(n=5).status_code == 200
