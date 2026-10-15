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

  /* ACL-1826: понятные тексты устойчивых кодов Multilogin (те же, что отдаёт сервер). */
  var FRIENDLY = {
    mlx_session_lost: 'Multilogin on this computer lost the shared KLD sign-in (it was used on another computer). Sign in again in the Multilogin app here, then press Retry.',
    proxy_auth: 'The proxy login for this profile is wrong or expired — update it in Multilogin'
  };

  function readReply(resp) {
    return resp.json().catch(function () { return {}; }).then(function (data) {
      if (!resp.ok) {
        var e = new Error(FRIENDLY[data.code] || data.error || ('Request failed (' + resp.status + ')'));
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
    // Родитель, который включается этой же командой, не предупреждение.
    var switchedOn = {};
    if (op.command === 'set_status' && op.value === 'ACTIVE') {
      (op.affected || []).forEach(function (a) { switchedOn[a.id] = true; });
    }
    var rows = (op.affected || []).map(function (a) {
      return { level: LEVEL_WORD[a.level] || a.level, name: a.name, before: a.before_display,
               after: a.after_display, strategy: a.strategy || '',
               notes: (a.constraints || []).filter(function (c) { return !(c.id && switchedOn[c.id]); })
                 .map(function (c) { return c.text; }) };
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

  // Стили диалогов — один раз, на токенах дешборда (с запасными значениями).
  function ensureStyle() {
    var doc = root.document;
    if (!doc || !doc.head || (doc.getElementById && doc.getElementById('acm-style'))) return;
    var st = doc.createElement('style');
    st.id = 'acm-style';
    st.textContent =
      'dialog.acm-confirm h2,dialog.acm-edit h2{margin:0 0 .25rem;font-size:1.05rem}' +
      'dialog.acm-confirm h3{margin:.75rem 0 .25rem;font-size:.85rem}' +
      '.acm-sub{margin:0 0 .5rem;font-size:.8rem;color:var(--muted,#647084)}' +
      '.acm-warn{margin:.35rem 0;font-size:.8rem;color:var(--warn,#d97706)}' +
      '.acm-diff{width:100%;border-collapse:collapse;font-size:.85rem}' +
      '.acm-diff td{padding:.3rem .4rem;border-bottom:1px solid var(--border,#dfe3ea)}' +
      '.acm-actions{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1rem}' +
      '.acm-actions button{min-height:32px;padding:0 .9rem;border-radius:6px;border:1px solid var(--border-strong,#c8ced8);' +
      'background:var(--bg-elev,#fff);color:var(--text,#1a1d23);cursor:pointer}' +
      '.acm-actions button[data-acm="confirm"],.acm-actions button[data-acm="review"]{background:var(--accent,#2563eb);' +
      'border-color:var(--accent,#2563eb);color:var(--on-accent,#fff)}' +
      '.acm-actions button:disabled{opacity:.5;cursor:not-allowed}' +
      'dialog.acm-edit label{display:flex;flex-direction:column;gap:.25rem;font-size:.85rem}' +
      'dialog.acm-edit input{min-height:32px;padding:0 .5rem}';
    doc.head.appendChild(st);
  }

  function el(tag, text, cls) {
    var n = root.document.createElement(tag);
    if (text !== undefined) n.textContent = text;   // только textContent: имена объектов не разбираются как HTML
    if (cls) n.className = cls;
    return n;
  }

  function buildConfirmDialog(op) {
    var m = dialogModel(op);
    ensureStyle();
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
        tr.appendChild(el('td', (r.strategy ? r.strategy + ': ' : '') + r.before + ' → ' + r.after));
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

  // ── Таблица Home (ACL-1798 отдаёт строки и data-атрибуты, ACL-1800 — команды) ──
  // Всё решение «можно ли» здесь, чтобы его проверять без браузера. Сервер всё равно
  // перепроверяет в превью и перед записью; страница только не предлагает лишнего.
  var CAP_STRATEGIES = ['LOWEST_COST_WITH_BID_CAP', 'COST_CAP'];
  var AUTOMATIC = 'Automatic · no manual bid';
  var ZERO_DECIMAL = ['CLP', 'COP', 'CRC', 'HUF', 'ISK', 'IDR', 'JPY', 'KRW', 'PYG', 'TWD', 'VND'];

  function availability() {
    return root.fetch('/campaign-commands/availability').then(readReply).catch(function (e) {
      return { available: false, reason: String((e && e.message) || e), max_targets: 0 };
    });
  }

  function budgetState(row) {
    if (row.level === 'ad') return { enabled: false, reason: 'Ads have no budget' };
    var b = row.budget || { kind: 'none' };
    if (b.kind === 'daily') return { enabled: true, reason: '' };
    if (b.kind === 'ad_sets') {   // у кампании бюджета нет: показываем бюджеты её ad set
      return { enabled: true, reason: '', openKids: true, label: 'View ad set budgets' };
    }
    if (b.kind === 'inherited') return { enabled: false, reason: 'Budget is set on the campaign' };
    if (b.kind === 'lifetime') return { enabled: false, reason: 'Lifetime budget is not supported here' };
    if (b.kind === 'unit_unknown') return { enabled: false, reason: 'The budget unit of this currency is not confirmed' };
    return { enabled: false, reason: 'No daily budget on this object' };
  }

  function bidState(row) {
    if (row.level === 'ad') return { enabled: false, reason: 'Managed by ad set' };
    if (row.level === 'campaign') return { enabled: false, reason: 'A manual bid is set on ad sets' };
    var raw = row.bid_strategy_raw || '';
    if (CAP_STRATEGIES.indexOf(raw) >= 0) return { enabled: true, reason: '' };
    if (raw === 'LOWEST_COST_WITHOUT_CAP') return { enabled: false, reason: AUTOMATIC };
    if (!raw) return { enabled: false, reason: 'The bid strategy is set on the campaign' };
    return { enabled: false, reason: 'No manual bid for this strategy' };
  }

  // Состояние кнопки команды: включена ли и почему нет. `avail` — ответ /campaign-commands/availability.
  function controlState(row, action, avail) {
    if (!avail || !avail.available) {
      return { enabled: false, reason: 'Commands unavailable' + (avail && avail.reason ? ': ' + avail.reason : '') };
    }
    if (row.busy) return { enabled: false, reason: 'A command for this object is running' };
    if (action === 'status') {
      if (row.status === 'on' || row.status === 'off') return { enabled: true, reason: '' };
      return { enabled: false, reason: 'Status ' + (row.status_raw || 'unknown') + ' cannot be changed here' };
    }
    if (action === 'budget') return budgetState(row);
    if (action === 'bid-down' || action === 'bid-up') return bidState(row);
    return { enabled: false, reason: '' };
  }

  // Что просить у сервера при щелчке на переключателе: только по подтверждённому состоянию строки.
  function statusTarget(row) { return row.status === 'on' ? 'PAUSED' : 'ACTIVE'; }

  function isZeroDecimal(currency) { return ZERO_DECIMAL.indexOf(String(currency || '').toUpperCase()) >= 0; }

  // Подсказка для − / +: 5% от текущей ставки, округлено до единицы валюты.
  function stepValue(current, dir, currency) {
    if (current == null || !(current > 0)) return '';
    var next = current * (dir < 0 ? 0.95 : 1.05);
    if (isZeroDecimal(currency)) return String(Math.max(1, Math.round(next)));
    return String(Math.max(0.01, Math.round(next * 100) / 100).toFixed(2));
  }

  function actor() {
    var sel = root.document && root.document.getElementById && root.document.getElementById('buyer-select');
    return (sel && sel.value) || '';
  }

  // Выбранные строки → одна команда: один аккаунт и не больше потолка вызовов.
  function batchPlan(rows, avail) {
    if (!avail || !avail.available) {
      return { ok: false, reason: 'Commands unavailable' + (avail && avail.reason ? ': ' + avail.reason : '') };
    }
    var total = rows.length;
    rows = rows.filter(function (r) { return !r.busy; });   // строка с идущей командой в пакет не входит
    if (!total) return { ok: false, reason: 'Select rows first' };
    if (!rows.length) return { ok: false, reason: 'The selected rows are busy with another command' };
    var accounts = {};
    rows.forEach(function (r) { accounts[r.account] = true; });
    if (Object.keys(accounts).length > 1) return { ok: false, reason: 'Select rows from one account' };
    var cap = Math.min(MAX_TARGETS, avail.max_targets || MAX_TARGETS);
    if (rows.length > cap) {
      return { ok: false, reason: 'At most ' + cap + ' objects per command with the current call limit' };
    }
    return { ok: true, account: rows[0].account, rows: rows, skippedBusy: total - rows.length,
             targets: rows.map(function (r) { return { level: r.level, id: String(r.id) }; }) };
  }

  function skippedText(op) {
    var s = (op.skipped || []).map(function (x) { return x.name + ': ' + x.reason; });
    return s.length ? s.join('; ') : 'Nothing to change';
  }

  // Итог операции → то, что показывает строка. Применять к строке можно ТОЛЬКО `applied`
  // (объекты, подтверждённые чтением Meta); переключатель не меняется заранее.
  function classify(op) {
    var applied = (op.affected || []).filter(function (a) { return a.result === 'saved'; })
      .map(function (a) { return { id: a.id, level: a.level, field: a.field, saved: a.saved, unit: op.unit || 100 }; });
    var notes = {};
    (op.affected || []).forEach(function (a) { if (a.result !== 'saved') notes[a.id] = a.note || ''; });
    var kind = op.state === 'Succeeded' ? 'confirmed' : op.state === 'Outcome unknown' ? 'unknown' : 'failed';
    return { kind: kind, op: op, applied: applied, notes: notes, message: op.message || '' };
  }

  function applySaved(row, item) {
    if (item.field === 'status') {
      row.status = item.saved === 'ACTIVE' ? 'on' : item.saved === 'PAUSED' ? 'off' : 'unknown';
      row.status_raw = item.saved;
    } else if (item.field === 'daily_budget') {
      row.budget = { kind: 'daily', amount: item.saved / item.unit };
    } else if (item.field === 'bid_amount') {
      row.bid_amount = item.saved / item.unit;
    }
    return row;
  }

  var UNKNOWN_TEXT = 'Outcome unknown — check the history and Read campaigns';
  var CHECK_TEXT = 'Outcome unknown — press Read campaigns to check';

  // Что показать в строках по итогу и что в них изменить. Строка меняется ТОЛЬКО объектами,
  // которые операция подтвердила чтением Meta (`res.applied`); при отказе и неизвестном
  // исходе переключатель, бюджет и ставка остаются прежними. Возвращает пометки по ключам строк.
  function settle(rows, res) {
    var byId = {};
    rows.forEach(function (r) { byId[r.id] = r; });
    res.applied.forEach(function (item) { if (byId[item.id]) applySaved(byId[item.id], item); });
    var notes = {};
    if (res.kind === 'cancelled' || res.kind === 'nothing') return notes;
    rows.forEach(function (r) {
      var saved = res.applied.some(function (a) { return a.id === r.id; });
      if (saved) notes[r.key] = { text: 'Confirmed', tone: 'ok' };
      else if (res.kind === 'unknown') notes[r.key] = { text: CHECK_TEXT, tone: 'warn' };
      else notes[r.key] = { text: 'Failed — previous state kept' + (res.notes[r.id] ? ': ' + res.notes[r.id] : ''), tone: 'bad' };
    });
    return notes;
  }

  // Превью → диалог → запись → опрос. Диалог показывается и когда менять нечего — чтобы были видны причины.
  // Ошибка ДО принятия записи сервером («не отправлено») уходит наружу как есть; сбой сети или
  // срок опроса ПОСЛЕ неё — «исход неизвестен», а не «не отправлено»: запись могла дойти до Meta.
  function flow(req, hooks) {
    hooks = hooks || {};
    var ask = hooks.confirm || confirm;
    var say = hooks.onState || function () {};
    say('Previewing…');
    return preview(req).then(function (op) {
      return ask(op).then(function (yes) {
        if (!yes) return { kind: 'cancelled', op: op, applied: [], notes: {}, message: 'Cancelled' };
        if (!(op.affected || []).length) return { kind: 'nothing', op: op, applied: [], notes: {}, message: skippedText(op) };
        say('Running…');
        var unknown = function () {
          return { kind: 'unknown', op: op, applied: [], notes: {}, message: UNKNOWN_TEXT };
        };
        return execute(op.revision_id, hooks.key).then(function (started) {
          return poll(started.op_id, { intervalMs: hooks.intervalMs, timeoutMs: hooks.timeoutMs, onUpdate: function (o) {
            say(o.state === 'Verifying' ? 'Verifying…' : 'Running…');
          } }).then(classify, unknown);
        }, function (e) {
          if (e && e.status) throw e;          // сервер отказал: запись не принята
          return unknown();                    // ответа нет: запись могла дойти
        });
      });
    });
  }

  // Поле ввода суммы в диалоге: проверка на странице, сервер проверит ещё раз.
  function editValue(opts) {
    return new Promise(function (resolve) {
      ensureStyle();
      var dlg = el('dialog', undefined, 'acm-edit');
      dlg.setAttribute('aria-label', opts.title);
      dlg.style.cssText = 'max-width:420px;width:calc(100% - 32px);border:1px solid var(--border,#dfe3ea);'
        + 'border-radius:var(--radius-lg,10px);padding:var(--space-lg,16px);background:var(--bg-elev,#fff);'
        + 'color:var(--text,#1a1d23)';
      dlg.appendChild(el('h2', opts.title));
      var label = el('label', opts.label + ' (' + opts.currency + ')');
      var input = el('input');
      input.type = 'text';
      input.setAttribute('inputmode', 'decimal');
      input.setAttribute('data-acm', 'value');
      input.value = opts.value || '';
      label.appendChild(input);
      dlg.appendChild(label);
      if (opts.hint) dlg.appendChild(el('p', opts.hint, 'acm-sub'));
      var err = el('p', '', 'acm-warn');
      err.setAttribute('role', 'alert');
      dlg.appendChild(err);
      var bar = el('div', undefined, 'acm-actions');
      var cancel = el('button', 'Cancel'); cancel.type = 'button'; cancel.setAttribute('data-acm', 'cancel');
      var ok = el('button', 'Review'); ok.type = 'button'; ok.setAttribute('data-acm', 'review');
      bar.appendChild(cancel); bar.appendChild(ok); dlg.appendChild(bar);
      function done(v) {
        try { dlg.close(); } catch (e) { /* уже закрыт */ }
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        resolve(v);
      }
      ok.addEventListener('click', function () {
        var v = String(input.value).trim();
        var zero = isZeroDecimal(opts.currency);
        var good = zero ? /^[1-9][0-9]{0,11}$/.test(v) : /^[0-9]{1,12}(\.[0-9]{1,2})?$/.test(v) && Number(v) > 0;
        if (!good) { err.textContent = zero ? 'Enter a whole positive amount.' : 'Enter a positive amount with at most two decimals.'; return; }
        done(v);
      });
      cancel.addEventListener('click', function () { done(null); });
      dlg.addEventListener('cancel', function () { done(null); });
      root.document.body.appendChild(dlg);
      dlg.showModal();
      input.focus();
    });
  }

  // ── Короткие сообщения для человека (одно место на страницу) ──
  // Ключ — код отказа сервера (`code` в ответе: open_by_hand, stale, not_loopback, rpa_backend, logged_out,
  // page_unresponsive, restart_cooldown …), а где кода нет — узнаваемый кусок текста. Сырой текст
  // не теряется: его показывает «Details». Текст, который сервер уже написал по-человечески
  // (open_by_hand, restart_cooldown, not_loopback, rpa), отдаётся как есть.
  var FRIENDLY = {
    mlx: "Multilogin isn't running — start it and try again",
    hand: 'Profile is open in Multilogin by hand — close it there, wait about 3 minutes, then press Open profile',
    closing: 'Profile is closing — try again in a few seconds',
    loggedOut: 'Logged out in this profile — log in inside Multilogin, then retry',
    checkpoint: 'Facebook checkpoint — resolve it inside the profile',
    unresponsive: 'Ads Manager stopped answering — try again in a few minutes',
    permission: 'Multilogin refused to start this profile (permission) — another user may be signed in to the same Multilogin account',
    stale: 'Changed since the preview — review again',
    unknown: 'Something went wrong — see Details'
  };
  var OWN_TEXT_CODES = ['restart_cooldown', 'not_loopback', 'rpa_backend', 'rpa'];   // текст сервера уже человеческий (с оставшимся временем)
  var HAND_RE = /no automation port|opened (?:in multilogin |in mlx desktop )?by hand/i;
  var CLOSING_RE = /profile is closing|closing or starting/i;

  function friendly(text, code) {
    text = String(text == null ? '' : text); code = String(code || '');
    if (OWN_TEXT_CODES.indexOf(code) >= 0 && text) return text.replace(/^Not sent:\s*/, '');
    if (code === 'open_by_hand' || code === 'no_port' || HAND_RE.test(text)) return FRIENDLY.hand;
    if (code === 'profile_closing' || CLOSING_RE.test(text)) return FRIENDLY.closing;
    if (/\b403\b/.test(text) && /permission|restricted|forbidden/i.test(text)) return FRIENDLY.permission;
    if (/launcher\.mlx\.yt|port\W{0,3}45001|winerror\W{0,3}10061/i.test(text)) return FRIENDLY.mlx;
    if (code === 'logged_out' || /logged[ _-]?out/i.test(text)) return FRIENDLY.loggedOut;
    if (/checkpoint/i.test(text)) return FRIENDLY.checkpoint;
    if (code === 'page_unresponsive' || /stopped answering|page[ _]unresponsive/i.test(text)) return FRIENDLY.unresponsive;
    if (code === 'stale' || /changed since|since the preview/i.test(text)) return FRIENDLY.stale;
    if (/listens on this computer alone|\(127\.0\.0\.1\)/.test(text) || /ACL_FB_BACKEND=api/.test(text)) return text.replace(/^Not sent:\s*/, '');
    return null;
  }

  // Причина про профиль (а не про саму команду): если строка аккаунта уже показывает такую же, строка объекта не повторяет текст.
  function profileCause(text, code) {
    text = String(text == null ? '' : text); code = String(code || '');
    if (code === 'open_by_hand' || code === 'no_port' || HAND_RE.test(text)) return 'hand';
    if (code === 'profile_closing' || CLOSING_RE.test(text)) return 'closing';
    if (code === 'restart_cooldown' || /can be started again|restart(?:ed)? .*(?:min|cooldown)/i.test(text)) return 'cooldown';
    if (code === 'logged_out' || /logged[ _-]?out/i.test(text)) return 'logged_out';
    if (/launcher\.mlx\.yt|port\W{0,3}45001|winerror\W{0,3}10061/i.test(text)) return 'mlx';
    return '';
  }

  // Итог для строки: {text: короткая строка, raw: полный текст для Details | null, tone}.
  // Хорошее и «идёт» (ok, busy) остаётся как есть; плохое и предупреждение — коротко, с деталями.
  var LONG = 80;
  function explain(text, tone, code) {
    var raw = String(text == null ? '' : text);
    if (tone !== 'bad' && tone !== 'warn') return { text: raw, raw: null, tone: tone };
    var cause = profileCause(raw, code);
    var out = explainOne(raw, tone, code);
    out.cause = cause;
    return out;
  }
  function explainOne(raw, tone, code) {
    var f = friendly(raw, code);
    var i = raw.indexOf(': '), pre = i > 0 ? raw.slice(0, i) : '', cause = i > 0 ? raw.slice(i + 2) : '';
    if (f === null && cause) f = friendly(cause, code);
    if (f !== null) return { text: f, raw: f === raw ? null : raw, tone: tone };
    if (pre === 'Not sent' || (!pre && raw.length > LONG)) return { text: FRIENDLY.unknown, raw: raw, tone: tone };
    if (raw.length > LONG && pre) return { text: pre, raw: raw, tone: tone };
    return { text: raw, raw: null, tone: tone };
  }

  root.acmCommands = {
    preview: preview, execute: execute, poll: poll, confirm: confirm, run: run,
    buildConfirmDialog: buildConfirmDialog, dialogModel: dialogModel, assertAllowed: assertAllowed,
    newKey: newKey, FINAL_STATES: FINAL.slice(), friendly: friendly, explain: explain, profileCause: profileCause, FRIENDLY: FRIENDLY,
    home: { availability: availability, controlState: controlState, statusTarget: statusTarget,
            stepValue: stepValue, batchPlan: batchPlan, flow: flow, classify: classify,
            applySaved: applySaved, editValue: editValue, actor: actor, AUTOMATIC: AUTOMATIC,
            settle: settle, FRIENDLY: FRIENDLY, UNKNOWN_TEXT: UNKNOWN_TEXT, CHECK_TEXT: CHECK_TEXT }
  };
})(typeof window !== 'undefined' ? window : globalThis);
