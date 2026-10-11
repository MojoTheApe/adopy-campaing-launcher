/* ACL-1800: помощник страницы для команд над кампаниями (статус, бюджет, ставка).
 *
 * Таблицу Home строит ACL-1798 и зовёт отсюда:
 *   acmCommands.preview({account, actor, command, value, targets})  -> разбор (операция в Preview)
 *   acmCommands.confirm(op)                                         -> Promise<boolean> (диалог)
 *   acmCommands.execute(op.revision_id[, key])                      -> операция (Running)
 *   acmCommands.poll(op_id, {onUpdate})                             -> итог (Succeeded / Failed / Outcome unknown)
 *   acmCommands.run(request)                                        -> всё подряд: превью → диалог → запись → опрос
 *
 * Опрос ходит только на наш сервер (статус в памяти), не в Meta.
 * Здесь же — зеркало серверной политики: страница сама отказывается слать
 * что-либо, кроме статуса ACTIVE/PAUSED, дневного бюджета и ставки.
 * Весь текст для человека — по-английски.
 */
(function (root) {
  'use strict';

  var COMMANDS = { set_status: 'status', set_budget: 'daily_budget', set_bid: 'bid_amount' };
  var LEVELS = ['campaign', 'adset', 'ad'];
  var STATUSES = ['ACTIVE', 'PAUSED'];
  var FINAL = ['Succeeded', 'Failed', 'Outcome unknown'];
  var LEVEL_WORD = { campaign: 'Campaign', adset: 'Ad set', ad: 'Ad' };
  var MAX_TARGETS = 20;

  function refuse(message) { var e = new Error(message); e.refused = true; return e; }

  // Зеркало серверной политики: что вообще можно просить у сервера.
  function assertAllowed(req) {
    if (!req || typeof req !== 'object') throw refuse('Empty command.');
    if (!Object.prototype.hasOwnProperty.call(COMMANDS, req.command)) {
      throw refuse('Only status, daily budget and bid commands are allowed.');
    }
    if (!Array.isArray(req.targets) || !req.targets.length) throw refuse('Pick at least one object.');
    if (req.targets.length > MAX_TARGETS) throw refuse('At most ' + MAX_TARGETS + ' objects per command.');
    req.targets.forEach(function (t) {
      if (!t || LEVELS.indexOf(t.level) < 0 || !/^[1-9][0-9]{4,}$/.test(String(t.id))) {
        throw refuse('Each target needs a level and a numeric id.');
      }
    });
    if (req.command === 'set_status') {
      if (STATUSES.indexOf(String(req.value)) < 0) throw refuse('Status must be ACTIVE or PAUSED.');
    } else if (!/^[0-9]{1,12}(\.[0-9]{1,2})?$/.test(String(req.value)) || !(Number(req.value) > 0)) {
      throw refuse('Enter a positive amount with at most two decimals.');
    }
    return true;
  }

  function newKey() {
    var bytes = new Uint8Array(12);
    if (root.crypto && root.crypto.getRandomValues) root.crypto.getRandomValues(bytes);
    else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    var hex = '';
    for (var j = 0; j < bytes.length; j++) hex += ('0' + bytes[j].toString(16)).slice(-2);
    return 'k_' + hex;
  }

  function post(url, body) {
    return root.fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                             body: JSON.stringify(body) }).then(readReply);
  }

  function readReply(resp) {
    return resp.json().catch(function () { return {}; }).then(function (data) {
      if (!resp.ok) {
        var e = new Error(data.error || ('Request failed (' + resp.status + ')'));
        e.code = data.code || '';
        e.status = resp.status;
        throw e;
      }
      return data;
    });
  }

  function preview(req) {
    try { assertAllowed(req); } catch (e) { return Promise.reject(e); }
    return post('/campaign-commands/preview', {
      account: req.account, actor: req.actor, command: req.command, value: req.value,
      targets: req.targets.map(function (t) { return { level: t.level, id: String(t.id) }; })
    });
  }

  // Ключ можно хранить у вызывающего: повтор с тем же ключом ничего не пишет второй раз.
  function execute(revisionId, key) {
    if (!revisionId) return Promise.reject(refuse('Preview the command first.'));
    return post('/campaign-commands/execute', { revision_id: revisionId, idempotency_key: key || newKey() });
  }

  function poll(opId, opts) {
    opts = opts || {};
    var started = Date.now();
    var timeout = opts.timeoutMs || 10 * 60 * 1000;
    var delay = opts.intervalMs || 1500;
    return new Promise(function (resolve, reject) {
      function tick() {
        root.fetch('/campaign-commands/' + encodeURIComponent(opId)).then(readReply).then(function (op) {
          if (opts.onUpdate) { try { opts.onUpdate(op); } catch (e) { /* экран не ломает опрос */ } }
          if (FINAL.indexOf(op.state) >= 0) return resolve(op);
          if (Date.now() - started > timeout) {
            return reject(new Error('Still running after ' + Math.round(timeout / 60000)
                                    + ' min. Check the history; nothing is retried.'));
          }
          delay = Math.min(delay * 1.3, 4000);
          setTimeout(tick, delay);
        }).catch(reject);
      }
      tick();
    });
  }

  // ── Модель диалога (чистая функция — её и тестируем) ──
  function titleFor(op) {
    var n = (op.affected || []).length;
    var what = { set_status: op.value === 'ACTIVE' ? 'Switch on' : 'Switch off',
                 set_budget: 'Change daily budget of', set_bid: 'Change bid of' }[op.command] || 'Change';
    return what + ' ' + n + ' object' + (n === 1 ? '' : 's');
  }

  function dialogModel(op) {
    var rows = (op.affected || []).map(function (a) {
      return { level: LEVEL_WORD[a.level] || a.level, name: a.name, before: a.before_display,
               after: a.after_display,
               notes: (a.constraints || []).map(function (c) { return c.text; }) };
    });
    var skipped = (op.skipped || []).map(function (s) {
      return { level: LEVEL_WORD[s.level] || s.level, name: s.name, reason: s.reason };
    });
    var warnings = [];
    rows.forEach(function (r) {
      r.notes.forEach(function (n) { warnings.push(r.name + ': ' + n + ' (the parent stays as it is)'); });
    });
    return { title: titleFor(op), account: op.account, currency: op.currency, rows: rows,
             skipped: skipped, warnings: warnings, canConfirm: rows.length > 0,
             confirmLabel: rows.length ? 'Confirm' : 'Nothing to change' };
  }

  function el(tag, text, cls) {
    var n = root.document.createElement(tag);
    if (text !== undefined) n.textContent = text;   // только textContent: имена объектов не разбираются как HTML
    if (cls) n.className = cls;
    return n;
  }

  function buildConfirmDialog(op) {
    var m = dialogModel(op);
    var dlg = el('dialog', undefined, 'acm-confirm');
    dlg.setAttribute('aria-label', m.title);
    dlg.style.cssText = 'max-width:640px;width:calc(100% - 32px);border:1px solid var(--border,#dfe3ea);'
      + 'border-radius:var(--radius-lg,10px);padding:var(--space-lg,16px);background:var(--bg-elev,#fff);'
      + 'color:var(--text,#1a1d23)';
    dlg.appendChild(el('h2', m.title));
    dlg.appendChild(el('p', m.account + ' · amounts in ' + m.currency, 'acm-sub'));
    if (m.rows.length) {
      var table = el('table', undefined, 'acm-diff');
      m.rows.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', r.level));
        tr.appendChild(el('td', r.name));
        tr.appendChild(el('td', r.before + ' → ' + r.after));
        table.appendChild(tr);
      });
      dlg.appendChild(table);
    }
    m.warnings.forEach(function (w) { dlg.appendChild(el('p', w, 'acm-warn')); });
    if (m.skipped.length) {
      dlg.appendChild(el('h3', 'Skipped'));
      var ul = el('ul');
      m.skipped.forEach(function (s) { ul.appendChild(el('li', s.level + ' ' + s.name + ': ' + s.reason)); });
      dlg.appendChild(ul);
    }
    var bar = el('div', undefined, 'acm-actions');
    var cancel = el('button', 'Cancel'); cancel.type = 'button'; cancel.setAttribute('data-acm', 'cancel');
    var ok = el('button', m.confirmLabel); ok.type = 'button'; ok.setAttribute('data-acm', 'confirm');
    ok.disabled = !m.canConfirm;
    bar.appendChild(cancel); bar.appendChild(ok);
    dlg.appendChild(bar);
    return { dialog: dlg, confirmButton: ok, cancelButton: cancel, model: m };
  }

  // Диалог: «Confirm» — единственный путь к записи; закрытие/Cancel — отмена.
  function confirm(op) {
    return new Promise(function (resolve) {
      var built = buildConfirmDialog(op);
      var dlg = built.dialog;
      function done(value) {
        try { dlg.close(); } catch (e) { /* уже закрыт */ }
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(value);
      }
      built.confirmButton.addEventListener('click', function () { done(true); });
      built.cancelButton.addEventListener('click', function () { done(false); });
      dlg.addEventListener('cancel', function () { done(false); });
      root.document.body.appendChild(dlg);
      dlg.showModal();
      built.cancelButton.focus();
    });
  }

  // Всё подряд. Запись возможна только после «Confirm» человека.
  function run(req, opts) {
    opts = opts || {};
    return preview(req).then(function (op) {
      return confirm(op).then(function (yes) {
        if (!yes) return { state: 'Cancelled', op_id: op.op_id, cancelled: true };
        return execute(op.revision_id, opts.idempotencyKey).then(function (started) {
          return poll(started.op_id, opts);
        });
      });
    });
  }

  root.acmCommands = {
    preview: preview, execute: execute, poll: poll, confirm: confirm, run: run,
    buildConfirmDialog: buildConfirmDialog, dialogModel: dialogModel, assertAllowed: assertAllowed,
    newKey: newKey, FINAL_STATES: FINAL.slice()
  };
})(typeof window !== 'undefined' ? window : globalThis);
