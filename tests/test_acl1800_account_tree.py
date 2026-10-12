"""ACL-1800: одно дерево — строка аккаунта родитель, под ней раскрываются её кампании, ad set'ы и объявления.

Решение владельца 12.10: кампании не отдельный блок. Строка аккаунта в списке страны сама раскрывается
(клик по строке, стрелка, Enter/Space), под ней — таблица команд только по этому аккаунту; полос аккаунтов
внутри таблицы больше нет, их «Read campaigns», возраст снимка и строка статуса/ошибки живут на самой строке.

Компонент (`_campaign_table.html`, `hcMount`) выполняется в node на заглушке DOM: узлы строк (стрелка, строка
снимка, кнопки, рамка прокрутки, таблица) подставляет тест, данные — настоящие ответы `ci.campaign_rows` /
`ci.child_rows`, сеть — заглушка `fetch`. Что видно на экране, читается из HTML, который компонент пишет в
таблицу аккаунта. Браузерные проверки (скриншоты, диалог подтверждения) — в `test_acl1798_home_campaigns_browser.py`
и `test_acl1800_home_commands_browser.py`; они идут при свободной памяти больше 6 ГБ.
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

from tests.test_acl1798_campaign_inventory import D  # noqa: E402
from tests.test_acl1800_account_scoped_table import REPORT, TABLE, _component_js, _fixtures, _render_table  # noqa: E402

ACM_JS = ROOT / "src" / "web" / "static" / "acm_commands.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

_HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const IN = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const calls = [], posts = [], warmCalls = [];
function mkEl(name) {
  const e = {name, innerHTML: '', textContent: '', dataset: {}, hidden: false, value: '', listeners: {}, attrs: {}, cls: new Set(),
    scrollLeft: 0, scrollTop: 0, scrollWidth: 2400, clientWidth: IN.frameW || 1400, disabled: false, title: '', open: false, vars: {},
    style: {}, tabIndex: 0,
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k]; },
    querySelector(sel) { return /option/.test(sel) ? {hidden: false} : null; }, querySelectorAll() { return []; }, closest() { return null; }};
  e.classList = {add: c => e.cls.add(c), remove: c => e.cls.delete(c), contains: c => e.cls.has(c),
    toggle: (c, on) => { if (on === undefined ? !e.cls.has(c) : on) e.cls.add(c); else e.cls.delete(c); }};
  e.style.setProperty = (k, v) => { e.vars[k] = v; };
  return e;
}
function mkTree(accounts) {
  const h = {};
  accounts.forEach(a => {
    const o = {};
    ['row', 'caret', 'snap', 'readBtn', 'chip', 'openBtn', 'child', 'embed', 'frame', 'scroll', 'top', 'topIn', 'table'].forEach(k => { o[k] = mkEl(k + ':' + a); });
    o.child.hidden = true; o.row.attrs['aria-expanded'] = 'false';
    if (IN.ownWarm === false) { o.chip = null; o.openBtn = null; }
    h[a] = o;
  });
  return h;
}
function mkRoot() {
  const els = {}, vars = {};
  const root = {dataset: IN.period, style: {setProperty(k, v) { vars[k] = v; }}, classList: {add() {}, remove() {}},
    querySelector(sel) {
      const m = /\[data-hc="([^"]+)"\]/.exec(sel);
      const key = m ? m[1] : sel;
      if (!els[key]) els[key] = mkEl(key);
      return els[key];
    }};
  return {root, els, vars, H: mkTree((IN.seed[0] || {accounts: []}).accounts)};
}
const R1 = mkRoot(), root = R1.root, els = R1.els, H = R1.H;
const win = {listeners: {}, addEventListener() {}, localStorage: null};
const store = Object.assign({}, IN.storage || {}), docL = {};
function reply(body, status) { return Promise.resolve({ok: (status || 200) < 400, status: status || 200, json: () => Promise.resolve(body)}); }
async function fakeFetch(url, init) {
  const u = new URL(url, 'http://t');
  calls.push((init && init.method || 'GET') + ' ' + u.pathname + u.search);
  if (u.pathname === '/campaign-inventory/status') {
    const out = {};
    u.searchParams.getAll('account').forEach(a => { out[a] = IN.data[a] ? IN.data[a].summary : {state: 'idle'}; });
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
  if (u.pathname.startsWith('/warm/')) {
    const W = IN.warm;
    if (!W) return reply({error: 'not found'}, 404);                                  // ветки ACL-1812 нет: маршрутов тоже нет
    if (u.pathname === '/warm/state') { warmCalls.push('GET /warm/state?' + u.search.slice(1)); return reply({available: W.available !== false, actions: W.actions !== false, accounts: W.accounts}); }
    if (u.pathname === '/warm/open') { warmCalls.push('POST /warm/open ' + init.body); return reply({job_id: 'j1', state: 'running'}, 202); }
    if (u.pathname.startsWith('/warm/job/')) { warmCalls.push('GET ' + u.pathname); return reply({state: 'done', results: W.results}); }
  }
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
const tick = async (n) => { for (let i = 0; i < (n || 30); i++) await new Promise(r => setImmediate(r)); };
// событие с целью: карта «селектор → найденный узел»; ни один селектор не подошёл — closest вернёт null
const T = (map) => ({closest: sel => { for (const k of Object.keys(map || {})) if (sel.indexOf(k) >= 0) return map[k]; return null; }});
const BTN = {'button, a, input': {classList: {contains: () => false}}};
const CARET = {'button, a, input': {classList: {contains: c => c === 'amap-caret'}}};
const colw = html => { const m = /<colgroup>((?:<col[^>]*>)+)<\/colgroup>/.exec(html); return m ? [...m[1].matchAll(/width:(\d+)px/g)].map(x => Number(x[1])) : []; };
const rowOf = (html, key) => (new RegExp('<tr class="hc-r-[a-z]+[^"]*" data-key="' + key + '".*?</tr>', 's')).exec(html);
(async () => {
  const out = {};
  const inst = win.hcMount(root, {scoped: true, seed: IN.seed, rows: H});
  const ctx = {inst, H, els, R1, calls, posts, warmCalls, win, store, docL, tick, T, BTN, CARET, colw, rowOf, out, mkRoot,
    mount: (r) => win.hcMount(r.root, {scoped: true, seed: IN.seed, rows: r.H}), sandbox};
  if (!IN.noinit) await inst.init();
  await tick(10);
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const ret = await new AsyncFunction('ctx', 'with (ctx) {' + IN.script + '\n}')(ctx);
  console.log(JSON.stringify(ret === undefined ? out : ret));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
"""


