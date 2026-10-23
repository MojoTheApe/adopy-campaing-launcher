"""ACL-1822: страница Rules без браузера - разметка шаблона, помощники страницы (в node), охрана бэкенда.

Apply проверяется на подмене `acmCommands`: страница обязана идти только через `acmCommands.run`.
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

STATIC = ROOT / "src" / "web" / "static"
PAGE = (ROOT / "src" / "web" / "templates" / "_rules_page.html").read_text(encoding="utf-8")
TABLE = (ROOT / "src" / "web" / "templates" / "_campaign_table.html").read_text(encoding="utf-8")
REPORT = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
HELPER = (STATIC / "rules_ui.js").read_text(encoding="utf-8")


def render(live: bool, seed=None) -> str:
    from src.web.app import templates

    return templates.env.get_template("_rules_page.html").render(
        campaign_table_api=lambda: live, rl_seed=seed if seed is not None else [])


# ───────────────────────── разметка ─────────────────────────

def test_rpa_shows_one_notice_and_no_page_machinery():
    html = render(False)
    assert "Rules need the API backend" in html and 'id="rl-rpa"' in html
    assert "rl-items" not in html and "<script" not in html and "rules_ui.js" not in html


def test_live_page_has_the_decision_flow_pieces_and_no_design_switcher():
    html = render(True, [{"country": "Mexico", "accounts": ["A <b>1</b>"]}])
    for needle in ("Your rules", "Create rule", "Templates", "Choose campaigns", "Evidence basis",
                   "Why would this rule act?", "Evaluate now", "Save rule", "Condition match is not execution permission",
                   "Rule settings", "Report period", "Cohort", "Lifetime"):
        assert needle in html, needle
    for banned in ("Coverage matrix", "Timeline", "Threshold map", "Portfolio", "EDITOR DESIGNS", "synthetic", "Try a scenario",
                   "DESIGN HANDOFF"):
        assert banned not in html, banned
    # данные посева - только как JSON внутри script: разметка из них не строится
    assert "<b>1</b>" not in html.split('id="rl-seed"')[0]
    assert "\\u003cb\\u003e1" in html or "&lt;b&gt;1" in html or "\\u003c" in html  # linux-ok: JSON Unicode escapes, not a filesystem path


def test_page_has_no_direct_command_writes_and_no_dynamic_sinks():
    assert "campaign-commands" not in PAGE, "the only write path is acmCommands.run"
    assert "campaign-commands" not in HELPER
    # одна запись ответов страницы в innerHTML - всё через esc(); сырого eval и document.write нет
    assert "eval(" not in PAGE and "document.write" not in PAGE
    assert "set_status" in HELPER and "acm.run" in HELPER
    assert not re.search(r"fetch\([^)]*campaign-commands", PAGE + HELPER)


def test_automatic_is_shown_disabled_with_the_reason():
    assert "Automatic comes with the rules executor (ACL rules T5) — needs owner settings" in HELPER
    assert "disabled aria-disabled=\"true\"" in PAGE and "m.disabled" in PAGE


def test_report_wires_the_nav_item_the_panel_and_the_hash():
    assert 'data-view="rules"' in REPORT and 'id="view-rules"' in REPORT
    assert '{% include "_rules_page.html" %}' in REPORT
    assert "'rules'].indexOf(hash.slice(1))" in REPORT
    # страница живёт в основной области, правый билдер не затрагивается
    assert REPORT.index('id="view-rules"') < REPORT.index('id="view-targets"')


def test_table_has_a_rules_column_that_opens_an_inspector():
    assert "k: 'rules'" in TABLE and "data-rules-open" in TABLE and "openRulesInspector" in TABLE
    assert "/rules/cells" in TABLE and "acmCommands" in TABLE
    assert "/campaign-commands/execute" not in TABLE.split("openRulesInspector")[1].split("function findRow")[0]
    assert '<script src="/static/rules_ui.js"></script>' in TABLE


def test_routes_exist_and_commands_routes_are_untouched():
    from src.web.app import app

    paths = {(m, r.path) for r in app.routes for m in getattr(r, "methods", None) or ()}
    for route in (("GET", "/rules/overview"), ("GET", "/rules/scope-options"), ("GET", "/rules/cells"),
                  ("POST", "/rules/save"), ("POST", "/rules/mode"), ("POST", "/rules/duplicate"),
                  ("POST", "/rules/dismiss"), ("POST", "/rules/action-result"), ("POST", "/rules/evaluate-now")):
        assert route in paths, route


# ───────────────────────── помощники (node) ─────────────────────────

NODE = shutil.which("node")
CASES = r"""
const R = require(process.argv[2]);
const out = {};
const rule = (over) => Object.assign({ id: 'r1', mode: 'suggest', counts: { campaign: 2, adset: 3, ad: 0, total: 5 }, last_eval: null }, over || {});
const lastEval = (o) => ({ at: '2026-10-12T10:00:00+00:00', objects: [o], T: { value: 60 }, reasons: o.reasons || [] });

