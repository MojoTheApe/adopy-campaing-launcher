"""ACL-1652: одни и те же шесть метрик в каждой таблице атрибуции.

Spend · Leads · FTDs · CPL · CPA · CR, в таком порядке, с такими формулами и
таким видом чисел. Таблица аккаунтов на Home и в Manage добавляет Account spend
рядом со Spend. Строка Totals (ACL-1654) и секция Campaign managers (ACL-1656)
стоят на той же сетке.

Тест держит три вещи, чтобы таблицы не разошлись снова:
  * формулы и вид чисел в `metric_columns` (нулевой делитель — прочерк);
  * JS-двойник `acl6Metrics` даёт те же строки, что Python;
  * заголовки каждой таблицы равны целевому списку.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.analysis_agent import metric_columns as mc

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "src" / "web" / "templates"
REPORT = (TPL / "report.html").read_text(encoding="utf-8")

TARGET = ["Spend", "Leads", "FTDs", "CPL", "CPA", "CR"]


# ── Формулы ────────────────────────────────────────────────────────────────

def test_the_target_lists_are_the_owners_decision():
    assert list(mc.METRIC_HEADERS) == TARGET
    assert list(mc.HOME_ACCOUNT_HEADERS) == ["Spend", "Leads", "FTDs", "CPL", "CPA", "CR"]


def test_the_three_formulas():
    r = mc.ratios(spend=400, leads=40, ftds=8)
    assert r["cpl"] == 10 and r["cpa"] == 50 and r["cr"] == 20.0


@pytest.mark.parametrize("spend,leads,ftds,cpl,cpa,cr", [
    (100, 0, 0, None, None, None),      # ни лидов, ни FTD: везде прочерк
    (100, 10, 0, 10, None, 0.0),        # FTD ноль: CPA — прочерк, CR — 0.0%
    (100, 0, 3, None, 33.333333, None),  # лидов нет: CPL и CR — прочерк
    (0, 10, 2, 0, 0, 20.0),             # спенда нет — это $0, а не прочерк
])
def test_zero_divisors(spend, leads, ftds, cpl, cpa, cr):
    got = mc.ratios(spend, leads, ftds)
    assert got["cpl"] == cpl
    assert got["cpa"] == pytest.approx(cpa) if cpa is not None else got["cpa"] is None
    assert got["cr"] == cr


def test_nan_and_none_are_not_a_number_not_zero():
    nan = float("nan")
    assert mc.ratios(nan, nan, nan) == {"cpl": None, "cpa": None, "cr": None}
    assert mc.ratios(None, None, None) == {"cpl": None, "cpa": None, "cr": None}


def test_values_are_whole_dollars_rounded_up_and_cr_has_one_decimal():
    # $7.10 -> $8: так округляет `fmt_usd` по всему дашборду (решение 06.10)
    assert mc.metric_values(71, 10, 1) == ["$71", "10", "1", "$8", "$71", "10.0%"]
    assert mc.metric_values(1044.2, 138, 11) == ["$1,045", "138", "11", "$8", "$95", "8.0%"]


def test_a_zero_is_a_dash_only_when_the_divisor_is_zero():
    assert mc.metric_values(100, 10, 0) == ["$100", "10", "0", "$10", "—", "0.0%"]
    assert mc.metric_values(0, 0, 0) == ["$0", "0", "0", "—", "—", "—"]


# ── JS-двойник ─────────────────────────────────────────────────────────────

def _js_twin() -> str:
    start = REPORT.index("var ACL_METRIC_HEADERS")
    end = REPORT.index("function assetVariantToggle", start)
    return REPORT[start:end]


CASES = [
    (400, 40, 8), (100, 0, 0), (100, 10, 0), (100, 0, 3), (0, 10, 2), (0, 0, 0),
    (71, 10, 1), (1044.2, 138, 11), (7.0000001, 1, 1), (123456.78, 987, 12),
    (None, None, None), (50, None, 5), (10.5, 3, 0),
    # CR на точных «половинках»: 6.25, 18.75, 31.25, 3.125 — обе стороны округляют вверх
    (10, 16, 1), (10, 16, 3), (10, 16, 5), (10, 32, 1),
    (-0.4, 5, 1), (-0.4, 0, 0),  # отрицательный спенд между -1 и 0 — «$0», не «$-0»
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the JS twin")
def test_the_js_twin_gives_the_python_strings(tmp_path):
    script = _js_twin() + "\nconsole.log(JSON.stringify({h: ACL_METRIC_HEADERS, r: %s.map(c => acl6Metrics(c[0], c[1], c[2]))}));" % json.dumps(CASES)
    # файлом и в UTF-8: через `node -e` кириллица и «—» портятся в кодовой странице Windows
    js_file = tmp_path / "twin.js"
    js_file.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(js_file)], capture_output=True, timeout=60, check=True)
    got = json.loads(out.stdout.decode("utf-8"))
    assert got["h"] == TARGET, "JS-заголовки разошлись с Python"
    for case, js in zip(CASES, got["r"]):
        assert js == mc.metric_values(*case), f"JS и Python разошлись на {case}"


# ── Заголовки каждой таблицы ───────────────────────────────────────────────

def _env():
    from src.web.app import templates

    return templates.env


def _th(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", t).strip() for t in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)]


def test_the_macro_renders_exactly_the_target_headers():
    tpl = _env().from_string('{% import "_metrics.html" as mx %}<table><tr>{{ mx.headers() }}</tr></table>')
    assert _th(tpl.render()) == TARGET


def test_the_macro_cells_follow_the_helper():
    tpl = _env().from_string('{% import "_metrics.html" as mx %}<table><tr>{{ mx.cells(100, 10, 0) }}</tr></table>')
    cells = re.findall(r"<td[^>]*>(.*?)</td>", tpl.render())
    assert cells == mc.metric_values(100, 10, 0)


def _tables(src: str) -> list[str]:
    return re.findall(r"<table.*?</table>", src, re.S)


#: каждая таблица атрибуции страны: (начало раздела в report.html, первая колонка,
#: что стоит после шести метрик)
COUNTRY_TABLES = {
    "CM Comparison": (["CM"], ["Net", "ROI", ""]),
    "Assets": (["Ad Asset"], ["Player Value"]),
    "Landings": (["Landing"], ["Net"]),
    "Trend": (["Offer", "Device", "Interest", "Creative"], []),
    "Top Combos": (["Combo"], []),
    "Targeting": (["Target"], []),
    "Creatives": (["Creative", "Text"], []),
    "Optimization": (["Ad set name"], ["Reason"]),
}


def _section(title: str) -> str:
    """Кусок report.html от заголовка раздела до следующего `view-title` страны."""
    heading = {
        "Assets": "{{ n_assets }} Assets", "Landings": "{{ n_landings }} Landings",
        "Trend": "{{ n_trend }} Recent Trend", "Top Combos": "{{ n_combos }} Top Combos",
        "Targeting": "{{ n_targeting }} Targeting", "Creatives": "{{ n_creatives }} Creative breakdown",
        "Optimization": "{{ n_optimization }} Optimization",
    }.get(title, title)
    i = REPORT.index('<h2 class="view-title">{{ c.country }}: ' + heading)
    j = REPORT.find('<h2 class="view-title">', i + len(title))
    return REPORT[i: j if j != -1 else len(REPORT)]


@pytest.mark.parametrize("title", list(COUNTRY_TABLES))
def test_every_country_table_head_is_name_then_the_six_then_extras(title):
    section = _section(title)
    head = section[section.index("<thead>"): section.index("</thead>")]
    first, extras = COUNTRY_TABLES[title]
    # шесть метрик идут из макроса, а не переписаны руками
    assert head.count("mx.headers()") == 1, f"{title}: заголовки метрик не из _metrics.html"
    pieces = head.split("{{ mx.headers() }}")
    assert _th(pieces[0]) == first, f"{title}: колонки до метрик"
    assert _th(pieces[1]) == extras, f"{title}: колонки после метрик"


def test_no_table_still_spells_old_metric_names_by_hand():
    for label in ("Deposits", "Dep<", "FTD<", "Total dep", "Recent dep"):
        for name in ("report.html", "_details_fragment.html", "_account_map.html", "_account_map_country.html"):
            src = (TPL / name).read_text(encoding="utf-8")
            heads = " ".join(re.findall(r"<thead>.*?</thead>", src, re.S))
            assert label not in heads, f"{name}: заголовок «{label}» остался"
    # ни один шаблон не рисует CPL/CPA/CR из готового ключа мимо общего кода
    for name in ("report.html", "_details_fragment.html"):
        src = (TPL / name).read_text(encoding="utf-8")
        assert not re.search(r"\b(?:row|r|v)\.(?:cpa|cpl|cr)\s*\|", src), (
            f"{name}: CPL/CPA/CR берутся из ключа, а не из metric_values"
        )


def test_the_trend_shows_trending_down_too():
    assert "('Trending down', 'trending_down')" in REPORT


def test_the_home_account_table_head_is_the_home_list():
    src = (TPL / "_account_map_country.html").read_text(encoding="utf-8")
    head = src[src.index("<thead>"): src.index("</thead>")]
    cols = _th(head)
    metrics = [c for c in cols if c in mc.HOME_ACCOUNT_HEADERS]
    assert metrics == list(mc.HOME_ACCOUNT_HEADERS)
    assert cols[0] == "Account" and cols.index("Spend") == 1 and cols.index("Campaign list") == 2


def _render_fragment(section: str, rows: list[dict]) -> str:
    return _env().get_template("_details_fragment.html").render(
        rows=rows, section=section, country="Colombia", ncols=8, message="",
    )


ROW = {"account": "A1", "adset": "AS1", "ad_title": "T", "ad_asset": "X", "spend": 100.0, "leads": 10,
       "ftds": 0, "cpl": 10.0, "cpa": None, "cm": "me", "date": "2026-10-05", "variants": []}


def test_the_drill_tables_end_in_the_six_metrics():
    # ads under an ad set
    h = _th(_render_fragment("optimization", [ROW]))
    assert h == ["Ad title", "Asset"] + TARGET, "в раскрытии объявлений нет ни Date, ни кнопки «+»"
    # ad sets under a CM or an account
    h = _th(_render_fragment("account", [ROW]))
    assert h[:2] == ["Campaign / Ad set", "Account"] and h[2:8] == TARGET
    # everything else
    h = _th(_render_fragment("trend", [ROW]))
    assert h == ["Account", "CM", "Adset", "Ad title", "Asset"] + TARGET


def test_a_zero_divisor_in_a_drill_row_is_a_dash_not_dollar_zero():
    html = _render_fragment("optimization", [ROW])
    cells = re.findall(r'<td class="num[^"]*">(.*?)</td>', html)
    assert cells[:6] == ["$100", "10", "0", "$10", "—", "0.0%"]


def test_the_drill_scrolls_inside_when_the_builder_is_open():
    assert 'class="drill-scroll"' in _render_fragment("optimization", [ROW])
    rule = REPORT[REPORT.index(".drill-scroll {"):]
    assert "overflow-x: auto" in rule[: rule.index("}")]


def test_the_nested_drill_uses_the_parent_column_count():
    """cm/account drill has name, account and six metrics = 8 columns."""
    src = (TPL / "_details_fragment.html").read_text(encoding="utf-8")
    assert "&ncols=8" in src and "&ncols=6" not in src


# ── Home: страны, Totals, Campaign managers ───────────────────────────────

def _home(countries, live=None, cm=None, source_info=None):
    from src.web.app import _amap_cm_section, _amap_totals

    amap = {
        "countries": countries, "all_accounts": [], "available": [], "available_inactive": [],
        "available_hidden": [], "dates": [], "live": live or {}, "splits": {}, "notes": {},
        "fallback": {}, "span": "all", "date_from": "", "date_to": "", "activity_window": None,
        "source_info": source_info,
    }
    amap["totals"] = _amap_totals(countries, live or {})
    amap["cm_section"] = _amap_cm_section({"countries": cm or []})
    return _env().get_template("_account_map.html").render(amap=amap, mlx={}, request=None)


def _acc(name, spend, leads, ftd):
    return {"account": name, "spend": spend, "leads": leads, "deposits": ftd, "cpl": None, "cpa": None, "cr": None,
            "has_data": True, "workbook_alias": "", "elsewhere": {}, "status": {}, "to_do": "", "ready": False,
            "in_workbook": True, "assets": [], "countries": []}


def _country(name, spend, leads, ftd, rows=None):
    return {"country": name, "key": name.lower(), "virtual": False, "rows": rows if rows is not None else [_acc("A_" + name, spend, leads, ftd)],
            "book": {"spend": spend, "leads": leads, "deposits": ftd}, "live_sums": [], "live_read": 0}


def test_home_country_grid_head_is_the_six_metrics():
    html = _home([_country("Colombia", 100, 10, 2)])
    head = html[html.index('class="amap-grid-head"'): html.index("</div>", html.index('class="amap-grid-head"'))]
    spans = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<span[^>]*>(.*?)</span>", head, re.S)]
    assert spans == ["", "Country", ""] + TARGET + ["Accounts", ""]


def test_home_totals_row_sums_and_computes_ratios_from_the_sums():
    html = _home([_country("Colombia", 100, 10, 2), _country("Peru", 300, 30, 0)])
    row = html[html.index('id="amap-totals"'):]
    row = row[: row.index("</div>")]
    nums = re.findall(r'<span class="num"><b>(.*?)</b></span>', row)
    # 400 / 40 / 2 -> CPL 10, CPA 200, CR 5.0 (не среднее из 5.0 и 0.0 по странам)
    assert nums[:6] == ["$400", "40", "2", "$10", "$200", "5.0%"]
    assert nums[6] == "2"  # аккаунтов
    assert html.index('id="amap-totals"') < html.index("amap-available"), "Totals должна стоять перед Available accounts"


def test_totals_account_read_is_summed_only_in_one_currency():
    from src.web.app import _amap_totals

    countries = [_country("Colombia", 1, 1, 1, rows=[_acc("A", 1, 1, 1), _acc("B", 1, 1, 1)]),
                 _country("Peru", 1, 1, 1, rows=[_acc("A", 1, 1, 1)])]  # A привязан к двум странам
    one = {"A": {"state": "ok", "spend": 10, "currency": "USD"}, "B": {"state": "ok", "spend": 5, "currency": "USD"}}
    assert _amap_totals(countries, one)["read"] == {"currency": "USD", "spend": 15.0}, "аккаунт посчитан дважды"
    mixed = {**one, "B": {"state": "ok", "spend": 5, "currency": "KWD"}}
    assert _amap_totals(countries, mixed)["read"] is None
    sign = {**one, "B": {"state": "ok", "spend": 5, "currency": "$"}}
    assert _amap_totals(countries, sign)["read"] is None, "знак без кода не складывается"
    assert _amap_totals(countries, {})["read"] is None


def test_campaign_managers_section_is_collapsed_and_sums_every_manager():
    cm = [{"country": "Colombia", "cm_comparison": [
        {"cm": "Me", "is_me": True, "spend": 100, "leads": 10, "deposits": 2},
        {"cm": "Dima", "is_me": False, "spend": 300, "leads": 20, "deposits": 0}]}]
    html = _home([_country("Colombia", 100, 10, 2)], cm=cm)
    tag = re.search(r'<details class="amap-cm"[^>]*>', html).group(0)
    assert " open" not in tag, "секция раскрыта разметкой"
    country_tag = re.search(r'<details class="amap-cm-country"[^>]*>', html).group(0)
    assert " open" not in country_tag
    section = html[html.index('id="amap-cm"'):]
    country_row = re.search(r'<summary class="amap-cm-row amap-row">(.*?)</summary>', section, re.S).group(1)
    nums = re.findall(r'<span class="num"><b>(.*?)</b></span>', country_row)
    assert nums == ["$400", "30", "2", "$14", "$200", "6.7%"], "строка страны — сумма всех менеджеров"
    assert section.index("Me (you)") < section.index("Dima"), "текущий менеджер первым"
    assert 'amap-cm-person me' in section
    # на той же сетке, что и страны
    assert re.search(r"\.amap-cm-row[^{]*\{[^}]*grid-template-columns", REPORT) or ".amap-cm-row," in REPORT


def test_the_open_state_is_remembered_and_survives_an_empty_storage():
    assert "details.amap-cm[data-country]" in REPORT
    assert "details.amap-cm-country[data-country]" in REPORT
    i = REPORT.index("function amapOpenList()")
    assert "try {" in REPORT[i: i + 200], "чтение localStorage без try/catch"


def test_the_workbook_mix_up_is_gone_asset_drill_matches_the_server():
    """Раньше JS округлял к ближайшему, сервер — вверх; теперь один код."""
    i = REPORT.index("function assetVariantToggle")
    body = REPORT[i: REPORT.index("// §6 Creative Brief", i)]
    assert "Math.round" not in body and "acl6Metrics" in body
    assert 'colspan="8"' in body, "Assets: имя, шесть метрик, Player Value"


def test_the_tree_rows_use_the_same_labels_in_the_same_order():
    """Строки дерева под аккаунтом: поля и подписи идут в порядке шести метрик."""
    cols = re.search(r"var AMAP_ATTR_COLS = \[(.*?)\];", REPORT).group(1)
    fields = re.findall(r"'(\w+)'", cols)
    labels = dict(re.findall(r"(\w+): '(\w+)'", re.search(r"var AMAP_ATTR_LABELS = \{(.*?)\};", REPORT, re.S).group(1)))
    assert [labels[f] for f in fields] == TARGET


def test_the_drills_have_no_date_and_no_cart_button():
    """Решение владельца 07.10: Date и «+ 🛒» из раскрытия объявлений убраны.

    Обработчика кнопки на странице давно нет (корзину убрали раньше), так что
    она была мёртвой.
    """
    for section in ("optimization", "account", "trend"):
        html = _render_fragment(section, [ROW])
        assert "cb-cart-btn" not in html and "2026-10-05" not in html
    assert "td.date" not in REPORT


def test_cr_rounds_half_up_on_both_sides_at_ties():
    # 1/16 = 6.25% -> 6.3, 3/16 = 18.75% -> 18.8, 5/16 = 31.25% -> 31.3, 1/32 = 3.125% -> 3.1
    assert [mc.metric_values(1, l, f)[5] for l, f in ((16, 1), (16, 3), (16, 5), (32, 1))] == [
        "6.3%", "18.8%", "31.3%", "3.1%"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the JS twin")
def test_cr_agrees_between_python_and_js_for_every_lead_count_up_to_400(tmp_path):
    script = _js_twin() + """