def _node(script="", *, period_ok=True, seed=None, storage=None, frameW=1400, warm=None, acm=True, noinit=False, ownWarm=True) -> dict:
    data = _fixtures()
    seed = seed or [{"country": "Chile", "accounts": ["Acc", "Other", "Fresh"]}]
    period = {"periodFrom": D if period_ok else "", "periodTo": D if period_ok else "", "periodSpan": "" if period_ok else "all"}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "h.js").write_text(_HARNESS, encoding="utf-8")
        (Path(tmp) / "c.js").write_text(_component_js(), encoding="utf-8")
        (Path(tmp) / "in.json").write_text(json.dumps({
            "data": data, "seed": seed, "period": period, "script": script, "storage": storage or {}, "frameW": frameW,
            "warm": warm, "acm": str(ACM_JS) if acm else "", "noinit": noinit, "ownWarm": ownWarm}), encoding="utf-8")
        done = subprocess.run([shutil.which("node"), "h.js", "c.js", "in.json"], cwd=tmp, capture_output=True, text=True,
                              encoding="utf-8", timeout=90)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


OPEN_ALL = "inst.state.acctOpen = {Acc: true, Other: true, Fresh: true}; inst.render();"


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))


# ───────────────────────── дерево: строка аккаунта — родитель ─────────────────────────

def test_accounts_start_collapsed_and_opening_reads_nothing():
    out = _node("""
      out.child = Object.fromEntries(Object.keys(H).map(a => [a, H[a].child.hidden]));
      out.tables = Object.fromEntries(Object.keys(H).map(a => [a, H[a].table.innerHTML]));
      out.aria = Object.fromEntries(Object.keys(H).map(a => [a, H[a].row.attrs['aria-expanded']]));
      H.Acc.row.listeners.click[0]({target: T({})}); await tick(20);
      out.after = {child: H.Acc.child.hidden, aria: H.Acc.row.attrs['aria-expanded'], open: H.Acc.row.cls.has('amap-open')};
      out.posts = calls.filter(c => c.startsWith('POST'));
    """)
    assert out["child"] == {"Acc": True, "Other": True, "Fresh": True} and set(out["tables"].values()) == {""}
    assert set(out["aria"].values()) == {"false"}
    assert out["after"] == {"child": False, "aria": "true", "open": True}
    assert out["posts"] == []                                                       # раскрытие ничего не читает


def test_clicking_the_row_expands_only_its_own_campaigns():
    out = _node("""
      H.Acc.row.listeners.click[0]({target: T({})}); await tick(10);
      out.acc = H.Acc.table.innerHTML; out.other = H.Other.table.innerHTML;
      H.Other.row.listeners.click[0]({target: T({})}); await tick(10);
      out.other2 = H.Other.table.innerHTML; out.acc2 = H.Acc.table.innerHTML;
      H.Acc.row.listeners.click[0]({target: T({})}); await tick(10);          // второй клик сворачивает
      out.closed = H.Acc.child.hidden;
    """)
    assert "CL - Mercado Libre - Evergreen" in out["acc"] and "Other account campaign" not in out["acc"]
    assert out["other"] == ""                                                       # чужой аккаунт не раскрылся
    assert "Other account campaign" in out["other2"] and "Evergreen" not in out["other2"]
    assert out["closed"] is True
    for html in (out["acc"], out["other2"]):
        assert "hc-band" not in html and "data-read=" not in html                   # полос аккаунтов внутри таблицы нет