// экранирование
out.esc = R.esc('<img src=x onerror="a()">&\'');
out.identity = [R.identityText('duplicate_name'), R.identityText('exact_by_children'), R.identityText(null), R.identityText('weird')];

// предложение
const pause = { action_id: 'act_1', account: 'Acc', level: 'adset', id: '120000000000001', kind: 'pause', requested: { status: 'PAUSED' }, before: { status: 'ACTIVE' } };
const scale = { action_id: 'act_2', account: 'Acc', level: 'campaign', id: '120000000000002', kind: 'budget_up', requested: { daily_budget: 110 }, before: { daily_budget: 100 } };
out.reqPause = R.proposalRequest(pause);
out.reqScale = R.proposalRequest(scale);
out.reqAlert = R.proposalRequest({ account: 'Acc', level: 'adset', id: '1', kind: 'alert' });
out.reqEnable = R.proposalRequest({ account: 'Acc', level: 'adset', id: '1', kind: 'enable', requested: { status: 'ACTIVE' } });
out.text = [R.proposalText(pause), R.proposalText(scale)];

// условия
out.sentence = R.conditionSentence([{ op: 'AND', items: [{ metric: 'spend', cmp: '>=', value: 3, unit: 't_multiple' }, { metric: 'ftd', cmp: '==', value: 0, unit: 'abs' }] }], 60);
out.sentenceNoT = R.conditionSentence([{ metric: 'cpa_block', cmp: '<=', value: 0.8, unit: 't_multiple' }], null);
out.sentenceEmpty = R.conditionSentence([], 1);

// карточки
const matched = { key: 'adset:1', verdict: 'matched', conditions_matched: true, identity: 'exact', reasons: [], notes: [], observed: { spend: 620, ftd: 0, target: 60 }, action: { kind: 'pause' } };
out.cardsMatched = R.cards(rule({ last_eval: lastEval(matched) }), matched);
out.cardsObserve = R.cards(rule({ mode: 'observe', last_eval: lastEval(matched) }), matched)[3];
const amb = { key: 'adset:1', verdict: 'blocked', conditions_matched: null, identity: 'duplicate_name', reasons: ['Ambiguous mapping'], notes: ['2 ad sets named X'], observed: {}, action: null };
out.cardsAmbiguous = R.cards(rule({ last_eval: lastEval(amb) }), amb);
const noT = { key: 'adset:1', verdict: 'blocked', conditions_matched: null, identity: 'exact', reasons: ['No target'], notes: ['No target for Mexico'], observed: {}, action: null };
out.cardsNoTarget = R.cards(rule({ last_eval: lastEval(noT) }), noT)[1];
out.cardsStale = R.cards(rule({ last_eval: lastEval(Object.assign({}, amb, { reasons: ['Stale BI'], notes: [] })) }), Object.assign({}, amb, { reasons: ['Stale BI'], notes: [] }))[1];
out.cardsNever = R.cards(rule(), null);
out.cardsError = R.cards(rule({ last_eval: { error: 'the stats workbook is not available' } }), null)[1];
out.cardsEmptyScope = R.cards(rule({ counts: { campaign: 0, adset: 0, ad: 0, total: 0 } }), null)[0];
out.counters = R.counters(rule({ last_eval: { objects: [matched, amb, Object.assign({}, matched, { conditions_matched: false, verdict: 'not_matched', action: null })] } }));
out.countersObserve = R.counters(rule({ mode: 'observe', last_eval: { objects: [matched] } }));

