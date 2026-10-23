/* ACL-1822: общие помощники страницы Rules и ячейки Rules в таблице кампаний.
 *
 * Чистые функции (без DOM), чтобы их проверять в node:
 *   aclRules.esc(text)                      -> экранированный текст для innerHTML
 *   aclRules.identityText(verdict)          -> подпись вердикта идентичности
 *   aclRules.conditionSentence(conds, T)    -> «Spend ≥ 3× T ($180) AND FTDs = 0»
 *   aclRules.cards(rule, objectEval)        -> модель четырёх карточек Scope → Evidence → Conditions → Action
 *   aclRules.counters(rule)                 -> assigned / matched / ready
 *   aclRules.proposalRequest(p)             -> запрос для acmCommands.run или null
 *   aclRules.applyProposal(p, owner, deps)  -> Apply ТОЛЬКО через acmCommands.run (превью → подтверждение → readback)
 *   aclRules.undoProposal(p, owner, deps)   -> Undo через тот же человеческий поток; предыдущие значения даёт сервер команд
 *   aclRules.dismissProposal(p, owner, deps)-> пометка «dismissed» в журнале правил
 *
 * Единственный путь записи в кампании - `acmCommands.run`: здесь нет ни одного прямого POST
 * в маршруты команд над кампаниями. Весь текст для человека - по-английски.
 */