def test_clicks_on_own_buttons_inputs_and_the_note_do_not_expand_but_the_chevron_does():
    out = _node("""
      const click = (map) => { H.Acc.row.listeners.click[0]({target: T(map)}); return H.Acc.child.hidden; };
      out.button = click(BTN);                       // кнопка, ссылка, поле ввода, заметка: closest находит не стрелку
      out.caret = click(CARET);                      // стрелка раскрывает
      out.caret2 = click(CARET);                     // и сворачивает
    """)
    assert out["button"] is True and out["caret"] is False and out["caret2"] is True


def test_the_row_is_keyboard_accessible_with_enter_and_space():
    out = _node("""
      const key = (k, target) => { const e = {key: k, target: target, preventDefault() { this.stopped = true; }}; H.Acc.row.listeners.keydown[0](e); return [H.Acc.child.hidden, !!e.stopped]; };
      out.enter = key('Enter', H.Acc.row);
      out.space = key(' ', H.Acc.row);
      out.other = key('a', H.Acc.row);
      out.fromButton = key('Enter', H.Acc.readBtn);   // Enter на вложенной кнопке — это её нажатие, не раскрытие
      out.aria = H.Acc.row.attrs['aria-expanded'];
    """)
    assert out["enter"] == [False, True] and out["space"] == [True, True]            # открыла, закрыла; прокрутку страницы Space гасит
    assert out["other"] == [True, False] and out["fromButton"] == [True, False] and out["aria"] == "false"


def test_the_unread_account_says_so_inside_its_section_and_has_no_header():
    out = _node("""
      H.Fresh.row.listeners.click[0]({target: T({})}); await tick(10); out.fresh = H.Fresh.table.innerHTML;
      H.Acc.row.listeners.click[0]({target: T({})}); await tick(10); out.acc = H.Acc.table.innerHTML;
    """)
    assert "Not read yet — press Read campaigns" in out["fresh"] and "hc-heads" not in out["fresh"]
    assert "hc-heads" in out["acc"] and 'class="hc-groups"' in out["acc"]             # заголовок кампаний — первая строка раскрытого аккаунта


# ───────────────────────── строка аккаунта несёт своё ─────────────────────────

def test_the_account_row_carries_the_snapshot_line_and_its_own_read_button():
    out = _node("""
      out.snap = Object.fromEntries(Object.keys(H).map(a => [a, H[a].snap.textContent]));
      out.btn = Object.fromEntries(Object.keys(H).map(a => [a, [H[a].readBtn.textContent, H[a].readBtn.disabled]]));
      H.Fresh.readBtn.listeners.click[0](); await tick(80);
      out.posts = posts.slice();
    """)
    assert out["snap"]["Acc"].startswith("Campaigns: 3 campaigns") and "read" in out["snap"]["Acc"]
    assert out["snap"]["Fresh"] == "Campaigns: not read yet"
    assert all(v == ["Read campaigns", False] for v in out["btn"].values())
    assert out["posts"] == ["Fresh"]                                                  # нажатие читает именно этот аккаунт


def test_a_read_failure_is_one_line_on_the_row_and_the_details_row_inside():
    out = _node("""
      const raw = "the profile could not be opened with automation: HTTPSConnectionPool(host='launcher.mlx.yt', port=45001): [WinError 10061] refused";
      inst.state.summaries.Acc = Object.assign({}, inst.state.summaries.Acc, {attempt: {state: 'failed', at: '2026-10-12 08:54:22', error: raw, code: ''}});
      inst.state.acctOpen.Acc = true; inst.render();
      out.snap = [H.Acc.snap.textContent, H.Acc.snap.dataset.tone, H.Acc.snap.title];
      out.row = /data-msg="acct:Acc:read".*?<\\/tr>/s.exec(H.Acc.table.innerHTML)[0];
    """)
    assert out["snap"][0] == "Campaigns: last read failed — Multilogin isn't running — start it and try again" and out["snap"][1] == "bad"
    assert "WinError" in out["snap"][2] and "WinError" not in out["snap"][0]
    assert "The last read failed (2026-10-12 08:54:22)" in out["row"] and 'data-details="acct:Acc:read"' in out["row"] and "WinError" not in out["row"]


def test_the_period_gate_blocks_reads():
    out = _node("out.dis = H.Acc.readBtn.disabled; out.title = H.Acc.readBtn.title; await inst.readAll(); out.posts = posts.slice();", period_ok=False)
    assert out["dis"] is True and "Select dates" in out["title"] and out["posts"] == []


def test_reading_a_country_queues_one_account_at_a_time_and_posts_only_there():
    out = _node("await inst.readAll(); out.posts = posts.slice(); out.busy = inst.state.busy; out.reads = calls.filter(c => c.startsWith('POST'));")
    assert out["posts"] == ["Acc", "Other", "Fresh"] and out["busy"] is False
    assert out["reads"] == ["POST /campaign-inventory/read"] * 3


# ───────────────────────── колонки и данные (как в макете) ─────────────────────────

def _acc_table():
    return _node(OPEN_ALL + " out.acc = H.Acc.table.innerHTML; out.other = H.Other.table.innerHTML; out.count = els.count.textContent;"
                 " inst.state.acctOpen = {}; out.collapsedCount = els.count.textContent;")


