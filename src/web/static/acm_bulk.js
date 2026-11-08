/* ACL-2457: bulk edit of selected campaigns on the Home table (budget, age, gender, languages, placements).
 *
 * Loaded after acm_commands.js and adds `acmCommands.bulk`; it reuses that helper's request, poll and dialog
 * plumbing (`acmCommands.kit`). The flow is the ACL-1800 one: preview (nothing written) -> human Confirm ->
 * execute -> poll the readback. All text for people is English.
 */
(function (root) {
  'use strict';

  var A = root.acmCommands;
  if (!A || !A.kit) return;
  var K = A.kit, el = K.el, ensureStyle0 = K.ensureStyle, post = K.post, execute = K.execute, poll = K.poll,
      refuse = K.refuse, isZeroDecimal = K.isZeroDecimal, UNKNOWN_TEXT = K.UNKNOWN_TEXT;

  function ensureStyle() {
    ensureStyle0();
    var doc = root.document;
    if (!doc || !doc.head || (doc.getElementById && doc.getElementById('acm-bulk-style'))) return;
    var st = doc.createElement('style');
    st.id = 'acm-bulk-style';
    st.textContent =
      'dialog.acm-edit input,dialog.acm-edit select{min-height:32px;padding:0 .5rem}' +
      '.acm-camp{margin:.5rem 0;padding-top:.25rem;border-top:1px solid var(--border,#dfe3ea)}' +
      '.acm-camp h3{margin:.25rem 0}.acm-skipped{margin:.25rem 0;padding-left:1.1rem;font-size:.8rem;color:var(--muted,#647084)}';
    doc.head.appendChild(st);
  }

  // ── ACL-2457: bulk edit of selected campaigns (budget, age, gender, languages, placements) ──
  // Same path as every command here: preview (nothing written) -> human Confirm -> execute -> poll the readback.
  // The page only asks; the server re-checks, re-reads before writing and writes only the fields that change.
  var BULK_RPA = 'API backend only';
  var BULK_MAX = 10;
  var PRESET_TEXT = { advantage_plus: 'Advantage+ (automatic)', acl_default: 'ACL default (manual placements)' };
  var RESULT_TEXT = { saved: 'Saved - confirmed by readback', refused: 'Meta refused', mismatch: 'Saved a different value',
                      unknown: 'Outcome unknown', not_attempted: 'Not attempted', not_sent: 'Not sent', pending: 'Waiting',
                      sending: 'Sending', sent: 'Sent' };

  // Whether the control is on and why not. Older servers without `bulk` mean "off".
  function bulkState(avail) {
    if (avail && avail.bulk) {
      return { enabled: !!avail.bulk.available, reason: avail.bulk.available ? '' : (avail.bulk.reason || 'Bulk edit unavailable'),
               max: avail.bulk.max_campaigns || 0 };
    }
    if (avail && avail.available === false) {
      return { enabled: false, reason: avail.code === 'rpa_backend' ? BULK_RPA : ('Commands unavailable' + (avail.reason ? ': ' + avail.reason : '')), max: 0 };
    }
    return { enabled: false, reason: 'Bulk edit unavailable', max: 0 };
  }

  // Selected campaign rows -> one request: campaigns only, one account, within the limit.
  function bulkPlan(rows, avail) {
    var st = bulkState(avail);
    if (!st.enabled) return { ok: false, reason: st.reason };
    rows = rows.filter(function (r) { return r.level === 'campaign'; });
    if (!rows.length) return { ok: false, reason: 'Select campaigns first' };
    var free = rows.filter(function (r) { return !r.busy; });
    if (!free.length) return { ok: false, reason: 'The selected campaigns are busy with another command' };
    var accounts = {};
    free.forEach(function (r) { accounts[r.account] = true; });
    if (Object.keys(accounts).length > 1) return { ok: false, reason: 'Select campaigns from one account' };
    var cap = Math.min(BULK_MAX, st.max || BULK_MAX);
    if (free.length > cap) return { ok: false, reason: 'At most ' + cap + ' campaigns per bulk edit' };
    return { ok: true, account: free[0].account, rows: free, ids: free.map(function (r) { return String(r.id); }) };
  }

  // Form values -> the change set (only what the buyer filled in). Mirrors the server; the server checks again.
  function buildChanges(v, currency) {
    v = v || {};
    var out = {}, t = function (x) { return x == null ? '' : String(x).trim(); };
    if (t(v.budget)) {
      var zero = isZeroDecimal(currency), b = t(v.budget);
      var good = zero ? /^[1-9][0-9]{0,11}$/.test(b) : /^[0-9]{1,12}(\.[0-9]{1,2})?$/.test(b) && Number(b) > 0;
      if (!good) return { error: zero ? 'Enter a whole positive budget.' : 'Enter a positive budget with at most two decimals.' };
      out.budget = b;
    }
    var ages = ['age_min', 'age_max'];
    for (var i = 0; i < ages.length; i++) {
      var a = t(v[ages[i]]);
      if (!a) continue;
      if (!/^[0-9]{2}$/.test(a) || Number(a) < 13 || Number(a) > 65) return { error: 'Age must be a whole number from 13 to 65.' };
      out[ages[i]] = Number(a);
    }
    if (out.age_min != null && out.age_max != null && out.age_min > out.age_max) return { error: 'The minimum age is above the maximum age.' };
    if (t(v.gender)) {
      if (['all', 'men', 'women'].indexOf(t(v.gender)) < 0) return { error: 'Gender must be all, men or women.' };
      out.gender = t(v.gender);
    }
    if (t(v.languages)) {
      var names = [];
      t(v.languages).split(',').forEach(function (n) { n = n.trim().replace(/\s+/g, ' '); if (n && names.indexOf(n) < 0) names.push(n); });
      if (!names.length || names.length > 5) return { error: 'Give 1 to 5 languages, separated by commas.' };
      if (names.some(function (n) { return n.length < 2 || n.length > 60; })) return { error: 'A language name must be 2 to 60 characters.' };
      out.languages = names;
    }
    if (t(v.placements)) {
      if (!PRESET_TEXT[t(v.placements)]) return { error: 'Choose a placements preset from the list.' };
      out.placements = t(v.placements);
    }
    if (!Object.keys(out).length) return { error: 'Fill in at least one field to change.' };
    return { changes: out };
  }

  function bulkPreview(req) {
    if (!req || !req.account || !Array.isArray(req.campaigns) || !req.campaigns.length) {
      return Promise.reject(refuse('Pick at least one campaign.'));
    }
    if (req.campaigns.length > BULK_MAX) return Promise.reject(refuse('At most ' + BULK_MAX + ' campaigns per bulk edit.'));
    return post('/campaign-commands/bulk/preview', { account: req.account, actor: req.actor, changes: req.changes,
                                                     campaigns: req.campaigns.map(String) });
  }

  // The operation -> what the dialogs show, grouped by campaign (pure; tested without a browser).
  function bulkModel(op) {
    var groups = [], byId = {};
    function group(id, name) {
      if (!byId[id]) { byId[id] = { id: id, name: name || id, lines: [], skipped: [] }; groups.push(byId[id]); }
      return byId[id];
    }
    (op.affected || []).forEach(function (a) {
      var g = group(a.campaign_id, a.campaign_name);
      var label = a.level === 'campaign' ? (a.field === 'lifetime_budget' ? 'Lifetime budget' : 'Daily budget') : 'Ad set "' + a.name + '"';
      g.lines.push({ id: a.id, label: label, before: a.before_display, after: a.after_display, result: a.result || '',
                     resultText: RESULT_TEXT[a.result] || '', note: a.note || '' });
    });
    (op.skipped || []).forEach(function (s) {
      var g = group(s.campaign_id || s.id, s.campaign_name || s.name);
      g.skipped.push({ name: s.level === 'adset' ? 'Ad set "' + s.name + '"' : (s.field === 'budget' ? 'Budget' : 'Campaign'), reason: s.reason });
    });
    groups.forEach(function (g) {
      var n = g.lines.length, saved = g.lines.filter(function (l) { return l.result === 'saved'; }).length;
      if (!n) g.state = 'skipped';
      else if (saved === n) g.state = 'saved';
      else if (g.lines.some(function (l) { return l.result === 'unknown'; })) g.state = 'unknown';
      else if (saved) g.state = 'partial';
      else g.state = g.lines.some(function (l) { return l.result; }) ? 'failed' : 'pending';
    });
    var changes = (op.affected || []).length;
    var campaigns = groups.filter(function (g) { return g.lines.length; }).length;
    return { account: op.account, currency: op.currency, groups: groups, changes: changes, campaigns: campaigns,
             skippedCount: (op.skipped || []).length, canConfirm: changes > 0,
             title: 'Bulk edit: ' + changes + ' change' + (changes === 1 ? '' : 's') + ' on ' + campaigns + ' campaign' + (campaigns === 1 ? '' : 's') };
  }

  function dialogShell(title, cls, width) {
    ensureStyle();
    var dlg = el('dialog', undefined, cls);
    dlg.setAttribute('aria-label', title);
    dlg.style.cssText = 'max-width:' + width + 'px;width:calc(100% - 32px);max-height:85vh;overflow:auto;border:1px solid var(--border,#dfe3ea);'
      + 'border-radius:var(--radius-lg,10px);padding:var(--space-lg,16px);background:var(--bg-elev,#fff);color:var(--text,#1a1d23)';
    return dlg;
  }
  function button(label, kind) {
    var b = el('button', label); b.type = 'button'; b.setAttribute('data-acm', kind); return b;
  }
  function fieldRow(label, control, hint) {
    var l = el('label', label); l.style.marginTop = '.5rem'; l.appendChild(control);
    if (hint) l.appendChild(el('span', hint, 'acm-sub'));
    return l;
  }
  function selectOf(name, options) {
    var sel = el('select'); sel.setAttribute('data-acm-field', name);
    options.forEach(function (o) { var op = el('option', o[1]); op.value = o[0]; sel.appendChild(op); });
    return sel;
  }
  function inputOf(name, mode, placeholder) {
    var inp = el('input'); inp.type = 'text'; inp.setAttribute('data-acm-field', name);
    if (mode) inp.setAttribute('inputmode', mode);
    if (placeholder) inp.placeholder = placeholder;
    return inp;
  }

  // The edit panel. Resolves with the change set, or null on Cancel.
  function bulkEditDialog(opts) {
    return new Promise(function (resolve) {
      var dlg = dialogShell('Bulk edit', 'acm-edit acm-bulk', 520);
      dlg.appendChild(el('h2', 'Bulk edit ' + opts.count + ' campaign' + (opts.count === 1 ? '' : 's')));
      dlg.appendChild(el('p', opts.account + '. Fill in only what should change - empty fields are left alone. You see every before and after before anything is written.', 'acm-sub'));
      var f = {};
      f.budget = inputOf('budget', 'decimal', 'e.g. 25.00');
      dlg.appendChild(fieldRow('Budget (' + (opts.currency || '?') + ') - daily or lifetime, as each campaign has it', f.budget,
                               'Campaigns with the budget on their ad sets are skipped.'));
      var ages = el('div', undefined, 'acm-ages');
      f.age_min = inputOf('age_min', 'numeric', 'min 13'); f.age_max = inputOf('age_max', 'numeric', 'max 65');
      ages.appendChild(fieldRow('Age from', f.age_min)); ages.appendChild(fieldRow('Age to', f.age_max));
      ages.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:.5rem';
      dlg.appendChild(ages);
      f.gender = selectOf('gender', [['', 'No change'], ['all', 'All'], ['men', 'Men'], ['women', 'Women']]);
      dlg.appendChild(fieldRow('Gender', f.gender));
      f.languages = inputOf('languages', '', 'e.g. Spanish, French');
      dlg.appendChild(fieldRow('Languages (replaces the current list)', f.languages, 'Names as in Ads Manager; the first stays the main one.'));
      f.placements = selectOf('placements', [['', 'No change'], ['advantage_plus', PRESET_TEXT.advantage_plus], ['acl_default', PRESET_TEXT.acl_default]]);
      dlg.appendChild(fieldRow('Placements preset', f.placements));
      var err = el('p', '', 'acm-warn'); err.setAttribute('role', 'alert'); dlg.appendChild(err);
      var bar = el('div', undefined, 'acm-actions');
      var cancel = button('Cancel', 'cancel'), ok = button('Review', 'review');
      bar.appendChild(cancel); bar.appendChild(ok); dlg.appendChild(bar);
      function done(v) {
        try { dlg.close(); } catch (e) { /* already closed */ }
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(v);
      }
      ok.addEventListener('click', function () {
        var vals = {};
        Object.keys(f).forEach(function (k) { vals[k] = f[k].value; });
        var res = buildChanges(vals, opts.currency);
        if (res.error) { err.textContent = res.error; return; }
        done(res.changes);
      });
      cancel.addEventListener('click', function () { done(null); });
      dlg.addEventListener('cancel', function () { done(null); });
      root.document.body.appendChild(dlg);
      dlg.showModal();
      f.budget.focus();
    });
  }

  var GROUP_WORD = { saved: 'saved', partial: 'partly saved', failed: 'not saved', unknown: 'outcome unknown', skipped: 'skipped', pending: '' };
  function renderGroups(dlg, m, withResult) {
    m.groups.forEach(function (g) {
      var box = el('section', undefined, 'acm-camp');
      box.appendChild(el('h3', g.name + (withResult && GROUP_WORD[g.state] ? ' - ' + GROUP_WORD[g.state] : '')));
      if (g.lines.length) {
        var table = el('table', undefined, 'acm-diff');
        g.lines.forEach(function (l) {
          var tr = el('tr');
          tr.appendChild(el('td', l.label));
          tr.appendChild(el('td', l.before + ' → ' + l.after));
          if (withResult) tr.appendChild(el('td', l.resultText + (l.note && l.result !== 'saved' ? ': ' + l.note : '')));
          table.appendChild(tr);
        });
        box.appendChild(table);
      }
      if (g.skipped.length) {
        var ul = el('ul', undefined, 'acm-skipped');
        g.skipped.forEach(function (s) { ul.appendChild(el('li', 'Skipped - ' + s.name + ': ' + s.reason)); });
        box.appendChild(ul);
      }
      dlg.appendChild(box);
    });
  }

  // The confirmation: before -> after per campaign. "Confirm" is the only way to a write.
  function bulkConfirm(op) {
    return new Promise(function (resolve) {
      var m = bulkModel(op);
      var dlg = dialogShell(m.title, 'acm-confirm acm-bulk', 720);
      dlg.appendChild(el('h2', m.title));
      dlg.appendChild(el('p', m.account + ' · amounts in ' + m.currency + '. Nothing has been written yet.', 'acm-sub'));
      renderGroups(dlg, m, false);
      var bar = el('div', undefined, 'acm-actions');
      var cancel = button('Cancel', 'cancel'), ok = button(m.canConfirm ? 'Confirm' : 'Nothing to change', 'confirm');
      ok.disabled = !m.canConfirm;
      bar.appendChild(cancel); bar.appendChild(ok); dlg.appendChild(bar);
      function done(v) {
        try { dlg.close(); } catch (e) { /* already closed */ }
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(v);
      }
      ok.addEventListener('click', function () { done(true); });
      cancel.addEventListener('click', function () { done(false); });
      dlg.addEventListener('cancel', function () { done(false); });
      root.document.body.appendChild(dlg);
      dlg.showModal();
      cancel.focus();
    });
  }

  // The result per campaign, after the readback.
  function bulkResult(op) {
    return new Promise(function (resolve) {
      var m = bulkModel(op);
      var dlg = dialogShell('Bulk edit result', 'acm-confirm acm-bulk acm-result', 760);
      dlg.appendChild(el('h2', 'Bulk edit result'));
      dlg.appendChild(el('p', op.message || '', 'acm-sub'));
      renderGroups(dlg, m, true);
      var bar = el('div', undefined, 'acm-actions');
      var close = button('Close', 'close'); bar.appendChild(close); dlg.appendChild(bar);
      function done() {
        try { dlg.close(); } catch (e) { /* already closed */ }
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(true);
      }
      close.addEventListener('click', done);
      dlg.addEventListener('cancel', done);
      root.document.body.appendChild(dlg);
      dlg.showModal();
      close.focus();
    });
  }

  // preview -> Confirm -> execute -> poll. A refusal before the server accepts the write is "not sent";
  // a lost answer after it is "outcome unknown", never "not sent".
  function bulkFlow(req, hooks) {
    hooks = hooks || {};
    var ask = hooks.confirm || bulkConfirm, say = hooks.onState || function () {};
    say('Previewing…');
    return bulkPreview(req).then(function (op) {
      return ask(op).then(function (yes) {
        if (!yes) return { kind: 'cancelled', op: op };
        if (!(op.affected || []).length) return { kind: 'nothing', op: op };
        say('Running…');
        return execute(op.revision_id, hooks.key).then(function (started) {
          return poll(started.op_id, { intervalMs: hooks.intervalMs, timeoutMs: hooks.timeoutMs, onUpdate: function (o) {
            say(o.state === 'Succeeded' && o.refresh_pending ? 'Refreshing list…' : o.state === 'Verifying' ? 'Verifying…' : 'Running…');
          } }).then(function (final) {
            return { kind: final.state === 'Succeeded' ? 'confirmed' : final.state === 'Outcome unknown' ? 'unknown' : 'failed', op: final };
          }, function () { return { kind: 'unknown', op: op, message: UNKNOWN_TEXT }; });
        }, function (e) {
          if (e && e.status) throw e;
          return { kind: 'unknown', op: op, message: UNKNOWN_TEXT };
        });
      });
    });
  }


  A.bulk = { state: bulkState, plan: bulkPlan, buildChanges: buildChanges, preview: bulkPreview, model: bulkModel,
             editDialog: bulkEditDialog, confirm: bulkConfirm, result: bulkResult, flow: bulkFlow,
             RPA_REASON: BULK_RPA, PRESETS: PRESET_TEXT };
})(typeof window !== 'undefined' ? window : globalThis);
