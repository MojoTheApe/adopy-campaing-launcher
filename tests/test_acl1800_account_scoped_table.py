"""ACL-1800: командная таблица кампаний стоит ТОЛЬКО в блоке страны, под списком аккаунтов.

Решение владельца 12.10: отдельной секции «Campaigns» на Home нет; старый список «in the
account» убран; одна таблица на страну (аккаунты страны — полосами внутри), кнопка
«Read campaigns in <Country>» в заголовке страны читает аккаунты по очереди.

Компонент (`_campaign_table.html`, `hcMount`) выполняется в node на заглушке DOM — без
браузера и без Meta: данные — настоящие ответы `ci.campaign_rows` / `ci.child_rows` на
фикстурном снимке, сеть — заглушка `fetch`. Что видно на экране, читается из HTML, который
компонент пишет в таблицу. Браузерные проверки тех же вещей (скриншоты, диалог
подтверждения) лежат в `test_acl1798_home_campaigns_browser.py` и
`test_acl1800_home_commands_browser.py` и запускаются при свободной памяти больше 6 ГБ.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from tests.test_acl1798_campaign_inventory import D, ad, adset, camp, snapshot  # noqa: E402

TEMPLATES = ROOT / "src" / "web" / "templates"
REPORT = (TEMPLATES / "report.html").read_text(encoding="utf-8")
COUNTRY = (TEMPLATES / "_account_map_country.html").read_text(encoding="utf-8")
TABLE = (TEMPLATES / "_campaign_table.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


# ───────────────────────── разметка и подключение ─────────────────────────

def test_the_home_section_is_gone_and_the_component_is_included_once():
    assert not (TEMPLATES / "_home_campaigns.html").exists()
    assert "_home_campaigns" not in REPORT
    assert REPORT.count('{% include "_campaign_table.html" %}') == 1
    for gone in ('id="hc-root"', 'id="hc-title"', 'id="hc-seed"', "hc-country", "Read campaigns in ' + esc(a.country)"):
        assert gone not in TABLE and gone not in REPORT, gone
    # шаблон разметки один, без id: таблиц на странице много
    html = _render_table()
    assert html.count('<template id="hc-scoped-tpl">') == 1 and ' id="hc-table"' not in html


def _country_html(monkeypatch, backend):
    """Блок страны, отрисованный настоящим шаблоном приложения при заданном бэкенде."""
    from types import SimpleNamespace as NS

    from src.web.app import templates

    monkeypatch.setenv("ACL_FB_BACKEND", backend)
    mc = NS(country="Chile", key="chile", rows=[{"account": "Acc", "has_data": False, "ready": True, "in_workbook": True,
                                                  "status": {}, "to_do": ""}], virtual=False)
    return templates.env.get_template("_account_map_country.html").render(mc=mc, amap=NS(live={}, fallback={}, splits={}, countries=[mc]))


def test_the_backend_decides_which_campaign_ui_renders(monkeypatch):
    """rpa (по умолчанию): прежний список из Read spend; api: таблица страны. Обе — никогда для одной страны."""
    rpa = _country_html(monkeypatch, "rpa")
    assert "amap-campaigns" not in rpa and "Read campaigns in" not in rpa and "data-hc-mount" not in rpa
    assert '<span class="amap-caret-off">·</span>' in rpa                       # стрелку, как раньше, рисует JS из чтения спенда
    api = _country_html(monkeypatch, "api")
    assert api.count('class="hc hc-scoped amap-campaigns"') == 1 and "Read campaigns in Chile" in api
    assert '<span class="amap-caret-off">·</span>' in api                        # строка аккаунта одна и та же
    monkeypatch.delenv("ACL_FB_BACKEND", raising=False)                          # по умолчанию — rpa
    assert "amap-campaigns" not in _country_html(monkeypatch, "")


def test_the_page_flag_and_the_component_follow_the_backend(monkeypatch):
    from src.web.app import templates

    flag = next(l for l in REPORT.splitlines() if l.startswith("window.AMAP_CAMPAIGN_TABLE ="))
    include = '{% if campaign_table_api is defined and campaign_table_api() %}{% include "_campaign_table.html" %}{% endif %}'
    assert REPORT.count(include) == 1
    for backend, want in (("rpa", False), ("api", True)):
        monkeypatch.setenv("ACL_FB_BACKEND", backend)
        assert templates.env.from_string(flag).render().strip() == "window.AMAP_CAMPAIGN_TABLE = %s;" % str(want).lower()
        out = templates.env.from_string(include).render()
        assert ('id="hc-scoped-tpl"' in out) is want, backend                       # компонент грузится только под api


def test_the_old_list_is_kept_for_rpa_and_switched_off_under_api():
    for kept in ("function amapPaintTree(", "function amapTreeLine(", "function amapTreeNotRead(", "function amapLogAttach(",
                 "function amapTurnOffAsk(", "function amapCoverage(", "function amapResumeTree(", "in the account · "):
        assert kept in REPORT, kept
    paint = REPORT[REPORT.index("function amapPaintTree("):]
    head = paint[: paint.index("var wasOpen")]
    assert "if (window.AMAP_CAMPAIGN_TABLE) return;" in head                     # под api прежний список не рисуется
    mount = REPORT[REPORT.index("if (typeof window !== 'undefined' && typeof document !== 'undefined' && window.AMAP_CAMPAIGN_TABLE) {"):]
    assert "amapCampaignMountVisible" in mount[:400]                              # а таблица страны монтируется только под api


def test_the_old_paint_is_a_no_op_under_api_and_runs_under_rpa():
    src = REPORT[REPORT.index("function amapPaintTree("): REPORT.index("function amapPaintTree(") + 2500]
    head = src[: src.index("var wasOpen")] + "}"
    js = ("global.window = {AMAP_CAMPAIGN_TABLE: FLAG}; let touched = false;\n" + head +
          "\nconst row = {get parentNode() { touched = true; return null; }, classList: {}};\namapPaintTree(row, {});\nconsole.log(touched);")
    for flag, touched in (("true", "false"), ("false", "true")):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t.js").write_text(js.replace("FLAG", flag), encoding="utf-8")
            done = subprocess.run([shutil.which("node"), "t.js"], cwd=tmp, capture_output=True, text=True, timeout=30)
        assert done.returncode == 0 and done.stdout.strip() == touched, (flag, done.stdout, done.stderr)


def test_country_block_carries_tabs_the_read_button_and_one_tree():
    assert 'class="amap-btn amap-read-campaigns" onclick="amapReadCampaigns(this)"' in COUNTRY
    assert "Read campaigns in {{ mc.country }}" in COUNTRY and "Read all spend in {{ mc.country }}" in COUNTRY
    assert COUNTRY.count('class="hc hc-scoped amap-campaigns"') == 1 and COUNTRY.count("data-hc-mount") == 1
    assert "data-hc-tabs" in COUNTRY and "anchor_slug" not in COUNTRY      # вкладки строит JS: шаблон не требует серверных фильтров
    # одно дерево: тулбар и вкладки — НАД списком аккаунтов, подвал и журнал — под ним; отдельной таблицы кампаний нет
    assert COUNTRY.index('amap-campaigns"') < COUNTRY.index('<table class="amap-grid"') < COUNTRY.index("data-hc-after")
    assert '<span class="amap-caret-off">·</span>' in COUNTRY              # стрелку и остальное строке аккаунта добавляет JS (`amapCampaignTree`), только под api
    assert "amapCampaignToggle" not in COUNTRY + REPORT and "hc-band " not in TABLE


def test_opening_a_block_reads_nothing_from_meta():
    src = REPORT[REPORT.index("function amapCampaignMount("): REPORT.index("function amapPollSpend(")]
    assert "/campaign-inventory/read" not in src and "method:" not in src and "fetch(" not in src
    assert "readAll(" in src                      # чтение — только в обработчике кнопки страны
    assert "readAll" not in src[: src.index("function amapReadCampaigns(")]


def _render_table() -> str:
    from src.web.app import templates

    return templates.env.get_template("_campaign_table.html").render()


def test_tabs_link_only_to_sections_that_exist_on_the_page():
    src = REPORT[REPORT.index("function amapCampaignSlug("): REPORT.index("function amapCampaignMount(")]
    js = src + """
    const made = [];
    const present = new Set(IN.panels);
    global.document = {getElementById: id => (present.has(id) ? {} : null),
      createElement: tag => { const e = {tag, className: '', textContent: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }}; made.push(e); return e; }};
    const nav = {innerHTML: 'x', appendChild() {}};
    const host = {dataset: {country: IN.country}, querySelector: () => nav};
    amapCampaignTabs(host);
    console.log(JSON.stringify({slug: amapCampaignSlug(IN.country), tabs: made.map(e => ({tag: e.tag, text: e.textContent, cls: e.className, href: e.href || '', cur: e.attrs['aria-current'] || '', title: e.title || ''}))}));
    """
    def run(country, panels):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "t.js").write_text("const IN = " + json.dumps({"country": country, "panels": panels}) + ";\n" + js, encoding="utf-8")
            done = subprocess.run([shutil.which("node"), "t.js"], cwd=tmp, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    got = run("Chile", ["view-country-chile-manage", "view-country-chile-assets", "view-country-chile-creatives"])
    assert got["slug"] == "chile"
    by = {t["text"]: t for t in got["tabs"]}
    assert list(by) == ["Campaigns", "Winning setups", "Landings", "Targets", "Creatives", "Buyers"]
    assert by["Campaigns"]["tag"] == "a" and by["Campaigns"]["href"] == "#country-chile-manage" and by["Campaigns"]["cur"] == "page"
    assert by["Winning setups"]["href"] == "#country-chile-assets" and by["Creatives"]["href"] == "#country-chile-creatives"
    for off in ("Landings", "Targets", "Buyers"):
        assert by[off]["tag"] == "span" and "hc-tab-off" in by[off]["cls"] and by[off]["title"], off
    assert run("Côte d'Ivoire", [])["slug"] == "c-te-d-ivoire"                      # тот же слаг, что у `anchor_slug` на сервере
    assert run("  ", [])["slug"] == "unknown"


# ───────────────────────── серверная часть: цель и бенчмарк ─────────────────────────

def test_goal_context_uses_my_goals_and_benchmarks(monkeypatch, tmp_path):
    from src.web import app as web
    from src.web import goals as goals_store

    monkeypatch.setenv("MY_CM_NAME", "Buyer")
    monkeypatch.setattr(goals_store, "for_cm", lambda cm, seed=None: [
        {"country": "Chile", "ftd": 12, "cpa": 500}, {"country": "Peru", "ftd": 5, "cpa": 0}])
    import config.benchmarks as bm

    monkeypatch.setattr(bm, "get_benchmark", lambda country, offer=None: {"max_cpa": 650.0} if country == "Chile" else {"max_cpa": None})
    ok = web._inventory_goal_context(["Chile"])
    assert ok["goal_cpa"] == 500.0 and ok["goal_ftd"] == 12 and ok["benchmark_cpa"] == 650.0
    none = web._inventory_goal_context(["Peru"])
    assert none["goal_cpa"] is None and "No CPA goal is set for Peru" in none["goal_note"]
    assert none["benchmark_cpa"] is None and "No benchmark CPA is set for Peru" in none["benchmark_note"]
    unplaced = web._inventory_goal_context([])
    assert unplaced["goal_cpa"] is None and "not placed" in unplaced["goal_note"]


def test_the_tree_route_returns_the_context(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace as NS

    from starlette.datastructures import QueryParams

    from src.web import app as web

    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "campaign_inventory.json")
    ci._RUNNING.clear()
    ci.put_result(snapshot())
    monkeypatch.setattr(web, "_inventory_bi", lambda a, c, f, t, s: ({"campaigns": {}, "ads": {}}, ""))
    monkeypatch.setattr(web, "_inventory_goal_context", lambda c: {"country": "Kuwait", "goal_cpa": 500.0})
    resp = asyncio.run(web.campaign_inventory_tree(NS(query_params=QueryParams("account=Acc&country=Kuwait"))))
    assert json.loads(resp.body)["context"] == {"country": "Kuwait", "goal_cpa": 500.0}


def test_command_history_route_is_registered_before_the_op_route_and_filters_by_account(monkeypatch, tmp_path):
    from src.fb_api import commands as cmd
    from src.web import app as web
    from src.web import campaign_commands as cc

    paths = [r.path for r in web.app.routes if hasattr(r, "methods") and "GET" in r.methods]
    assert paths.index("/campaign-commands/history") < paths.index("/campaign-commands/{op_id}")
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    cmd._STORES.clear()
    store = cmd.get_store()
    for acct, state in (("Acc", "Succeeded"), ("Other", "Succeeded"), ("Acc", "Preview")):
        store.add({"op_id": f"op_{acct}_{state}", "state": state, "account": acct, "command": "set_status", "value": "ACTIVE",
                   "currency": "USD", "actor": "me", "created_at": f"2026-10-12T10:00:0{len(store._ops)}", "summary": "1 to change"})
    out, code = cc.account_history("Acc")
    assert code == 200 and [e["state"] for e in out["entries"]] == ["Succeeded"]        # превью и чужой аккаунт — не в журнале
    assert cc.account_history("")[1] == 400
    cmd._STORES.clear()


# ───────────────────────── компонент в node ─────────────────────────

def _component_js() -> str:
    html = _render_table()
    return re.findall(r"<script>(.*?)</script>", html, re.S)[-1]


def _fixtures() -> dict:
    snap = snapshot()
    snap.campaigns, _ = ci.parse_inventory([
        camp(1, "CL - Mercado Libre - Evergreen", daily_budget="15000", bid_strategy="LOWEST_COST_WITHOUT_CAP"),
        camp(2, "CL - Mercado Libre - Landing test", status="PAUSED"),
        camp(3, "CL - Spend with no FTDs", daily_budget="7500"),
    ], "campaign")
    snap.adsets, _ = ci.parse_inventory([
        adset(11, "Broad 30-55", 1, daily_budget="7500", bid_strategy="LOWEST_COST_WITH_BID_CAP", bid_amount="150"),
        adset(12, "Investment 40+", 1, daily_budget="7500"),
    ], "adset")
    snap.ads, _ = ci.parse_inventory([ad(111, "1121 - 1 - Feed + Stories", 11, 1)], "ad")
    snap.insights = {"campaign": {}, "adset": {}, "ad": {}}
    # Кампания Evergreen сходится по обоим ad set (1 200 + 790 = 1 990, 5 + 1 = 6 FTD): итог идёт путём ad_sets.
    bi = {"campaigns": {"Broad 30-55": {"spend": 1200.0, "leads": 140, "deposits": 5},
                        "Investment 40+": {"spend": 790.0, "leads": 64, "deposits": 1},
                        "CL - Spend with no FTDs": {"spend": 800.0, "leads": 30, "deposits": 0}}, "ads": {}}
    joined, summary = ci.join_bi(snap, bi)
    ctx = {"country": "Chile", "goal_cpa": 500.0, "goal_ftd": 12, "benchmark_cpa": 650.0, "goal_note": "", "benchmark_note": ""}
    other = snapshot()
    other.account = "Other"
    other.campaigns, _ = ci.parse_inventory([camp(9, "Other account campaign")], "campaign")
    other.adsets, other.ads = [], []
    return {
        "Acc": {"summary": ci.summary(snap, None), "rows": ci.campaign_rows(snap, joined), "bi": summary, "context": ctx,
                "kids": {"adset:1": ci.child_rows(snap, "adset", "1", joined), "ad:11": ci.child_rows(snap, "ad", "11", joined)}},
        "Other": {"summary": ci.summary(other, None), "rows": ci.campaign_rows(other, {}), "bi": {"available": False, "note": "x"},
                  "context": {"country": "Chile", "goal_cpa": None, "goal_note": "No CPA goal is set for Chile in My Goals",
                              "benchmark_cpa": None, "benchmark_note": "No benchmark CPA is set for Chile"}, "kids": {}},
        "Fresh": None,
    }