def test_toolbar_and_count_line_are_compact_and_the_template_has_no_separate_table_frame():
    tpl = _render_table()
    for needle in ("Search campaigns, ad sets or ads", "All delivery", "Enabled campaigns", "Paused campaigns", ">Controls<", ">BI metrics<",
                   ">Meta metrics<", ">Tracking<", "Expand all", "Collapse all", "Switch on", "Set bid…", "What was changed here"):
        assert needle in tpl, needle
    assert 'data-hc="frame"' not in tpl and 'data-hc="table"' not in tpl and "Identity stays pinned" not in tpl     # отдельной таблицы нет
    assert _acc_table()["count"] == "4 campaigns"                                                                  # Acc: 3, Other: 1


def test_column_groups_and_order_follow_the_prototype():
    acc = _acc_table()["acc"]
    names = [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<th[^>]*>(.*?)</th>", acc)]
    at = names.index("Delivery")
    assert names[at: at + 9] == ["Delivery", "Duplicate", "Bid control", "Budget", "Optimization hint", "Alerts", "Rules", "Goal CPA", "Benchmark CPA"]
    assert names[at + 9: at + 12] == ["Spend", "Leads", "FTDs"]
    assert "MANAGEMENT &amp; DECISIONS" in acc or "MANAGEMENT & DECISIONS" in acc
    assert "BI · business outcomes" in acc and "Account / campaign hierarchy" in acc and "Campaign / Ad set / Ad" in acc
    allcols = _node("inst.state.acctOpen = {Acc: true}; els.preset.value = 'all'; els.preset.listeners.change[0].call(els.preset); out.t = H.Acc.table.innerHTML;")["t"]
    flat = [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<th[^>]*>(.*?)</th>", allcols)]
    for h in ("Deposits (all)", "CR lead→FTD", "Keitaro", "Placements", "Devices", "Landing Variant", "History"):
        assert h in flat, h


def test_rows_pin_identity_and_show_level_country_account():
    acc = _acc_table()["acc"]
    assert 'data-sel="campaign:1"' in acc and 'data-exp="campaign:1"' in acc and 'class="hc-switch"' in acc
    assert "Chile · Acc" in _text(acc) and 'class="p3"' in acc


def test_goal_benchmark_and_hint_use_real_inputs_or_say_why_not():
    t = _acc_table()
    row = lambda html, key: re.search(r'<tr class="hc-r-campaign[^"]*" data-key="%s".*?</tr>' % key, html, re.S).group(0)
    ever = _text(row(t["acc"], "campaign:1"))
    assert "$500.00" in ever and "$650.00" in ever and "Within CPA goal" in ever and "6 FTDs" in ever and "$1,990.00" in ever
    assert "Spend above goal CPA, no FTDs" in _text(row(t["acc"], "campaign:3")) and "alert" in row(t["acc"], "campaign:3")
    assert "Switched off — nothing to advise" in row(t["acc"], "campaign:2")
    other = row(t["other"], "campaign:9")
    assert "No CPA goal is set for Chile in My Goals" in other and "No benchmark CPA is set for Chile" in other
    assert "No alerts from the data on screen" in other


def test_every_command_is_disabled_with_a_reason_without_the_helper():
    t = _node(OPEN_ALL + " out.t = H.Acc.table.innerHTML;", acm=False)["t"]
    assert 'class="hc-cmd hc-cmd-txt" disabled aria-disabled="true"' in t and 'title="ACL-1773"' in t
    assert re.search(r'class="hc-switch"[^>]*disabled aria-disabled="true"', t)
    assert "Commands unavailable: the command helper did not load" in t and "Coming in ACL-1800" not in t


def test_budget_and_bid_columns_show_the_value_and_the_owner_note():
    t = _node("""
      H.Acc.row.listeners.click[0]({target: T({})}); await tick(10);
      els['expand-all'].listeners.click[0](); await tick(80);
      out.t = H.Acc.table.innerHTML;
    """)["t"]
    text = _text(t)
    assert "$150.00 / day" in text and "$75.00 / day" in text and "Ad set budget" in text and "Campaign budget" in text
    assert "Ad set bids" in text and "$1.50" in text
    assert "Ads have no budget" in t and "Managed by ad set" in t


def test_delivery_filter_and_search_narrow_the_rows_of_every_account():
    out = _node(OPEN_ALL + """
      els.delivery.value = 'paused'; els.delivery.listeners.change[0].call(els.delivery);
      out.paused = H.Acc.table.innerHTML; out.pausedOther = H.Other.table.innerHTML; out.count = els.count.textContent;
      els.delivery.value = 'all'; els.delivery.listeners.change[0].call(els.delivery);
      els.search.value = 'spend with'; els.search.listeners.input[0].call(els.search);
      out.search = H.Acc.table.innerHTML;
    """)
    assert 'data-key="campaign:2"' in out["paused"] and 'data-key="campaign:1"' not in out["paused"] and "of" in out["count"]
    assert 'data-key="campaign:9"' in out["pausedOther"] or "campaign:9" not in out["pausedOther"]
    assert 'data-key="campaign:3"' in out["search"] and 'data-key="campaign:1"' not in out["search"]


def test_expand_all_opens_every_account_and_reads_children_from_the_local_snapshot_only():
    out = _node("""
      const before = calls.length;
      els['expand-all'].listeners.click[0](); await tick(120);
      out.open = Object.keys(H).filter(a => !H[a].child.hidden);
      out.acc = H.Acc.table.innerHTML; out.calls = calls.slice(before);
      els['collapse-all'].listeners.click[0](); await tick(5);
      out.afterCollapse = Object.keys(H).filter(a => !H[a].child.hidden);
      out.collapsedTable = H.Acc.child.hidden;
    """)
    assert out["open"] == ["Acc", "Other", "Fresh"]
    assert 'data-key="adset:11"' in out["acc"] and 'data-key="adset:12"' in out["acc"] and 'data-key="ad:111"' in out["acc"]
    kids = [c for c in out["calls"] if "/campaign-inventory/children" in c]
    assert kids and not [c for c in out["calls"] if c.startswith("POST")]
    assert out["afterCollapse"] == []


# ───────────────────────── общая прокрутка и ширина закреплённого блока ─────────────────────────

def test_horizontal_scroll_is_shared_between_the_open_accounts():
    out = _node(OPEN_ALL + """
      H.Acc.scroll.scrollLeft = 300; H.Acc.scroll.listeners.scroll[0].call(H.Acc.scroll);
      out.mirror = [H.Other.scroll.scrollLeft, H.Other.top.scrollLeft, H.Acc.top.scrollLeft];
      H.Other.top.scrollLeft = 120; H.Other.top.listeners.scroll[0].call(H.Other.top);
      out.fromTop = [H.Acc.scroll.scrollLeft, H.Fresh.scroll.scrollLeft];
      inst.state.acctOpen.Other = false; inst.render(); inst.state.acctOpen.Other = true; inst.render();
      out.reopened = H.Other.scroll.scrollLeft;                                   // заново открытый встаёт на общую позицию
      out.more = [H.Acc.frame.dataset.moreLeft, H.Acc.frame.dataset.moreRight];
    """)
    assert out["mirror"] == [300, 300, 300] and out["fromTop"] == [120, 120] and out["reopened"] == 120
    assert out["more"] == ["1", "1"]                                                # тени-подсказки — по краям рамки, а не посередине


def test_the_pinned_column_resizes_for_every_open_account_by_keyboard_mouse_and_double_click():
    out = _node(OPEN_ALL + """
      const handle = {closest: sel => (sel === '.hc-resize' ? handle : null)};
      const key = (k, extra) => Object.assign({key: k, target: handle, preventDefault() { this.stopped = true; }}, extra || {});
      const R2 = mkRoot(); const b = mount(R2); await b.init();
      out.initial = {w: inst.pinWidth(), cols: colw(H.Acc.table.innerHTML), stored: store['acl.hc.pinw.v1'] === undefined};
      const hm = /class="hc-resize" role="separator" aria-orientation="vertical" tabindex="0" aria-label="[^"]+" aria-valuemin="240" aria-valuemax="(\\d+)" aria-valuenow="(\\d+)"/.exec(H.Acc.table.innerHTML);
      out.handle = hm ? [hm[1], hm[2]] : null;
      const k1 = key('ArrowRight'); H.Acc.table.listeners.keydown[0](k1);
      out.right = {w: inst.pinWidth(), stored: store['acl.hc.pinw.v1'], prevented: !!k1.stopped, other: b.pinWidth(), var: R1.vars['--hc-pin-w']};
      H.Acc.table.listeners.keydown[0](key('ArrowLeft', {shiftKey: true})); out.leftShift = inst.pinWidth();
      H.Acc.table.listeners.keydown[0](key('Home')); out.home = inst.pinWidth();
      H.Acc.table.listeners.keydown[0](key('End')); out.end = inst.pinWidth();
      H.Acc.table.listeners.keydown[0]({key: 'a', target: handle, preventDefault() { throw new Error('other keys must pass'); }});
      inst.setPin(432, true);
      const md = {target: handle, button: 0, clientX: 500, preventDefault() {}};
      H.Other.table.listeners.mousedown[0](md);                                      // тянуть можно за ручку любой раскрытой таблицы
      docL.mousemove({clientX: 620}); out.dragLive = {w: inst.pinWidth(), stored: store['acl.hc.pinw.v1'], hasUp: typeof docL.mouseup};
      docL.mouseup({}); out.dragDone = {w: inst.pinWidth(), stored: store['acl.hc.pinw.v1'], moveGone: docL.mousemove === undefined, other: b.pinWidth()};
      inst.render(); out.colsAcc = colw(H.Acc.table.innerHTML); out.colsOther = colw(H.Other.table.innerHTML);
      H.Acc.table.listeners.mousedown[0](md); docL.mousemove({clientX: -5000}); docL.mouseup({}); out.dragMin = inst.pinWidth();
      H.Acc.table.listeners.mousedown[0](md); docL.mousemove({clientX: 99999}); docL.mouseup({}); out.dragMax = inst.pinWidth();
      H.Acc.table.listeners.dblclick[0]({target: handle}); out.reset = {w: inst.pinWidth(), stored: store['acl.hc.pinw.v1'], other: b.pinWidth()};
      H.Acc.table.listeners.mousedown[0]({target: {closest: () => null}, button: 0, clientX: 1, preventDefault() { throw new Error('not the handle'); }});
      for (const a of Object.keys(H)) H[a].frame.clientWidth = 700;
      inst.setPin(2000, false); out.narrow = inst.pinWidth();
      for (const a of Object.keys(H)) H[a].frame.clientWidth = 0;
      inst.setPin(2000, false); out.hidden = inst.pinWidth();
    """)
    assert out["initial"]["w"] == 432 and out["initial"]["cols"][:4] == [36, 28, 88, 280] and out["initial"]["stored"] is True
    assert out["handle"] == ["840", "432"]
    right = out["right"]
    assert right["w"] == 448 and right["stored"] == "448" and right["prevented"] is True and right["other"] == 448 and right["var"] == "448px"
    assert out["leftShift"] == 384 and out["home"] == 240 and out["end"] == 840
    assert out["dragLive"] == {"w": 552, "stored": "432", "hasUp": "function"}
    assert out["dragDone"] == {"w": 552, "stored": "552", "moveGone": True, "other": 552}
    assert out["colsAcc"][:4] == out["colsOther"][:4] == [36, 28, 88, 552 - 152]    # шапка и все аккаунты — одни и те же <col>
    assert out["dragMin"] == 240 and out["dragMax"] == 840
    assert out["reset"] == {"w": 432, "stored": "432", "other": 432}
    assert out["narrow"] == 420 and out["hidden"] == 900


def test_the_saved_width_is_used_and_garbage_is_ignored():
    assert _node("out.w = inst.pinWidth();", storage={"acl.hc.pinw.v1": "600"})["w"] == 600
    for bad in ("abc", "100", "99999", ""):
        assert _node("out.w = inst.pinWidth();", storage={"acl.hc.pinw.v1": bad})["w"] == 432, bad
    assert _node(OPEN_ALL + " out.w = inst.pinWidth();", storage={"acl.hc.pinw.v1": "900"}, frameW=1000)["w"] == 600


def test_names_wrap_to_two_lines_and_keep_the_full_name_in_the_tooltip():
    css = TABLE[: TABLE.index("</style>")]
    clamp = css[css.index(".hc-name b {"): css.index(".hc-name span")]
    assert "-webkit-line-clamp: 2" in clamp and "white-space: normal" in clamp and ".hc-table td.p3 { white-space: normal; }" in css
    acc = _acc_table()["acc"]
    assert 'class="p3"><div class="hc-id"><span class="hc-badge">Campaign</span><span class="hc-name"><b title="CL - Mercado Libre - Evergreen (id ' in acc
    assert 'class="hc-resize" role="separator"' in acc and 'aria-label="Resize the name column' in acc


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
HAND_NEW = "Profile is open in Multilogin by hand — close it there, wait about 3 minutes, then press Open profile"
LOOP = "Campaign commands are only available when the dashboard listens on this computer alone (127.0.0.1). It is bound to a network address."
RPA = "Campaign commands need the Graph API backend (ACL_FB_BACKEND=api). This machine uses the RPA backend, so nothing was changed."
MLX = "Multilogin isn't running — start it and try again"


@pytest.mark.parametrize("text,code,want", [
    (LAUNCHER, "", MLX),
    ("HTTPSConnectionPool(host='127.0.0.1', port=45001): refused", "", MLX),
    ("[WinError 10061] No connection could be made", "", MLX),
    ("ProfileError: " + HAND, "open_by_hand", HAND_NEW),
    (HAND, "", HAND_NEW),
    ("Opened in Multilogin by hand — close it there", "no_port", HAND_NEW),
    ("the profile is closing or starting; retry in a minute. Nothing was stopped.", "profile_closing", "Profile is closing — try again in a few seconds"),
    ("ProfileError: closing", "profile_closing", "Profile is closing — try again in a few seconds"),
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
        assert ex["raw"] == text                                                                   # сырой текст — в Details


def test_unknown_errors_say_something_went_wrong_and_keep_the_raw_text():
    (f, ex), (f2, ex2), (f3, ex3) = _map([["Not sent: kaboom", "", "bad"], ["Failed — previous state kept: Meta refused", "", "bad"], ["Confirmed", "", "ok"]])
    assert f is None and ex["text"] == "Something went wrong — see Details" and ex["raw"] == "Not sent: kaboom"
    assert ex2["text"] == "Failed — previous state kept: Meta refused" and ex2["raw"] is None
    assert ex3 == {"text": "Confirmed", "raw": None, "tone": "ok"}
    long = "Failed — previous state kept: " + "x" * 200
    (_, ex4), = _map([[long, "", "bad"]])
    assert ex4["text"] == "Failed — previous state kept" and ex4["raw"] == long


RAW = ("Not sent: Facebook_5414_x the profile could not be opened with automation: HTTPSConnectionPool(host='launcher.mlx.yt', port=45001): "
       "Max retries exceeded (Caused by NewConnectionError: [WinError 10061] No connection could be made) <b>x</b>")


def test_the_message_is_a_full_width_sub_row_under_its_row_not_a_status_cell_note():
    out = _node(OPEN_ALL + f"""
      inst.state.notes['campaign:1'] = win.acmCommands.explain({json.dumps(RAW)}, 'bad', '');
      inst.state.notes['campaign:3'] = win.acmCommands.explain('Confirmed', 'ok');
      inst.render();
      const html = H.Acc.table.innerHTML;
      out.status = (/<td class="p2">.*?<\\/td>/s.exec(rowOf(html, 'campaign:1')[0]) || [''])[0];
      out.sub = /<tr class="hc-rowmsg[^"]*" data-msg="row:campaign:1".*?<\\/tr>/s.exec(html)[0];
      out.order = html.indexOf('data-key="campaign:1"') < html.indexOf('data-msg="row:campaign:1"');
      out.ok = /data-msg="row:campaign:3"[^>]*>.*?<\\/tr>/s.exec(html)[0];
      const click = id => H.Acc.table.listeners.click[0]({{target: {{closest: sel => (/data-details/.test(sel) ? {{dataset: {{details: id}}}} : null)}}}});
      click('row:campaign:1'); out.opened = /data-msg="row:campaign:1".*?<\\/tr>/s.exec(H.Acc.table.innerHTML)[0];
      click('row:campaign:1'); out.closed = /data-msg="row:campaign:1".*?<\\/tr>/s.exec(H.Acc.table.innerHTML)[0];
    """)
    assert "hc-mk bad" in out["status"] and "WinError" not in out["status"] and "HTTPSConnectionPool" not in out["status"]
    assert out["order"] is True
    assert int(re.search(r'colspan="(\d+)"', out["sub"]).group(1)) > 4
    assert "Multilogin isn&#39;t running — start it and try again" in out["sub"] and "WinError" not in out["sub"]
    assert 'class="hc-rm-btn" data-details="row:campaign:1" aria-expanded="false">Details' in out["sub"] and "hc-rm-raw" not in out["sub"]
    assert 'class="hc-rm-btn"' not in out["ok"] and "Confirmed" in out["ok"]
    assert 'class="hc-rm-raw"' in out["opened"] and "WinError 10061" in out["opened"] and "&lt;b&gt;x&lt;/b&gt;" in out["opened"] and "<b>x</b>" not in out["opened"]
    assert "hc-rm-raw" not in out["closed"]
    css = TABLE[: TABLE.index("</style>")]
    assert ".hc-rm-text { min-width: 0; flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" in css and "user-select: text" in css


def test_the_mapping_lives_in_one_place():
    assert TABLE.count("launcher.mlx.yt") == 0 and "Multilogin isn't running" not in TABLE
    assert "Multilogin isn't running" in ACM_JS.read_text(encoding="utf-8")


# ───────────────────────── профиль в строке аккаунта (ACL-1812) и без повторов ─────────────────────────

WARM = {"Acc": {"state": "closed", "text": "Closed", "title": "Not running", "closable": False},
        "Other": {"state": "by_hand", "text": "Opened by hand", "title": "Opened in Multilogin by hand", "closable": False},
        "Fresh": {"state": "warm", "text": "Warm · since 09:15", "title": "Open with automation", "closable": True}}

STATE = "Object.fromEntries(Object.keys(H).map(a => [a, H[a].chip ? [H[a].chip.hidden, H[a].chip.textContent, H[a].chip.dataset.state] : null]))"
BTNS = "Object.fromEntries(Object.keys(H).map(a => [a, H[a].openBtn ? [H[a].openBtn.hidden, H[a].openBtn.disabled, H[a].openBtn.title] : null]))"


def test_without_the_warm_api_there_is_no_chip_and_no_button():
    out = _node(f"out.chip = {STATE}; out.btn = {BTNS};")
    assert all(v[0] is True for v in out["chip"].values()) and all(v[0] is True for v in out["btn"].values())       # маршрутов нет (404) — всё скрыто


def test_chip_and_open_button_follow_the_profile_state():
    out = _node(f"out.chip = {STATE}; out.btn = {BTNS}; out.calls = warmCalls.slice();", warm={"accounts": WARM, "results": {}})
    assert any(c.startswith("GET /warm/state?account=Acc&account=Other&account=Fresh") for c in out["calls"])
    assert out["chip"]["Acc"] == [False, "Closed", "closed"] and out["chip"]["Other"] == [False, "Opened by hand", "by_hand"]
    assert out["chip"]["Fresh"] == [False, "Warm · since 09:15", "warm"]
    assert out["btn"]["Acc"][:2] == [False, False]                                  # закрыт — кнопка доступна
    assert out["btn"]["Other"][:2] == [False, True] and out["btn"]["Other"][2] == "Close it in Multilogin first"
    assert out["btn"]["Fresh"][0] is True                                            # тёплый — открывать нечего
    assert not [c for c in out["calls"] if c.startswith("POST")]                    # само по себе ничего не открывается


def test_the_row_does_not_draw_its_own_chip_when_the_1812_script_already_owns_it():
    out = _node("out.chip = H.Acc.chip; out.btn = H.Acc.openBtn;", ownWarm=False, warm={"accounts": WARM, "results": {}})
    assert out["chip"] is None and out["btn"] is None                                # страница не создала узлы — компонент их не трогает
    assert "window.__aclWarmSessions" in REPORT


def test_actions_off_disables_the_button_with_the_reason():
    out = _node(f"out.btn = {BTNS};", warm={"accounts": WARM, "results": {}, "actions": False})
    assert out["btn"]["Acc"][1] is True and out["btn"]["Acc"][2] == "Opening profiles needs the dashboard to listen on 127.0.0.1 only"


def test_pressing_open_profile_calls_the_warm_open_endpoint_and_then_refreshes():
    out = _node("""
      H.Acc.openBtn.listeners.click[0]();
      out.busy = [H.Acc.openBtn.disabled, H.Acc.openBtn.textContent];
      await tick(80);
      out.calls = warmCalls.slice(); out.after = [H.Acc.openBtn.disabled, H.Acc.openBtn.textContent];
      inst.state.acctOpen.Acc = true; inst.render(); out.msg = /data-msg="acct:Acc:open"/.test(H.Acc.table.innerHTML);
    """, warm={"accounts": WARM, "results": {"Acc": {"state": "ok", "ok": True}}})
    assert out["busy"] == [True, "Opening…"]
    posts = [c for c in out["calls"] if c.startswith("POST")]
    assert posts == ['POST /warm/open {"accounts":["Acc"]}']                        # одно открытие, этого аккаунта
    assert "GET /warm/job/j1" in out["calls"] and sum(c.startswith("GET /warm/state") for c in out["calls"]) >= 2
    assert out["msg"] is False and out["after"][1] == "Open profile"


@pytest.mark.parametrize("code,text,want", [
    ("restart_cooldown", "Stopped a minute ago; it can be started again in about 3 min.", "Stopped a minute ago; it can be started again in about 3 min."),
    ("open_by_hand", "Opened in Multilogin by hand — close it there", HAND_NEW),
    ("profile_closing", "The profile is closing; try again in a minute. Nothing was started.", "Profile is closing — try again in a few seconds"),
])
def test_a_refused_open_shows_the_friendly_message_with_the_raw_text_in_details(code, text, want):
    res = {"Acc": {"state": "failed", "ok": False, "code": code, "message": text}}
    out = _node("""
      H.Acc.openBtn.listeners.click[0](); await tick(80);
      inst.state.acctOpen.Acc = true; inst.render();
      out.msg = /data-msg="acct:Acc:open".*?<\\/tr>/s.exec(H.Acc.table.innerHTML)[0];
    """, warm={"accounts": WARM, "results": res})
    assert want.replace("'", "&#39;") in out["msg"]


def _dedup(script_tail, **kw):
    return _node(OPEN_ALL + f"""
      inst.state.notes['campaign:1'] = win.acmCommands.explain({json.dumps(kw.pop('raw'))}, 'bad', {json.dumps(kw.pop('code', ''))});
      {kw.pop('attempt', '')}
      inst.render();
      out.row = (/data-msg="row:campaign:1".*?<\\/tr>/s.exec(H.Acc.table.innerHTML) || [null])[0];
    """ + script_tail, **kw)["row"]


def test_a_row_failure_with_the_cause_the_account_already_shows_is_one_short_line():
    hand = "Not sent: ProfileError: " + HAND
    chip = _dedup("", raw=hand, code="open_by_hand", warm={"accounts": {**WARM, "Acc": {"state": "by_hand", "text": "Opened by hand", "title": ""}}, "results": {}})
    assert "Profile problem — see account above" in chip and "Details" not in chip and "no automation port" not in chip
    attempt = ("inst.state.summaries.Acc = Object.assign({}, inst.state.summaries.Acc, {attempt: {state: 'failed', at: '2026-10-12 11:00:00', "
               "error: " + json.dumps(HAND) + ", code: 'open_by_hand'}});")
    read = _dedup("", raw=hand, code="open_by_hand", attempt=attempt, warm={"accounts": WARM, "results": {}})
    assert "Profile problem — see account above" in read and "Details" not in read
    other = _dedup("", raw=LAUNCHER, attempt=attempt, warm={"accounts": WARM, "results": {}})        # другая причина — полный текст
    assert "Multilogin isn&#39;t running" in other and "Details" in other
    alone = _dedup("", raw=hand, code="open_by_hand")                                                  # строке нечего показать — дубля нет
    assert "Profile is open in Multilogin by hand" in alone and "Details" in alone


# ───────────────────────── что убрано ─────────────────────────

def test_the_account_bands_and_the_separate_table_are_gone():
    script = TABLE[TABLE.index("<script>"):]
    assert "bandAccount" not in script and "'<tr class=\"hc-band " not in script and "data-read-country" not in script
    tpl = _render_table()
    assert 'data-hc="table"' not in tpl and 'data-hc="frame"' not in tpl and "Account / campaign hierarchy" in TABLE   # шапка кампаний — внутри раскрытой строки
    out = _node(OPEN_ALL + " out.html = Object.keys(H).map(a => H[a].table.innerHTML).join('');")["html"]
    assert 'class="hc-band ' not in out and "data-read=" not in out and "Read campaigns" not in _text(out).replace("press Read campaigns", "")