const out = [];
for (let l = 1; l <= 400; l++) for (let f = 0; f <= l; f++) out.push(acl6Metrics(1, l, f)[5]);
console.log(JSON.stringify(out));
"""
    js_file = tmp_path / "sweep.js"
    js_file.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(js_file)], capture_output=True, timeout=120, check=True)
    js = json.loads(out.stdout.decode("utf-8"))
    py = [mc.metric_values(1, l, f)[5] for l in range(1, 401) for f in range(0, l + 1)]
    assert len(js) == len(py) == 80_600
    bad = [(i, a, b) for i, (a, b) in enumerate(zip(js, py)) if a != b]
    assert not bad, f"{len(bad)} расхождений, первое: {bad[0]}"


def test_totals_account_read_says_how_many_accounts_were_read():
    import pandas as pd
    from src.analysis_agent import bi_compare
    from src.web.app import _amap_totals

    countries = [_country("A", 1, 1, 1, rows=[_acc("X", 1, 1, 1), _acc("Y", 1, 1, 1), _acc("Z", 1, 1, 1)])]
    live = {"X": {"state": "ok", "spend": 1, "currency": "USD"}, "Y": {"state": "ok", "spend": 2, "currency": "KWD"}}
    t = _amap_totals(countries, live)
    assert (t["read_count"], t["accounts"], t["mixed"], t["read"]) == (2, 3, True, None)
    for sources, description in [(None, "the selected statistics"),
            (["workbook"], "the Stats workbook"), (["bi"], "the BI data"),
            (["bi", "workbook"], "the selected source data")]:
        info = bi_compare.provenance(frame=pd.DataFrame({"_stats_source": sources})) if sources else None
        html = _home(countries, live=live, source_info=info)
        assert 'mixed currencies' not in html and 'accounts read' not in html
        assert f'numbers from {description}' in html


def test_the_campaign_managers_header_names_both_levels_and_the_section_is_last():
    cm = [{"country": "Colombia", "cm_comparison": [{"cm": "Me", "is_me": True, "spend": 1, "leads": 1, "deposits": 1}]}]
    html = _home([_country("Colombia", 100, 10, 2)], cm=cm)
    section = html[html.index('id="amap-cm"'):]
    assert "<span>Country / manager</span>" in section
    # последняя вещь на Home: после неё нет ни Available accounts, ни Totals
    assert html.index('id="amap-totals"') < html.index("amap-available") < html.index('id="amap-cm"')
    assert section.count("</details>") == 2 and section.rstrip().endswith("</div>")
    assert "amap-available" not in section and 'id="amap-totals"' not in section