// Apply: ТОЛЬКО через acmCommands.run, затем сообщаем op_id журналу правил
async function apply(p, runResult, fail) {
  const calls = { run: [], fetch: [] };
  const deps = {
    acm: { run: (req) => { calls.run.push(req); return fail ? Promise.reject(new Error('Not sent: boom')) : Promise.resolve(runResult); } },
    fetch: (url, opts) => { calls.fetch.push({ url, body: JSON.parse(opts.body) }); return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, state: 'verified' }) }); }
  };
  let res = null, err = '';
  try { res = await R.applyProposal(p, 'Buyer One', deps); } catch (e) { err = e.message; }
  return { calls, res, err };
}
out.applyOk = await apply(pause, { state: 'Succeeded', op_id: 'op_9' });
out.applyScale = await apply(scale, { state: 'Succeeded', op_id: 'op_10' });
out.applyCancel = await apply(pause, { state: 'Cancelled', cancelled: true });
out.applyFail = await apply(pause, null, true);
out.applyAlert = await apply({ account: 'Acc', level: 'adset', id: '1', kind: 'alert', action_id: 'a' }, null);
const d = { fetch: (url, opts) => { out.dismissCall = { url, body: JSON.parse(opts.body) }; return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) }); } };
await R.dismissProposal(pause, 'Buyer One', d);
const bad = { fetch: () => Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ ok: false, error: 'already dismissed', code: 'not_pending' }) }) };
try { await R.dismissProposal(pause, 'Buyer One', bad); out.dismissBad = 'no error'; } catch (e) { out.dismissBad = e.message; }
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def out():
    if not NODE:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "case.js"
        path.write_text("(async () => {" + CASES + "})().catch(e => { console.error(e); process.exit(1); });", encoding="utf-8")
        done = subprocess.run([NODE, str(path), str(STATIC / "rules_ui.js")], capture_output=True, text=True,
                              timeout=60, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_text_is_escaped_and_identity_has_plain_words(out):
    assert "<" not in out["esc"] and "&lt;img" in out["esc"] and "&quot;" in out["esc"] and "&#39;" in out["esc"]
    assert out["identity"] == ["Ambiguous: duplicate name", "By ad sets", "Not checked", "weird"]


def test_only_pause_and_scale_become_commands(out):
    assert out["reqPause"] == {"account": "Acc", "command": "set_status", "value": "PAUSED",
                               "targets": [{"level": "adset", "id": "120000000000001"}]}
    assert out["reqScale"]["command"] == "set_budget" and out["reqScale"]["value"] == "110"
    assert out["reqAlert"] is None and out["reqEnable"] is None
    assert out["text"] == ["Pause the ad set", "Daily budget 100 → 110"]


def test_condition_sentence_shows_units_and_the_target(out):
    assert out["sentence"] == "Spend ≥ 3× T ($180.00) AND FTDs = 0"
    assert out["sentenceNoT"] == "CPA, worst block ≤ 0.8× T (no target)"
    assert out["sentenceEmpty"] == "No conditions yet"


def test_four_cards_follow_the_latest_evaluation(out):
    names = [c["name"] for c in out["cardsMatched"]]
    assert names == ["Scope", "Evidence", "Conditions", "Action"]
    scope, evidence, conditions, action = out["cardsMatched"]
    assert scope["title"] == "2 campaigns" and "3 ad sets" in scope["sub"]
    assert evidence["title"] == "Available" and conditions["title"] == "Matched" and action["title"] == "Ready"
    assert out["cardsObserve"]["title"] == "Observe only"
    assert out["cardsAmbiguous"][1]["title"] == "Ambiguous mapping" and out["cardsAmbiguous"][3]["title"] == "Held"
    assert out["cardsNoTarget"]["title"] == "No target" and out["cardsStale"]["title"] == "Stale BI"
    assert out["cardsNever"][1]["title"] == "Not evaluated yet" and out["cardsNever"][2]["title"] == "—"
    assert out["cardsError"]["title"] == "Evaluation failed" and out["cardsError"]["tone"] == "bad"
    assert out["cardsEmptyScope"]["title"] == "Nothing assigned"


def test_counters_count_assigned_matched_and_ready(out):
    assert out["counters"] == {"assigned": 5, "matched": 1, "ready": 1}
    assert out["countersObserve"]["ready"] == 0, "Observe never has anything ready to execute"


def test_apply_goes_only_through_the_confirm_flow_and_then_tells_the_rules_journal(out):
    ok = out["applyOk"]
    assert len(ok["calls"]["run"]) == 1 and ok["calls"]["run"][0]["actor"] == "Buyer One"
    assert ok["calls"]["run"][0]["command"] == "set_status"
    assert [c["url"] for c in ok["calls"]["fetch"]] == ["/rules/action-result"], "no direct command POST from the page"
    assert ok["calls"]["fetch"][0]["body"] == {"action_id": "act_1", "op_id": "op_9", "owner": "Buyer One"}
    assert out["applyScale"]["calls"]["run"][0]["value"] == "110"


def test_cancelled_failed_or_nothing_to_apply_writes_nothing(out):
    assert out["applyCancel"]["res"] == {"cancelled": True} and out["applyCancel"]["calls"]["fetch"] == []
    assert out["applyFail"]["err"].startswith("Not sent") and out["applyFail"]["calls"]["fetch"] == []
    assert out["applyAlert"]["calls"]["run"] == [] and "nothing to apply" in out["applyAlert"]["err"]


def test_dismiss_posts_to_the_rules_journal_only(out):
    assert out["dismissCall"] == {"url": "/rules/dismiss", "body": {"action_id": "act_1", "owner": "Buyer One"}}
    assert out["dismissBad"] == "already dismissed"
