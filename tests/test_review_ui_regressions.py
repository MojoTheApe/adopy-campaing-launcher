import asyncio
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from src.web import app
from src.analysis_agent.money_format import fmt_currency

ROOT = Path(__file__).resolve().parents[1]


def run_node(args):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute dashboard handlers")
    r = subprocess.run([node, *map(str, args)], text=True, capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


def test_search_reload_and_instance_race():
    run_node([Path(__file__).with_name("account_search_harness.cjs"), ROOT / "src/web/templates/report.html"])


class Handlers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers = []
    def handle_starttag(self, tag, attrs):
        self.handlers += [value for name, value in attrs if name == "onclick"]


def account_map(span="", live=None):
    name = "Côte d'Ivoire <&> \"test\""
    row = dict(account="Buyer's X01", has_data=False, status={}, workbook_alias="", elsewhere={}, assets=[], countries=[])
    country = dict(country=name, key=name, rows=[row], spend=0, leads=0, deposits=0)
    return dict(dates=[], countries=[country], available=[], all_accounts=[row["account"]], live=live or {}, date_from="2026-09-06", date_to="2026-09-06", span=span)


def test_rendered_inline_handlers_keep_quotes_and_argument_values(tmp_path):
    amap = account_map()
    goal = dict(country=amap["countries"][0]["country"], ftd=5, cpa=10, deposits=0, pct=0, code="CI")
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from copy import deepcopy
    from test_web_drill import _MOCK_REPORT
    report = deepcopy(_MOCK_REPORT)
    app._enrich_country_overview(report)
    rendered = app.templates.env.get_template("report.html").render(
        request=MagicMock(), report=report, warnings=[], built_at="test", upload_errors={},
        account_preflight={}, account_preflight_json={}, ref_options={}, ref_options_json={},
        account_alias_map_json={}, draft_status={}, mlx_labels={"A":"A", "B":"B"},
        goals=dict(rows=[goal], deposits=0, target=5, pct=0, cm="Test", team_lead=False), account_map=amap)
    # The same real map template is also returned as a fragment.
    rendered += app.templates.env.get_template("_account_map.html").render(amap=amap, mlx={"A":"A", "B":"B"})
    parser = Handlers(); parser.feed(rendered)
    wanted = [h for h in parser.handlers if any(fn + "(" in h for fn in ("goalMenu", "amapUnlink", "amapLink", "amapClose"))]
    assert len(wanted) >= 4
    source = "const assert=require('assert/strict'), vm=require('vm'); const calls=[];\n"
    from test_report_scripts_parse import _SCRIPT
    for block in _SCRIPT.findall(rendered):
        source += "new vm.Script(" + json.dumps(block) + ");\n"
    source += "const goalMenu=(...a)=>calls.push(['goal',a[1]]), amapUnlink=(...a)=>calls.push(['unlink',...a]), amapLink=(...a)=>calls.push(['link',...a]), amapClose=(...a)=>calls.push(['close',...a]);\n"
    source += "const event={preventDefault(){},stopPropagation(){}};\n"
    source += "const originatingElement={previousElementSibling:'selector'};\n"
    for h in wanted:
        source += "new Function('goalMenu','amapUnlink','amapLink','amapClose','event'," + json.dumps(h) + ").call(originatingElement,goalMenu,amapUnlink,amapLink,amapClose,event);\n"
    source += "assert.deepEqual(calls.find(c=>c[0]==='goal')[1],"+json.dumps(goal)+");\n"
    source += "const unlink=calls.find(c=>c[0]==='unlink').slice(1); assert.equal(unlink.length,4);\n"
    source += "assert.deepEqual(unlink.slice(0,3),"+json.dumps(["Buyer's X01", "country", goal["country"]])+");\n"
    source += "assert.strictEqual(unlink[3],originatingElement);\n"
    source += "assert.deepEqual(calls.find(c=>c[0]==='close').slice(1),"+json.dumps(["country", goal["country"],goal["country"]])+");\n"
    path=tmp_path / "handlers.cjs"; path.write_text(source, encoding="utf-8")
    run_node([path])


def test_maximum_does_not_read_yesterday_or_show_cached_spend(monkeypatch):
    from src.analysis_agent import account_spend
    def unexpected(*a, **k):
        pytest.fail("Maximum must not start a browser read")
    monkeypatch.setattr(account_spend, "start_read", unexpected)
    async def form(**_limits):
        return {"account": "Acc", "span": "all", "date_from": "", "date_to": ""}
    response=asyncio.run(app.account_spend_read(NS(form=form)))
    assert response.status_code == 400
    assert asyncio.run(app.account_spend_status(account="Acc", span="all")).status_code == 400
    assert app._live_spend("", "") == {}
    amap=account_map("all")
    rendered=app.templates.env.get_template("_account_map_country.html").render(mc=amap["countries"][0], amap=amap, mlx={"A":"A","B":"B"})
    assert 'disabled title="Select dates' in rendered
    assert 'onclick="amapReadSpend' not in rendered


@pytest.mark.parametrize("currency,expected", [("EUR","EUR 1,234.56"),("USD","USD 1,234.56"),("","currency unknown 1,234.56")])
def test_reload_keeps_currency_and_cents(currency, expected):
    assert fmt_currency(1234.56, currency) == expected
    amap=account_map(live={"Buyer's X01":dict(state="ok", spend=1234.56, currency=currency)})
    rendered=app.templates.env.get_template("_account_map_country.html").render(mc=amap["countries"][0], amap=amap, mlx={"A":"A","B":"B"})
    assert expected in rendered


def test_poll_resumes_and_updates_all_copies_and_status(tmp_path):
    template=(ROOT/"src/web/templates/report.html").read_text(encoding="utf-8")
    script=template[template.index('function amapMoney('):template.index('function amapSpendFailed(')]
    source=r'''
const assert=require('assert/strict'), vm=require('vm');
const callbacks={}; const requests=[]; const queue=[];
function cell() {
  const btn={disabled:true}, status={set innerHTML(v){this.firstChild={};}};
  const row={querySelector:s=>s==='.amap-read'?btn:status};
  return {dataset:{account:'Acc',running:'1'},closest:()=>row,btn,status,set innerHTML(v){this.firstChild={};}};
}
const cells=[cell(),cell()];
const ctx={window:{addEventListener(){}},document:{readyState:'loading',getElementById:id=>id==='acl-source-info'?{dataset:{sourceName:'the selected source data'}}:null,addEventListener:(e,fn)=>callbacks[e]=fn,querySelectorAll:s=>s.startsWith('.amap-live')?cells:[]},
  amapPeriod:()=>({from:'2026-09-06',to:'2026-09-06'}),
  setTimeout:fn=>queue.push(fn),
  fetch:async url=>{requests.push(url);return {json:async()=>({ok:true,reading:{state:'ok',spend:1234.56,currency:'EUR',status:'disabled',status_text:'Account disabled',finished_at:'now'}})};},
  amapSpendFailed:(btn)=>{btn.disabled=false;}};
vm.createContext(ctx);
'''
    source+="vm.runInContext("+json.dumps(script)+",ctx);\n"
    source+='''(async()=>{callbacks.DOMContentLoaded();assert.equal(queue.length,1); queue.shift()();await new Promise(setImmediate);
      assert.equal(requests.length,1); for(const c of cells){assert.equal(c.firstChild.textContent,'EUR 1,234.56');assert.equal(c.status.firstChild.textContent,'disabled');assert.equal(c.btn.disabled,false);assert.equal(c.dataset.running,'0');}
      ctx.amapApplyReading('Acc',{state:'failed',status:'clear',finished_at:'later',error:'test'});
      for(const c of cells) assert.equal(c.status.firstChild.textContent,'no warning');
    })().catch(e=>{console.error(e);process.exit(1);});
'''
    path=tmp_path/"poll.cjs";path.write_text(source,encoding="utf-8");run_node([path])
