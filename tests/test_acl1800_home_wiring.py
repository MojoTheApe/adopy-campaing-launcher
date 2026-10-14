"""ACL-1800: проводка команд в таблицу Home без браузера.

Логика «можно ли» и итог операции живут в `acm_commands.js` (`acmCommands.home`) и
гоняются в node; настоящие итоги операций берутся у движка на `FakeMeta`, чтобы
страница читала ровно ту форму, которую отдаёт сервер.
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
from src.fb_api import commands as cmd  # noqa: E402
from tests.test_acl1798_campaign_inventory import adset, camp, snapshot  # noqa: E402
from tests.test_acl1800_campaign_commands import (  # noqa: E402
    CAMP_A, CAMP_CBO, SET_BID, World, t)

JS = ROOT / "src" / "web" / "static" / "acm_commands.js"
TEMPLATE = (ROOT / "src" / "web" / "templates" / "_campaign_table.html").read_text(encoding="utf-8")


# ───────────────────────── сервер: доступность и поля описи ─────────────────────────

@pytest.mark.parametrize("backend,host,ok,code", [
    ("api", "127.0.0.1", True, ""), ("api", "localhost", True, ""),
    ("api", "0.0.0.0", False, "not_loopback"), ("api", "", False, "not_loopback"),
    ("rpa", "127.0.0.1", False, "rpa_backend"), (None, "127.0.0.1", False, "rpa_backend"),
])
def test_availability_says_why_commands_are_off(monkeypatch, backend, host, ok, code):
    from src.web import campaign_commands as cc

    if backend is None:
        monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    else:
        monkeypatch.setenv("ACL_FB_BACKEND", backend)
    monkeypatch.setenv("ACL_BIND_HOST", host)
    monkeypatch.delenv("ACL_FB_API_MAX_CALLS", raising=False)
    got = cc.availability()
    assert got["available"] is ok and got["code"] == code
    assert (got["max_targets"] == 7) if ok else (got["reason"] and got["max_targets"] == 0)


def test_availability_route_is_registered_before_the_op_route(monkeypatch):
    import asyncio

    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    monkeypatch.setenv("ACL_BIND_HOST", "127.0.0.1")
    paths = [r.path for r in web.app.routes if hasattr(r, "methods") and "GET" in r.methods]
    assert paths.index("/campaign-commands/availability") < paths.index("/campaign-commands/{op_id}")
    body = json.loads(asyncio.run(web.campaign_commands_availability()).body)
    assert body["available"] is True


def test_inventory_rows_carry_the_bid_fields_the_editor_needs():
    snap = snapshot()
    snap.currency = "USD"
    snap.campaigns, _ = ci.parse_inventory([camp(1000001, "C")], "campaign")
    snap.adsets, _ = ci.parse_inventory([
        adset(2000001, "S", 1000001, bid_strategy="COST_CAP", bid_amount="150"),
        adset(2000002, "S2", 1000001)], "adset")
    snap.ads = []
    rows = ci.child_rows(snap, "adset", "1000001")["rows"]
    by = {r["id"]: r for r in rows}
    assert by["2000001"]["bid_amount"] == 1.5 and by["2000001"]["bid_strategy_raw"] == "COST_CAP"
    assert by["2000002"]["bid_amount"] is None and by["2000002"]["bid_strategy_raw"] == ""
    assert ci.campaign_rows(snap)[0]["bid_amount"] is None
    assert "bid_amount" in ci.ADSET_FIELDS


def test_template_loads_the_helper_first_and_keeps_one_post_path():
    assert TEMPLATE.index('<script src="/static/acm_commands.js"></script>') < TEMPLATE.index("(function () {")
    assert re.findall(r"fetch\('([^']+)'", TEMPLATE) == ["/warm/state?", "/warm/open", "/warm/job/", "/campaign-inventory/read"]   # команды ходят через помощник; Meta читает только первый
    assert "data-batch" in TEMPLATE and "Duplicate here" in TEMPLATE and "REASON_COPY" in TEMPLATE
    assert "setInterval" not in TEMPLATE


# ───────────────────────── страница (node) ─────────────────────────

_NODE = r"""
const fs = require('fs');
const src = fs.readFileSync(__PATH__, 'utf8');
const sent = [], replies = [];
const root = { fetch: async (url, init) => {
  sent.push({ url: url, method: (init && init.method) || 'GET', body: init && init.body && JSON.parse(init.body) });
  const r = replies.shift();
  if (r.throw) throw new TypeError('network down');
  return { ok: r.status < 400, status: r.status, json: async () => r.body };
} };
root.document = { createElement: () => ({}), body: {}, getElementById: () => null };
new Function('globalThis', 'window', src.replace(/\(typeof window[^\n]*$/m, '(globalThis);'))(root, root);
const H = root.acmCommands.home;
const IN = __INPUT__;
const AV = { available: true, reason: '', max_targets: 7 };
const row = (o) => Object.assign({ level: 'adset', id: '2000001', account: 'Acc', status: 'off', status_raw: 'PAUSED',
  currency: 'USD', budget: { kind: 'daily', amount: 30 }, bid_strategy_raw: 'LOWEST_COST_WITH_BID_CAP', bid_amount: 1.5 }, o || {});
const st = (r, a, av) => H.controlState(r, a, av === undefined ? AV : av);
(async () => {
  const out = {};
  // кто может править бюджет
  out.budget = {
    owner_adset: st(row(), 'budget'), owner_campaign: st(row({ level: 'campaign' }), 'budget'),
    campaign_no_budget: st(row({ level: 'campaign', budget: { kind: 'ad_sets' } }), 'budget'),
    under_cbo: st(row({ budget: { kind: 'inherited' } }), 'budget'),
    lifetime: st(row({ budget: { kind: 'lifetime', amount: 9 } }), 'budget'),
    none: st(row({ budget: { kind: 'none' } }), 'budget'),
    unit: st(row({ budget: { kind: 'unit_unknown' } }), 'budget'),
    ad: st(row({ level: 'ad' }), 'budget'),
  };
  // ставка по стратегии
  out.bid = {
    cap: st(row(), 'bid-up'), cost: st(row({ bid_strategy_raw: 'COST_CAP' }), 'bid-down'),
    auto: st(row({ bid_strategy_raw: 'LOWEST_COST_WITHOUT_CAP' }), 'bid-up'),
    roas: st(row({ bid_strategy_raw: 'LOWEST_COST_WITH_MIN_ROAS' }), 'bid-up'),
    inherited: st(row({ bid_strategy_raw: '' }), 'bid-up'),
    campaign: st(row({ level: 'campaign' }), 'bid-up'), ad: st(row({ level: 'ad' }), 'bid-up'),
  };
  // переключатель и недоступность
  out.status = {
    on: st(row({ status: 'on' }), 'status'), unknown: st(row({ status: 'unknown', status_raw: 'ARCHIVED' }), 'status'),
    busy: st(row({ busy: true }), 'status'),
    rpa: st(row(), 'status', { available: false, reason: 'Needs ACL_FB_BACKEND=api' }),
    loopback: st(row(), 'status', { available: false, reason: 'Only when the dashboard listens on this computer alone (127.0.0.1)' }),
    unknown_avail: st(row(), 'status', null),
  };
  out.target = [H.statusTarget(row({ status: 'on' })), H.statusTarget(row({ status: 'off' }))];
  out.step = [H.stepValue(1.5, 1, 'USD'), H.stepValue(1.5, -1, 'USD'), H.stepValue(1000, 1, 'JPY'), H.stepValue(null, 1, 'USD')];
  // пакет: один аккаунт, потолок
  const rows = (n, acc) => Array.from({ length: n }, (_, i) => row({ id: String(1000001 + i), level: 'campaign', account: acc || 'Acc' }));
  out.batch = { seven: H.batchPlan(rows(7), AV).ok, eight: H.batchPlan(rows(8), AV), none: H.batchPlan([], AV),
    mixed: H.batchPlan(rows(1).concat(rows(1, 'Other')), AV), off: H.batchPlan(rows(2), { available: false, reason: 'x' }),
    small: H.batchPlan(rows(3), { available: true, max_targets: 2 }), targets: H.batchPlan(rows(2), AV).targets };
  // сценарии с настоящими итогами движка: преврат -> диалог -> запись -> опрос -> строка
  out.flows = {};
  for (const name of Object.keys(IN)) {
    const sc = IN[name];
    sent.length = 0;
    replies.length = 0;
    replies.push({ status: 200, body: sc.preview });
    replies.push({ status: 202, body: Object.assign({}, sc.final, { state: 'Running' }) });
    replies.push({ status: 200, body: sc.final });
    const states = [];
    let asked = null;
    const res = await H.flow({ account: 'Acc', actor: 'Dana', command: 'set_status', value: 'ACTIVE',
      targets: [{ level: 'campaign', id: sc.id }] },
      { confirm: async (op) => { asked = op.revision_id; return sc.confirm !== false; },
        onState: (s) => states.push(s), intervalMs: 1, key: 'k_fixed_key_0001' });
    const r = row({ level: 'campaign', id: sc.id, status: 'off', status_raw: 'PAUSED' });
    res.applied.forEach((a) => H.applySaved(r, a));
    out.flows[name] = { kind: res.kind, status: r.status, applied: res.applied.length, notes: res.notes, message: res.message,
      urls: sent.map((s) => s.method + ' ' + s.url), exec: sent[1] && sent[1].body, asked: asked, states: states, budget: r.budget, bid: r.bid_amount };
  }
  // сбои после принятия записи — «исход неизвестен», до принятия — «не отправлено»
  const base = { account: 'Acc', actor: 'Dana', command: 'set_status', value: 'ACTIVE', targets: [{ level: 'campaign', id: '1000001' }] };
  const prev = { op_id: 'op_1', revision_id: 'rev_1', affected: [{ id: '1000001', level: 'campaign', name: 'C' }], skipped: [] };
  const yes = { confirm: async () => true, intervalMs: 1, key: 'k_fixed_key_0001', timeoutMs: 5 };
  const attempt = async (script) => {
    replies.length = 0; sent.length = 0; script.forEach((x) => replies.push(x));
    try { const r = await H.flow(base, yes); return { kind: r.kind, message: r.message, applied: r.applied.length, calls: sent.length }; }
    catch (e) { return { thrown: e.message, status: e.status || null, calls: sent.length }; }
  };
  out.fail = {
    poll_network: await attempt([{ status: 200, body: prev }, { status: 202, body: { op_id: 'op_1', state: 'Running' } }, { throw: true }]),
    poll_timeout: await attempt([{ status: 200, body: prev }, { status: 202, body: { op_id: 'op_1', state: 'Running' } },
                                 { status: 200, body: { state: 'Running' } }, { status: 200, body: { state: 'Running' } },
                                 { status: 200, body: { state: 'Running' } }, { status: 200, body: { state: 'Running' } }]),
    poll_http: await attempt([{ status: 200, body: prev }, { status: 202, body: { op_id: 'op_1', state: 'Running' } },
                              { status: 500, body: { error: 'boom' } }]),
    exec_network: await attempt([{ status: 200, body: prev }, { throw: true }]),
    exec_refused: await attempt([{ status: 200, body: prev }, { status: 409, body: { error: 'This preview was already used.' } }]),
    preview_refused: await attempt([{ status: 409, body: { error: 'Needs ACL_FB_BACKEND=api' } }]),
  };
  // занятые строки в пакет не входят
  const busyRows = [row({ id: '1000001', level: 'campaign' }), row({ id: '1000002', level: 'campaign', busy: true })];
  const bp = H.batchPlan(busyRows, AV);
  out.busy = { ok: bp.ok, targets: bp.targets, skipped: bp.skippedBusy, all: H.batchPlan([busyRows[1]], AV) };
  // строку меняет только подтверждённое: settle на отказе и неизвестном исходе её не трогает
  const mk = () => [row({ id: '1000001', level: 'campaign', key: 'campaign:1000001', status: 'off', status_raw: 'PAUSED' })];
  const settleCase = (res) => { const rs = mk(); const notes = H.settle(rs, res); return { status: rs[0].status, note: (notes['campaign:1000001'] || {}).text || null }; };
  const none = { applied: [], notes: { '1000001': 'Meta refused' } };
  out.settle = {
    failed: settleCase(Object.assign({ kind: 'failed' }, none)),
    unknown: settleCase(Object.assign({ kind: 'unknown' }, none)),
    saved: settleCase({ kind: 'confirmed', notes: {}, applied: [{ id: '1000001', level: 'campaign', field: 'status', saved: 'ACTIVE', unit: 100 }] }),
    cancelled: settleCase(Object.assign({ kind: 'cancelled' }, none)),
  };
  process.stdout.write(JSON.stringify(out));
})();
"""


def _engine_op(tmp_path, scenario, command="set_status", value="ACTIVE", target=("campaign", CAMP_A)):
    """Настоящее превью и итог движка на FakeMeta."""
    w = World(tmp_path)
    if scenario in ("refuse", "lost", "applied"):
        w.meta.fail_next[target[1]] = scenario
    pv = w.preview(command, value, [t(*target)])
    w.execute(pv)
    return {"id": target[1], "preview": pv, "final": w.final(pv)}


def _run_node(inputs):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    text = _NODE.replace("__PATH__", json.dumps(str(JS))).replace("__INPUT__", json.dumps(inputs))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, timeout=60, encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def page_out(tmp_path_factory):
    d = tmp_path_factory.mktemp("w")
    inputs = {
        "ok": _engine_op(d / "a", "ok"),
        "refused": _engine_op(d / "b", "refuse"),
        "unknown": _engine_op(d / "c", "lost"),
        "applied": _engine_op(d / "d", "applied"),
        "budget": _engine_op(d / "e", "ok", "set_budget", "60", ("campaign", CAMP_CBO)),
        "bid": _engine_op(d / "f", "ok", "set_bid", "2.40", ("adset", SET_BID)),
        "cancelled": {**_engine_op(d / "g", "ok"), "confirm": False},
    }
    return _run_node(inputs)


def test_budget_controls_follow_who_owns_the_budget(page_out):
    b = page_out["budget"]
    assert b["owner_adset"]["enabled"] and b["owner_campaign"]["enabled"]
    assert b["campaign_no_budget"] == {"enabled": True, "reason": "", "openKids": True, "label": "View ad set budgets"}
    assert b["under_cbo"] == {"enabled": False, "reason": "Budget is set on the campaign"}
    assert b["lifetime"]["enabled"] is False and "Lifetime" in b["lifetime"]["reason"]
    assert b["none"]["enabled"] is False and b["unit"]["enabled"] is False
    assert b["ad"] == {"enabled": False, "reason": "Ads have no budget"}


def test_bid_controls_are_strategy_aware_and_refuse_automatic(page_out):
    b = page_out["bid"]
    assert b["cap"]["enabled"] and b["cost"]["enabled"]
    assert b["auto"] == {"enabled": False, "reason": "Automatic · no manual bid"}
    assert b["roas"]["enabled"] is False and b["inherited"]["enabled"] is False
    assert b["campaign"]["enabled"] is False and b["ad"]["reason"] == "Managed by ad set"


def test_switch_state_and_unavailable_reasons(page_out):
    s = page_out["status"]
    assert s["on"]["enabled"] and "Status ARCHIVED" in s["unknown"]["reason"]
    assert s["busy"]["enabled"] is False
    assert s["rpa"] == {"enabled": False, "reason": "Commands unavailable: Needs ACL_FB_BACKEND=api"}
    assert "127.0.0.1" in s["loopback"]["reason"] and s["unknown_avail"]["reason"] == "Commands unavailable"
    assert page_out["target"] == ["PAUSED", "ACTIVE"]
    assert page_out["step"] == ["1.58", "1.42", "1050", ""]


def test_batch_is_one_account_and_capped_by_the_call_budget(page_out):
    b = page_out["batch"]
    assert b["seven"] is True
    assert b["eight"] == {"ok": False, "reason": "At most 7 objects per command with the current call limit"}
    assert b["none"]["reason"] == "Select rows first" and b["mixed"]["reason"] == "Select rows from one account"
    assert b["off"]["ok"] is False and "Commands unavailable" in b["off"]["reason"]
    assert "At most 2 objects" in b["small"]["reason"]
    assert b["targets"] == [{"level": "campaign", "id": "1000001"}, {"level": "campaign", "id": "1000002"}]


def test_switch_flow_updates_the_row_only_from_the_confirmed_result(page_out):
    f = page_out["flows"]["ok"]
    assert f["kind"] == "confirmed" and f["status"] == "on" and f["applied"] == 1
    assert f["urls"][0] == "POST /campaign-commands/preview" and f["urls"][1] == "POST /campaign-commands/execute"
    assert f["urls"][2].startswith("GET /campaign-commands/op_")
    assert f["exec"]["idempotency_key"] == "k_fixed_key_0001" and f["exec"]["revision_id"] == f["asked"]
    assert f["states"][0] == "Previewing…" and "Running…" in f["states"]


def test_failed_execute_applies_nothing_and_keeps_the_previous_state(page_out):
    f = page_out["flows"]["refused"]
    assert f["kind"] == "failed" and f["status"] == "off" and f["applied"] == 0
    assert any("Meta refused" in n for n in f["notes"].values())


def test_unknown_outcome_applies_nothing_until_a_readback_confirms(page_out):
    f = page_out["flows"]["unknown"]
    assert f["kind"] == "unknown" and f["status"] == "off" and f["applied"] == 0
    assert "Outcome unknown" not in f["message"] and "Nothing was retried" in f["message"]
    assert page_out["flows"]["applied"]["kind"] == "confirmed" and page_out["flows"]["applied"]["status"] == "on"


def test_budget_and_bid_rows_are_updated_from_the_saved_value(page_out):
    assert page_out["flows"]["budget"]["budget"] == {"kind": "daily", "amount": 60}
    assert page_out["flows"]["bid"]["bid"] == 2.4


def test_cancel_sends_neither_execute_nor_a_status_poll(page_out):
    f = page_out["flows"]["cancelled"]
    assert f["kind"] == "cancelled" and f["urls"] == ["POST /campaign-commands/preview"] and f["status"] == "off"


def test_poll_failures_after_execute_say_outcome_unknown_not_not_sent(page_out):
    unknown = "Outcome unknown — check the history and Read campaigns"
    for name in ("poll_network", "poll_timeout", "poll_http", "exec_network"):
        f = page_out["fail"][name]
        assert f.get("kind") == "unknown" and f["message"] == unknown and f["applied"] == 0, (name, f)
        assert "Not sent" not in json.dumps(f)
    # сервер отказал до принятия: исключение наружу, страница напишет «Not sent»
    assert page_out["fail"]["exec_refused"] == {"thrown": "This preview was already used.", "status": 409, "calls": 2}
    assert page_out["fail"]["preview_refused"]["thrown"] == "Needs ACL_FB_BACKEND=api"
    assert page_out["fail"]["preview_refused"]["calls"] == 1


def test_batch_leaves_busy_rows_out(page_out):
    b = page_out["busy"]
    assert b["ok"] is True and b["targets"] == [{"level": "campaign", "id": "1000001"}] and b["skipped"] == 1
    assert b["all"] == {"ok": False, "reason": "The selected rows are busy with another command"}


def test_a_row_changes_only_from_a_confirmed_readback(page_out):
    st = page_out["settle"]
    assert st["failed"] == {"status": "off", "note": "Failed — previous state kept: Meta refused"}
    assert st["unknown"] == {"status": "off", "note": "Outcome unknown — press Read campaigns to check"}
    assert st["saved"] == {"status": "on", "note": "Confirmed"}
    assert st["cancelled"] == {"status": "off", "note": None}


def test_template_never_sets_row_state_itself():
    """Мутация «оптимистичный переключатель» ловится и здесь, без браузера: строку меняет только помощник."""
    code = TEMPLATE.split("<script>", 1)[1]
    assert not re.search(r"\b(?:r|row)\.(?:status|status_raw|budget|bid_amount)\s*=(?!=)", code)
    assert "aria-checked=\"' + (st === 'on')" in TEMPLATE            # переключатель рисуется из r.status
    assert "ACM.home.settle(rows, res)" in TEMPLATE
    assert "Outcome unknown" not in TEMPLATE.replace("Outcome unknown —", "")  # текст живёт в помощнике
