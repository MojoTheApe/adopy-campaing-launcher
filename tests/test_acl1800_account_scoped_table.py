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


def test_country_block_carries_tabs_the_read_button_and_one_mount_point():
    assert 'class="amap-btn amap-read-campaigns" onclick="amapReadCampaigns(this)"' in COUNTRY
    assert "Read campaigns in {{ mc.country }}" in COUNTRY and "Read all spend in {{ mc.country }}" in COUNTRY
    assert COUNTRY.count('class="hc hc-scoped amap-campaigns"') == 1 and COUNTRY.count("data-hc-mount") == 1
    assert "data-hc-tabs" in COUNTRY and "anchor_slug" not in COUNTRY      # вкладки строит JS: шаблон не требует серверных фильтров
    # у строки аккаунта стрелки нет: «·» держит выравнивание, как до ACL-1800
    assert '<span class="amap-caret-off">·</span>' in COUNTRY and "amapCampaignToggle" not in COUNTRY + REPORT


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


_HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const IN = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const calls = [], posts = [];
function mkEl(name) {
  const e = {name, innerHTML: '', textContent: '', style: {setProperty() {}}, dataset: {}, hidden: false, value: '', listeners: {},
    scrollLeft: 0, scrollTop: 0, scrollWidth: 800, clientWidth: 400, disabled: false, title: '', open: false,
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    querySelector(sel) { return /option/.test(sel) ? {hidden: false} : null; },
    querySelectorAll() { return []; }, closest() { return null; }};
  return e;
}
function mkRoot() {
  const els = {}, vars = {};
  const root = {dataset: IN.period, style: {setProperty(k, v) { vars[k] = v; }}, classList: {add() {}, remove() {}},
    querySelector(sel) {
      const m = /\[data-hc="([^"]+)"\]/.exec(sel);
      const key = m ? m[1] : sel;
      if (!els[key]) { els[key] = mkEl(key); if (key === 'frame') els[key].clientWidth = IN.frameW || 1400; }
      return els[key];
    }};
  return {root, els, vars};
}
const R1 = mkRoot(), root = R1.root, els = R1.els;
const win = {listeners: {}, addEventListener() {}, localStorage: null};
let nextReply = null;
const store = Object.assign({}, IN.storage || {}), docL = {};
function reply(body, status) { return Promise.resolve({ok: (status || 200) < 400, status: status || 200, json: () => Promise.resolve(body)}); }
async function fakeFetch(url, init) {
  const u = new URL(url, 'http://t');
  calls.push((init && init.method || 'GET') + ' ' + u.pathname + u.search);
  if (u.pathname === '/campaign-inventory/status') {
    const accs = u.searchParams.getAll('account');
    if (accs.length === 1 && init === undefined && IN.statusAfterRead) return reply({ok: true, running: [], accounts: {[accs[0]]: IN.data[accs[0]] ? IN.data[accs[0]].summary : {state: 'idle'}}});
    const out = {};
    accs.forEach(a => { out[a] = IN.data[a] ? IN.data[a].summary : {state: 'idle'}; });
    return reply({ok: true, needs_api: false, running: [], accounts: out});
  }
  if (u.pathname === '/campaign-inventory/tree') {
    const d = IN.data[u.searchParams.get('account')];
    return reply(d ? {ok: true, has_snapshot: true, summary: d.summary, bi: d.bi, context: d.context, rows: d.rows} : {ok: true, has_snapshot: false, summary: {state: 'idle'}, rows: []});
  }
  if (u.pathname === '/campaign-inventory/children') {
    const d = IN.data[u.searchParams.get('account')];
    const k = d && d.kids[u.searchParams.get('level') + ':' + u.searchParams.get('parent')];
    return reply(k ? Object.assign({ok: true}, k) : {ok: true, state: 'empty', rows: [], note: ''});
  }
  if (u.pathname === '/campaign-inventory/read') { posts.push(init.body.get('account')); return reply({ok: true}); }
  if (u.pathname === '/campaign-commands/history') return reply({ok: true, entries: []});
  if (u.pathname === '/account-spend/turn-off-history') return reply({ok: true, entries: []});
  return reply({ok: false, error: 'unexpected ' + u.pathname}, 404);
}
const sandbox = {
  window: win, document: {getElementById: () => null, querySelector: () => null, createElement: () => ({}),
    addEventListener(t, f) { docL[t] = f; }, removeEventListener(t) { delete docL[t]; }},
  localStorage: {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }},
  fetch: fakeFetch, setTimeout: (f) => { Promise.resolve().then(f); return 0; }, clearTimeout() {}, Date, JSON, Promise, Object, Array, String, Number, Math, URL,
  console
};
win.fetch = fakeFetch;
if (IN.acm) new Function(...Object.keys(sandbox), fs.readFileSync(IN.acm, 'utf8'))(...Object.values(sandbox));
new Function(...Object.keys(sandbox), code)(...Object.values(sandbox));
const handle = {closest: sel => (sel === '.hc-resize' ? handle : null)};
const key = (k, extra) => Object.assign({key: k, target: handle, preventDefault() { this.stopped = true; }}, extra || {});
const colw = html => { const m = /<colgroup>((?:<col[^>]*>)+)<\/colgroup>/.exec(html); return m ? [...m[1].matchAll(/width:(\d+)px/g)].map(x => Number(x[1])) : []; };
async function resizeScenario() {
  const out = {};
  const a = win.hcMount(root, {scoped: true, seed: IN.seed}); await a.init();
  const R2 = mkRoot(); const b = win.hcMount(R2.root, {scoped: true, seed: IN.seed}); await b.init();
  out.initial = {w: a.pinWidth(), cols: colw(els.table.innerHTML), stored: store['acl.hc.pinw.v1'] === undefined};
  const hm = /class="hc-resize" role="separator" aria-orientation="vertical" tabindex="0" aria-label="[^"]+" aria-valuemin="240" aria-valuemax="(\d+)" aria-valuenow="(\d+)"/.exec(els.table.innerHTML);
  out.handle = hm ? [hm[1], hm[2]] : null;
  // клавиатура: стрелки по 16, Shift по 64, Home/End — края; ширина запоминается и доходит до второй таблицы
  const k1 = key('ArrowRight'); els.table.listeners.keydown[0](k1);
  out.right = {w: a.pinWidth(), stored: store['acl.hc.pinw.v1'], prevented: !!k1.stopped, other: b.pinWidth(), var: R1.vars['--hc-pin-w'], table: els.table.style.width};
  els.table.listeners.keydown[0](key('ArrowLeft', {shiftKey: true})); out.leftShift = a.pinWidth();
  els.table.listeners.keydown[0](key('Home')); out.home = a.pinWidth();
  els.table.listeners.keydown[0](key('End')); out.end = a.pinWidth();
  els.table.listeners.keydown[0]({key: 'a', target: handle, preventDefault() { throw new Error('other keys must pass'); }});
  // мышь: тянуть живо, запоминать по отпусканию
  a.setPin(432, true);
  const md = {target: handle, button: 0, clientX: 500, preventDefault() {}};
  els.table.listeners.mousedown[0](md);
  docL.mousemove({clientX: 620}); out.dragLive = {w: a.pinWidth(), stored: store['acl.hc.pinw.v1'], hasUp: typeof docL.mouseup};
  docL.mouseup({}); out.dragDone = {w: a.pinWidth(), stored: store['acl.hc.pinw.v1'], moveGone: docL.mousemove === undefined, other: b.pinWidth()};
  a.render(); out.cols_after_drag = colw(els.table.innerHTML);
  els.table.listeners.mousedown[0](md); docL.mousemove({clientX: -5000}); docL.mouseup({}); out.dragMin = a.pinWidth();
  els.table.listeners.mousedown[0](md); docL.mousemove({clientX: 99999}); docL.mouseup({}); out.dragMax = a.pinWidth();
  // двойной щелчок: сброс
  els.table.listeners.dblclick[0]({target: handle}); out.reset = {w: a.pinWidth(), stored: store['acl.hc.pinw.v1'], other: b.pinWidth()};
  els.table.listeners.mousedown[0]({target: {closest: () => null}, button: 0, clientX: 1, preventDefault() { throw new Error('not the handle'); }});
  // узкое окно: справа остаётся прокручиваемая часть
  els.frame.clientWidth = 700; a.setPin(2000, false); out.narrow = a.pinWidth();
  els.frame.clientWidth = 0; a.setPin(2000, false); out.hidden = a.pinWidth();
  console.log(JSON.stringify(out));
}
async function msgScenario() {
  const out = {};
  const inst = win.hcMount(root, {scoped: true, seed: IN.seed}); await inst.init();
  const ACM = win.acmCommands;
  const raw = "Not sent: Facebook_5414_x the profile could not be opened with automation: HTTPSConnectionPool(host='launcher.mlx.yt', port=45001): Max retries exceeded (Caused by NewConnectionError: [WinError 10061] No connection could be made because the target machine actively refused it) <b>x</b>";
  inst.state.notes['campaign:1'] = ACM.explain(raw, 'bad', '');
  inst.state.notes['campaign:3'] = ACM.explain('Confirmed', 'ok');
  inst.render();
  const row = key => (new RegExp('<tr class="hc-r-campaign[^"]*" data-key="' + key + '".*?</tr>', 's')).exec(els.table.innerHTML);
  out.statusCell = (/<td class="p2">.*?<\/td>/s.exec(row('campaign:1')[0]) || [''])[0];
  const sub = html => /<tr class="hc-rowmsg[^"]*" data-msg="row:campaign:1".*?<\/tr>/s.exec(html);
  out.sub = sub(els.table.innerHTML) && sub(els.table.innerHTML)[0];
  out.order = els.table.innerHTML.indexOf('data-key="campaign:1"') < els.table.innerHTML.indexOf('data-msg="row:campaign:1"');
  out.okSub = /data-msg="row:campaign:3"[^>]*>.*?<\/tr>/s.exec(els.table.innerHTML)[0];
  // Details: раскрыть и свернуть
  const click = id => els.table.listeners.click[0]({target: {closest: sel => (/data-details/.test(sel) ? {dataset: {details: id}} : null)}});
  click('row:campaign:1'); out.opened = sub(els.table.innerHTML)[0];
  click('row:campaign:1'); out.closed = sub(els.table.innerHTML)[0];
  // сбой чтения аккаунта: одна строка и Details
  inst.state.summaries.Acc = Object.assign({}, inst.state.summaries.Acc, {attempt: {state: 'failed', at: '2026-10-12 08:54:22', error: raw.replace('Not sent: ', ''), code: ''}});
  inst.render();
  out.band = /data-msg="acct:Acc:read".*?<\/tr>/s.exec(els.table.innerHTML)[0];
  console.log(JSON.stringify(out));
}
(async () => {
  if (IN.scenario === 'resize') { await resizeScenario(); return; }
  if (IN.scenario === 'msg') { await msgScenario(); return; }
  const out = {};
  const inst = win.hcMount(root, {scoped: true, seed: IN.seed});
  await inst.init();
  out.pin_initial = inst.pinWidth();
  const table = () => els.table.innerHTML;
  out.table = table(); out.count = els.count.textContent; out.foot = els.foot.innerHTML; out.body_hidden = els.body.hidden;
  out.preset_value = els.preset.value; out.cols_pop = els['cols-pop'].innerHTML;
  // фильтр доставки
  els.delivery.value = 'paused'; els.delivery.listeners.change[0].call(els.delivery);
  out.paused_only = table(); out.paused_count = els.count.textContent;
  els.delivery.value = 'all'; els.delivery.listeners.change[0].call(els.delivery);
  // развернуть всё
  const before = calls.length;
  await new Promise(r => { els['expand-all'].listeners.click[0](); setTimeout(r, 0); });
  for (let i = 0; i < 30; i++) await Promise.resolve();
  await new Promise(r => setImmediate(r));
  out.expanded = table(); out.expand_calls = calls.slice(before);
  els['collapse-all'].listeners.click[0]();
  out.collapsed = table();
  // поиск
  els.search.value = 'spend with'; els.search.listeners.input[0].call(els.search);
  out.search = table(); els.search.value = ''; els.search.listeners.input[0].call(els.search);
  // пресеты и группы
  els.preset.value = 'all'; els.preset.listeners.change[0].call(els.preset);
  out.all = table();
  // очередь чтений аккаунтов страны
  const q0 = calls.length;
  await inst.readAll();
  out.posts = posts.slice();
  out.read_calls = calls.slice(q0).filter(c => c.startsWith('POST'));
  out.busy_after = inst.state.busy;
  out.hist = null;
  await new Promise(r => { inst.loadHistory(); setImmediate(r); });
  await new Promise(r => setImmediate(r));
  out.history = els['history-body'].innerHTML || els['history-body'].textContent;
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
"""


ACM_JS = ROOT / 'src' / 'web' / 'static' / 'acm_commands.js'


def _run(period_ok=True, seed=None, statusAfterRead=False, scenario='', storage=None, frameW=1400) -> dict:
    data = _fixtures()
    js = _component_js()
    seed = seed or [{"country": "Chile", "accounts": ["Acc", "Other", "Fresh"]}]
    period = {"periodFrom": D if period_ok else "", "periodTo": D if period_ok else "", "periodSpan": "" if period_ok else "all"}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "h.js").write_text(_HARNESS, encoding="utf-8")
        (Path(tmp) / "c.js").write_text(js, encoding="utf-8")
        (Path(tmp) / "in.json").write_text(json.dumps({"data": data, "seed": seed, "period": period, "statusAfterRead": statusAfterRead,
                                                    "scenario": scenario, "storage": storage or {}, "frameW": frameW,
                                                    "acm": str(ACM_JS) if scenario == "msg" else ""}), encoding="utf-8")
        done = subprocess.run([shutil.which("node"), "h.js", "c.js", "in.json"], cwd=tmp, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def page():
    return _run()


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_one_table_covers_every_account_of_the_country_and_only_those(page):
    t = page["table"]
    assert t.count('class="hc-band ') == 3                                      # по полосе на аккаунт страны
    for acct in ("Acc", "Other", "Fresh"):
        assert f'data-read="{acct}"' in t, acct                                  # и у каждого своя кнопка чтения
    assert "CL - Mercado Libre - Evergreen" in t and "Other account campaign" in t
    assert "Not read yet — press Read campaigns" in t                            # Fresh: снимка нет
    assert "IN THE ACCOUNT" not in t.upper() and "What was changed here" not in t
    scoped = _run(seed=[{"country": "Chile", "accounts": ["Other"]}])["table"]
    assert "Other account campaign" in scoped and "Evergreen" not in scoped      # таблица рисует только своё дерево


def test_header_count_line_toolbar_and_hint_follow_the_prototype(page):
    assert page["count"] == "4 campaigns · Expand to inspect ad sets and ads · 0 selected"   # Acc: 3, Other: 1
    tpl = _render_table()
    for needle in ("Expand all", "Collapse all", "Search campaigns, ad sets or ads", "All delivery", "Enabled campaigns",
                   "Paused campaigns", ">Controls<", ">BI metrics<", ">Meta metrics<", ">Tracking<",
                   "Identity stays pinned", "Scroll to see all controls and metrics", 'data-hc="top"', "What was changed here"):
        assert needle in tpl, needle
    assert page["body_hidden"] is False


def test_column_groups_and_order_follow_the_prototype(page):
    heads = re.findall(r"<th[^>]*>(.*?)</th>", page["table"])
    names = [re.sub(r"<[^>]+>", "", h) for h in heads]
    ctrl = ["Delivery", "Duplicate", "Bid control", "Budget", "Optimization hint", "Alerts", "Goal CPA", "Benchmark CPA"]
    at = names.index("Delivery")
    assert names[at: at + 8] == ctrl
    assert names[at + 8: at + 11] == ["Spend", "Leads", "FTDs"]                  # BI идёт сразу за управлением
    assert "MANAGEMENT &amp; DECISIONS" in page["table"] or "MANAGEMENT & DECISIONS" in page["table"]
    assert "BI · business outcomes" in page["table"]
    assert "Account / campaign hierarchy" in page["table"] and "Campaign / Ad set / Ad" in page["table"]
    allcols = re.findall(r"<th[^>]*>(.*?)</th>", page["all"])
    flat = [re.sub(r"<[^>]+>", "", h) for h in allcols]
    for h in ("Deposits (all)", "CR lead→FTD", "Keitaro", "Placements", "Devices", "Landing Variant", "History"):
        assert h in flat, h
    assert "META · delivery" in page["all"] and "TRACKER &amp; CONTEXT" in page["all"].replace("TRACKER & CONTEXT", "TRACKER &amp; CONTEXT")


def test_rows_pin_identity_and_show_level_country_account(page):
    t = page["table"]
    assert 'data-sel="campaign:1"' in t and 'data-exp="campaign:1"' in t and 'class="hc-switch"' in t
    assert re.search(r"Chile · Acc", _text(t)) and "Campaign" in t
    assert 'class="p3"' in t


def test_goal_benchmark_and_hint_use_real_inputs_or_say_why_not(page):
    t = page["table"]
    row = lambda key: re.search(r'<tr class="hc-r-campaign[^"]*" data-key="%s".*?</tr>' % key, t, re.S).group(0)
    ever = _text(row("campaign:1"))
    assert "$500.00" in ever and "$650.00" in ever                                # Goal CPA / Benchmark CPA
    assert "Within CPA goal" in ever and "6 FTDs" in ever and "$1,990.00" in ever  # 1990/6 = 331.67 ≤ 500
    assert "Spend above goal CPA, no FTDs" in _text(row("campaign:3"))             # 800 ≥ 500 и ни одного FTD
    assert "alert" in row("campaign:3")                                            # и это же оповещение
    paused = row("campaign:2")
    assert "Switched off — nothing to advise" in paused                            # пауза: совета нет, причина названа
    other = re.search(r'<tr class="hc-r-campaign[^"]*" data-key="campaign:9".*?</tr>', t, re.S).group(0)
    assert "No CPA goal is set for Chile in My Goals" in other and "No benchmark CPA is set for Chile" in other
    assert "No alerts from the data on screen" in other


def test_every_command_is_disabled_with_a_reason_without_the_helper(page):
    t = page["table"]
    assert 'class="hc-cmd hc-cmd-txt" disabled aria-disabled="true"' in t        # Duplicate / Copy to…
    assert 'title="ACL-1773"' in t and "Copy to… " in t
    assert re.search(r'class="hc-switch"[^>]*disabled aria-disabled="true"', t)
    assert "Commands unavailable: the command helper did not load" in t           # в этом стенде помощника нет
    assert not re.search(r"Coming in ACL-1800", t)


def test_budget_and_bid_columns_show_the_value_and_the_owner_note(page):
    expanded = page["expanded"]
    t = _text(expanded)
    assert "$150.00 / day" in t and "$75.00 / day" in t and "Ad set budget" in t and "Campaign budget" in t
    assert "Ad set bids" in t                                                    # у кампании ставки живут на ad set
    assert "$1.50" in t                                                          # ad set с ручной ставкой показывает её
    assert "Ads have no budget" in expanded and "Managed by ad set" in expanded   # объявление: ни бюджета, ни ставки


def test_delivery_filter_and_search_narrow_the_rows(page):
    assert 'data-key="campaign:2"' in page["paused_only"] and 'data-key="campaign:1"' not in page["paused_only"]
    assert "of" in page["paused_count"]
    assert 'data-key="campaign:3"' in page["search"] and 'data-key="campaign:1"' not in page["search"]


def test_expand_all_reads_children_from_the_local_snapshot_only(page):
    assert 'data-key="adset:11"' in page["expanded"] and 'data-key="adset:12"' in page["expanded"]
    assert page["expand_calls"] and all(c.startswith("GET /campaign-inventory/children") for c in page["expand_calls"])
    assert 'data-key="adset:11"' not in page["collapsed"]


def test_reading_a_country_queues_one_account_at_a_time_and_posts_only_there(page):
    assert page["posts"] == ["Acc", "Other", "Fresh"]                              # порядок аккаунтов страны, по одному
    assert page["busy_after"] is False
    assert all(c.startswith("POST /campaign-inventory/read") for c in page["read_calls"]) and len(page["read_calls"]) == 3


def test_the_period_gate_blocks_reads(tmp_path):
    out = _run(period_ok=False)
    assert out["posts"] == []
    assert 'data-read="Acc" disabled' in out["table"] or "disabled" in re.search(r'<button[^>]*data-read="Acc"[^>]*>', out["table"]).group(0)


def test_what_was_changed_here_covers_the_country(page):
    assert "Nothing has been changed in this country from here." in page["history"]


# ───────────────────────── ширина закреплённого блока ─────────────────────────

def test_the_pinned_column_resizes_by_keyboard_mouse_and_double_click():
    out = _run(scenario="resize")
    assert out["initial"]["w"] == 432 and out["initial"]["cols"][:4] == [36, 28, 88, 280] and out["initial"]["stored"] is True
    assert out["handle"] == ["840", "432"]                                                           # aria-valuemax: 60% от 1400 px; valuenow
    right = out["right"]
    assert right["w"] == 448 and right["stored"] == "448" and right["prevented"] is True            # стрелка вправо: +16
    assert right["other"] == 448 and right["var"] == "448px"                                         # вторая таблица страны следует
    assert right["table"].endswith("px") and int(right["table"][:-2]) >= 448
    assert out["leftShift"] == 384 and out["home"] == 240 and out["end"] == 840                      # Shift: 64; Home/End — края
    assert out["dragLive"] == {"w": 552, "stored": "432", "hasUp": "function"}                       # живо, но ещё не запомнено
    assert out["dragDone"] == {"w": 552, "stored": "552", "moveGone": True, "other": 552}
    assert out["cols_after_drag"][:4] == [36, 28, 88, 552 - 152]                                     # шапка и строки — одни и те же <col>
    assert out["dragMin"] == 240 and out["dragMax"] == 840
    assert out["reset"] == {"w": 432, "stored": "432", "other": 432}
    assert out["narrow"] == 420                                                                      # 60% от 700; справа остаётся 280 px
    assert out["hidden"] == 900                                                                      # таблица скрыта — без предела по ширине окна


def test_the_saved_width_is_used_and_garbage_is_ignored():
    assert _run(storage={"acl.hc.pinw.v1": "600"})["pin_initial"] == 600
    for bad in ("abc", "100", "99999", ""):
        assert _run(storage={"acl.hc.pinw.v1": bad})["pin_initial"] == 432, bad
    narrow = _run(storage={"acl.hc.pinw.v1": "900"}, frameW=1000)
    assert narrow["pin_initial"] == 600                                                              # не шире 60% окна; сохранённое не затираем


def test_names_wrap_to_two_lines_and_keep_the_full_name_in_the_tooltip(page):
    css = TABLE[: TABLE.index("</style>")]
    clamp = css[css.index(".hc-name b {"): css.index(".hc-name span")]
    assert "-webkit-line-clamp: 2" in clamp and "white-space: normal" in clamp
    assert ".hc-table td.p3 { white-space: normal; }" in css
    assert 'class="p3"><div class="hc-id"><span class="hc-badge">Campaign</span><span class="hc-name"><b title="CL - Mercado Libre - Evergreen (id ' in page["table"]
    assert 'class="hc-resize" role="separator"' in page["table"] and 'aria-label="Resize the name column' in page["table"]


# ───────────────────────── сообщения: короткая строка под строкой, не в ячейке Status ─────────────────────────

def _map(cases):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "m.js").write_text(
            "const fs = require('fs'); const win = {}; new Function('window', fs.readFileSync(process.argv[2], 'utf8'))(win);\n"
            "const IN = JSON.parse(process.argv[3]);\n"
            "console.log(JSON.stringify(IN.map(([t, c, tone]) => [win.acmCommands.friendly(t, c), win.acmCommands.explain(t, tone || 'bad', c)])));",
            encoding="utf-8")
        done = subprocess.run([shutil.which("node"), "m.js", str(ACM_JS), json.dumps(cases)], cwd=tmp, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


LAUNCHER = ("Not sent: Facebook_5414_x the profile could not be opened with automation: HTTPSConnectionPool(host='launcher.mlx.yt', "
            "port=45001): Max retries exceeded (Caused by NewConnectionError: [WinError 10061] No connection could be made)")
HAND = "the profile is open but has no automation port (opened by hand in MLX Desktop?). Close it in MLX Desktop or open it from the dashboard, then retry."
LOOP = "Campaign commands are only available when the dashboard listens on this computer alone (127.0.0.1). It is bound to a network address."
RPA = "Campaign commands need the Graph API backend (ACL_FB_BACKEND=api). This machine uses the RPA backend, so nothing was changed."


@pytest.mark.parametrize("text,code,want", [
    (LAUNCHER, "", "Multilogin isn't running — start it and try again"),
    ("HTTPSConnectionPool(host='127.0.0.1', port=45001): refused", "", "Multilogin isn't running — start it and try again"),
    ("[WinError 10061] No connection could be made", "", "Multilogin isn't running — start it and try again"),
    ("ProfileError: " + HAND, "open_by_hand", "ProfileError: " + HAND),                                   # уже человеческий текст сервера
    ("Not sent: the profile restarted a minute ago; try again in 3 min.", "restart_cooldown", "the profile restarted a minute ago; try again in 3 min."),
    ("The Facebook_5 profile is logged out.", "logged_out", "Logged out in this profile — log in inside Multilogin, then retry"),
    ("session says: logged out of facebook", "", "Logged out in this profile — log in inside Multilogin, then retry"),
    ("The profile is on a Facebook checkpoint; solve it", "", "Facebook checkpoint — resolve it inside the profile"),
    ("The Ads Manager tab stopped answering on the last read", "", "Ads Manager stopped answering — try again in a few minutes"),
    ("tab is dead", "page_unresponsive", "Ads Manager stopped answering — try again in a few minutes"),
    ("Multilogin answered 403: restricted permission for this profile", "",
     "Multilogin refused to start this profile (permission) — another user may be signed in to the same Multilogin account"),
    ("The account currency changed since the preview.", "stale", "Changed since the preview — review again"),
    ("Failed — previous state kept: value changed since the preview. Nothing was written.", "", "Changed since the preview — review again"),
    (LOOP, "not_loopback", LOOP), (RPA, "rpa_backend", RPA),
])
def test_friendly_messages_map_the_common_causes(text, code, want):
    (f, ex), = _map([[text, code]])
    assert f == want and ex["text"] == want and ex["tone"] == "bad"
    if want != text:
        assert ex["raw"] == text                                                                      # сырой текст — в Details


def test_unknown_errors_say_something_went_wrong_and_keep_the_raw_text():
    (f, ex), (f2, ex2), (f3, ex3) = _map([["Not sent: kaboom", "", "bad"], ["Failed — previous state kept: Meta refused", "", "bad"],
                                          ["Confirmed", "", "ok"]])
    assert f is None and ex["text"] == "Something went wrong — see Details" and ex["raw"] == "Not sent: kaboom"
    assert ex2["text"] == "Failed — previous state kept: Meta refused" and ex2["raw"] is None          # известный исход короткий — Details не нужен
    assert ex3 == {"text": "Confirmed", "raw": None, "tone": "ok"}
    long = "Failed — previous state kept: " + "x" * 200
    (_, ex4), = _map([[long, "", "bad"]])
    assert ex4["text"] == "Failed — previous state kept" and ex4["raw"] == long


def test_the_message_is_a_full_width_sub_row_not_a_status_cell_note():
    out = _run(scenario="msg")
    assert "hc-mk bad" in out["statusCell"]                                                              # в ячейке — только маркер с подсказкой
    assert "WinError" not in out["statusCell"] and "HTTPSConnectionPool" not in out["statusCell"]       # сырой текст в узкую ячейку не попадает
    assert out["order"] is True                                                                          # строка сообщения — сразу под своей строкой
    sub = out["sub"]
    assert re.search(r'<td colspan="\d+">', sub) and int(re.search(r'colspan="(\d+)"', sub).group(1)) > 4        # на всю ширину таблицы
    assert "Multilogin isn&#39;t running — start it and try again" in sub and "WinError" not in sub
    assert 'class="hc-rm-btn" data-details="row:campaign:1" aria-expanded="false">Details' in sub
    assert "hc-rm-raw" not in sub                                                                         # свёрнуто
    assert 'class="hc-rm-btn"' not in out["okSub"] and "Confirmed" in out["okSub"]                      # у «Confirmed» деталей нет


def test_details_expands_with_the_escaped_raw_text_and_collapses_again():
    out = _run(scenario="msg")
    assert 'aria-expanded="true"' in out["opened"] and 'class="hc-rm-raw"' in out["opened"]
    assert "WinError 10061" in out["opened"] and "launcher.mlx.yt" in out["opened"]
    assert "&lt;b&gt;x&lt;/b&gt;" in out["opened"] and "<b>x</b>" not in out["opened"]                   # экранировано
    assert "hc-rm-raw" not in out["closed"] and 'aria-expanded="false"' in out["closed"]


def test_an_account_read_failure_is_one_line_plus_details():
    band = _run(scenario="msg")["band"]
    assert "The last read failed (2026-10-12 08:54:22) — Multilogin isn&#39;t running — start it and try again" in band
    assert 'data-details="acct:Acc:read"' in band and "WinError" not in band
    assert "hc-rm-raw" not in band
    css = TABLE[: TABLE.index("</style>")]
    assert ".hc-rm-text { min-width: 0; flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" in css
    assert "user-select: text" in css and "hc-rnote" not in TABLE


def test_the_mapping_lives_in_one_place():
    assert TABLE.count("launcher.mlx.yt") == 0 and "Multilogin isn't running" not in TABLE
    assert "Multilogin isn't running" in ACM_JS.read_text(encoding="utf-8")