(function (root) {
  'use strict';

  var AUTOMATIC_REASON = 'Automatic comes with the rules executor (ACL rules T5) — needs owner settings';
  var MODES = [
    { v: 'disabled', label: 'Disabled', hint: 'Nothing is evaluated' },
    { v: 'observe', label: 'Observe', hint: 'Record only' },
    { v: 'suggest', label: 'Suggest', hint: 'Review first' },
    { v: 'automatic', label: 'Automatic', hint: AUTOMATIC_REASON, disabled: true }
  ];
  var METRIC = {
    spend: 'Spend', ftd: 'FTDs', leads: 'Leads', cpa: 'CPA', cr: 'CR', ftd_total: 'FTDs, all blocks',
    ftd_block: 'FTDs, worst block', cpa_block: 'CPA, worst block', cpa_change_pct: 'CPA change',
    cr_change_pct: 'CR change', age_days: 'Age, days'
  };
  var MONEY = { spend: 1, cpa: 1, cpa_block: 1 };
  var IDENTITY = {
    exact: 'Exact', exact_by_children: 'By ad sets', copy_suffix: 'Copy suffix', alias_heuristic: 'Alias guess',
    partial: 'Partial', duplicate_name: 'Ambiguous: duplicate name', multi_country: 'Ambiguous: several countries',
    renamed: 'Renamed', not_in_workbook: 'Not in workbook'
  };
  var KIND = { pause: 'Pause', budget_up: 'Scale', alert: 'Alert', block_scale: 'Block scale',
    draft_replacement: 'Replacement draft', enable: 'Enable' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function identityText(v) { return v ? (IDENTITY[v] || String(v)) : 'Not checked'; }
  function usd(v) {
    if (v == null || isNaN(v)) return '—';
    return '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function num(v) { return v == null || isNaN(v) ? '—' : String(Math.round(v * 100) / 100); }

  // ── условия ──
  function leafText(c, T) {
    var m = METRIC[c.metric] || c.metric, v = c.value, cmp = { '>=': '≥', '<=': '≤', '==': '=' }[c.cmp] || c.cmp;
    if (c.unit === 't_multiple') {
      return m + ' ' + cmp + ' ' + num(v) + '× T' + (T != null ? ' (' + usd(v * T) + ')' : ' (no target)');
    }
    if (c.unit === 'pct') return m + ' ' + cmp + ' ' + num(v) + '%';
    return m + ' ' + cmp + ' ' + (MONEY[c.metric] ? usd(v) : num(v));
  }
  function conditionSentence(conds, T) {
    function one(c) {
      if (c && c.items) {
        var parts = c.items.map(one);
        var s = parts.join(' ' + c.op + ' ');
        return parts.length > 1 ? s : s;
      }
      return leafText(c, T);
    }
    return (conds || []).map(one).join(' AND ') || 'No conditions yet';
  }
  function observedText(o) {
    if (!o) return '';
    var bits = [];
    if (o.spend != null) bits.push('Spend ' + usd(o.spend));
    if (o.ftd != null) bits.push('FTDs ' + num(o.ftd));
    if (o.leads != null) bits.push('Leads ' + num(o.leads));
    bits.push('CPA ' + (o.cpa != null ? usd(o.cpa) : 'undefined (no FTDs)'));
    if (o.target != null) bits.push('T ' + usd(o.target));
    return bits.join(' · ');
  }

  // ── карточки «Why would this rule act?» ──
  var EVIDENCE_PROBLEMS = ['Ambiguous mapping', 'Missing mapping', 'Stale BI', 'Stale snapshot', 'Data mismatch',
    'Not reconciled', 'No target', 'Insufficient evidence', 'Pending outcomes'];
  function problemOf(reasons) {
    for (var i = 0; i < (reasons || []).length; i++) if (EVIDENCE_PROBLEMS.indexOf(reasons[i]) >= 0) return reasons[i];
    return '';
  }
  function cards(rule, o) {
    var counts = (rule && rule.counts) || { campaign: 0, adset: 0, ad: 0, total: 0 };
    var scope = counts.total
      ? { title: counts.campaign + ' campaign' + (counts.campaign === 1 ? '' : 's'),
          sub: counts.adset + ' ad set' + (counts.adset === 1 ? '' : 's') + (counts.ad ? ' · ' + counts.ad + ' ads' : '') + ' explicitly selected', tone: 'ok' }
      : { title: 'Nothing assigned', sub: 'Choose campaigns to start', tone: 'warn' };
    var last = rule && rule.last_eval;
    var evidence, conditions, action;
    if (!last || last.error) {
      var why = last && last.error ? String(last.error) : 'Press Evaluate now';
      evidence = { title: last ? 'Evaluation failed' : 'Not evaluated yet', sub: why, tone: last ? 'bad' : 'warn' };
    } else if (!o) {
      evidence = { title: 'Pick an object', sub: 'Choose one of the assigned objects', tone: 'warn' };
    } else {
      var prob = problemOf(o.reasons);
      evidence = prob ? { title: prob, sub: (o.notes && o.notes[0]) || (o.reasons || []).join(' · '), tone: 'warn' }
        : { title: 'Available', sub: 'Identity: ' + identityText(o.identity) + ' · report period', tone: 'ok' };
    }
    if (!last || last.error || !o) {
      conditions = { title: '—', sub: 'Needs an evaluation', tone: 'neutral' };
      action = { title: '—', sub: 'Needs an evaluation', tone: 'neutral' };
    } else {
      var cm = o.conditions_matched;
      conditions = cm === true ? { title: 'Matched', sub: observedText(o.observed), tone: 'ok' }
        : cm === false ? { title: 'Not matched', sub: observedText(o.observed), tone: 'neutral' }
        : { title: 'Unknown', sub: 'A value is undefined, so nothing is proven', tone: 'warn' };
      if (o.verdict === 'matched' && o.action) {
        var mode = rule.mode;
        action = mode === 'suggest' ? { title: 'Ready', sub: (KIND[o.action.kind] || 'Action') + ' can be suggested', tone: 'ok' }
          : { title: 'Observe only', sub: 'Would ' + (KIND[o.action.kind] || 'act').toLowerCase() + ' — no suggestion in Observe', tone: 'neutral' };
      } else if (o.verdict === 'blocked') {
        action = { title: 'Held', sub: (o.reasons || []).join(' · ') || 'Blocked', tone: 'warn' };
      } else {
        action = { title: 'No action', sub: 'Conditions did not match', tone: 'neutral' };
      }
    }
    return [
      { n: '01', name: 'Scope', title: scope.title, sub: scope.sub, tone: scope.tone },
      { n: '02', name: 'Evidence', title: evidence.title, sub: evidence.sub, tone: evidence.tone },
      { n: '03', name: 'Conditions', title: conditions.title, sub: conditions.sub, tone: conditions.tone },
      { n: '04', name: 'Action', title: action.title, sub: action.sub, tone: action.tone }
    ];
  }
  function counters(rule) {
    var last = rule && rule.last_eval, objs = (last && last.objects) || [];
    return {
      assigned: rule && rule.counts ? rule.counts.total : 0,
      matched: objs.filter(function (x) { return x.conditions_matched === true; }).length,
      ready: objs.filter(function (x) { return x.verdict === 'matched' && x.action && rule.mode === 'suggest'; }).length
    };
  }

  // ── Apply / Dismiss ──
  function proposalRequest(p) {
    if (!p || !p.account || !p.id || !/^(campaign|adset|ad)$/.test(p.level || '')) return null;
    var target = [{ level: p.level, id: String(p.id) }];
    if (p.kind === 'pause' && p.requested && p.requested.status === 'PAUSED') {
      return { account: p.account, command: 'set_status', value: 'PAUSED', targets: target };
    }
    if (p.kind === 'budget_up' && p.requested && p.requested.daily_budget > 0) {
      return { account: p.account, command: 'set_budget', value: String(p.requested.daily_budget), targets: target };
    }
    return null;   // alert / block_scale / draft_replacement ничего не пишут: Apply не нужен
  }
  function proposalText(p) {
    var req = proposalRequest(p);
    if (!req) return KIND[p && p.kind] || 'Suggestion';
    if (req.command === 'set_status') return 'Pause the ' + (p.level === 'adset' ? 'ad set' : p.level);
    var b = p.before && p.before.daily_budget;
    return 'Daily budget ' + (b != null ? num(b) + ' → ' : 'to ') + num(p.requested.daily_budget);
  }

  function postJson(fetchFn, url, body) {
    return fetchFn(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok || j.ok === false) { var e = new Error(j.error || ('Request failed (' + r.status + ')')); e.code = j.code; throw e; }
          return j;
        });
      });
  }

  /* Apply: превью → диалог подтверждения → запись → чтение Meta - всё внутри acmCommands.run.
     Страница сама ничего не пишет. После итога журналу правил сообщаем op_id: переход берётся
     у сервера команд, не со слов страницы. */
  function applyProposal(p, owner, deps) {
    deps = deps || {};
    var acm = deps.acm || root.acmCommands, fetchFn = deps.fetch || root.fetch.bind(root);
    var req = proposalRequest(p);
    if (!req) return Promise.reject(new Error('This suggestion has nothing to apply.'));
    req.actor = owner;
    if (!acm || !acm.run) return Promise.reject(new Error('The confirm flow is not loaded. Reload the dashboard.'));
    return acm.run(req).then(function (op) {
      if (!op || op.cancelled || !op.op_id) return { cancelled: true };
      return postJson(fetchFn, '/rules/action-result', { action_id: p.action_id, op_id: op.op_id, owner: owner })
        .then(function (rec) { return { cancelled: false, op: op, recorded: rec }; });
    });
  }
  function dismissProposal(p, owner, deps) {
    var fetchFn = (deps && deps.fetch) || root.fetch.bind(root);
    return postJson(fetchFn, '/rules/dismiss', { action_id: p.action_id, owner: owner });
  }

  function canUndo(p) { return p && p.state === 'verified' && p.undo_state !== 'unknown'; }
  function undoLabel(p) {
    return p.state === 'undone' ? 'Undone' : p.undo_state === 'unknown' ? 'Undo outcome unknown'
      : p.undo_state === 'failed' ? 'Applied · Undo failed' : 'Applied';
  }
  function undoProposal(p, owner, deps) {
    deps = deps || {};
    var acm = deps.acm || root.acmCommands, fetchFn = deps.fetch || root.fetch.bind(root);
    if (!canUndo(p)) return Promise.reject(new Error('This action has no verified undo available.'));
    if (!acm || !acm.run) return Promise.reject(new Error('The confirm flow is not loaded. Reload the dashboard.'));
    // This local read cannot change a campaign. The returned inverse request still
    // needs a fresh normal preview, explicit confirmation and verified command.
    return postJson(fetchFn, '/rules/undo-request', { action_id: p.action_id, owner: owner }).then(function (prepared) {
      var req = prepared.request;
      req.actor = owner;
      return acm.run(req).then(function (op) {
        if (!op || op.cancelled || !op.op_id) return { cancelled: true };
        return postJson(fetchFn, '/rules/undo-result', { action_id: p.action_id, op_id: op.op_id, owner: owner })
          .then(function (rec) { return { cancelled: false, op: op, recorded: rec }; });
      });
    });
  }

  root.aclRules = {
    AUTOMATIC_REASON: AUTOMATIC_REASON, MODES: MODES, METRIC: METRIC, KIND: KIND,
    esc: esc, usd: usd, num: num, identityText: identityText, conditionSentence: conditionSentence,
    observedText: observedText, cards: cards, counters: counters, problemOf: problemOf,
    proposalRequest: proposalRequest, proposalText: proposalText, applyProposal: applyProposal,
    dismissProposal: dismissProposal, canUndo: canUndo, undoLabel: undoLabel, undoProposal: undoProposal, postJson: postJson
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.aclRules;
})(typeof window !== 'undefined' ? window : globalThis);
