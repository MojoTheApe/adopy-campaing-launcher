/* ACL-1812: тёплые сессии — состояние профиля в строке аккаунта, «Open profiles for today»
   и «Close profiles». Подключается одним тегом из report.html; всё остальное (стили, кнопки)
   рисует этот файл, чтобы не трогать таблицу, которую правят другие.

   Правила:
   - Состояние строки берётся из GET /warm/state: реестр и процессы на машине, ни одного
     вызова к Meta. Запрашивается при загрузке страницы, после подмены фрагмента (htmx) и
     после своих действий. Таймеров нет; опрос задания «Open» идёт только пока оно работает
     и тоже локальный.
   - Открытие списка ничего не читает.
   - Close — только с подтверждением, и закрывает дешборд лишь профили, которые поднял сам. */
(function () {
  'use strict';
  if (window.__aclWarmSessions) return;
  window.__aclWarmSessions = true;

  var STYLE = [
    '.warm-chip{display:inline-flex;align-items:center;gap:.3rem;margin-left:.45rem;font-size:.68rem;font-weight:400;white-space:nowrap;vertical-align:middle}',
    '.warm-chip .warm-state{padding:.05rem .4rem;border-radius:999px;border:1px solid var(--border,#8884);color:var(--muted,#888)}',
    '.warm-chip[data-state="warm"] .warm-state{color:var(--ok,#2a9d4b);border-color:currentColor}',
    '.warm-chip[data-state="logged_out"] .warm-state,.warm-chip[data-state="by_hand"] .warm-state{color:var(--bad,#c0392b);border-color:currentColor}',
    '.warm-chip input{margin:0;cursor:pointer}',
    '.warm-chip button,.warm-bar button{font:inherit;font-size:.68rem;padding:.05rem .4rem;cursor:pointer}',
    '.warm-chip button:disabled,.warm-bar button:disabled{opacity:.6;cursor:default}',
    '.warm-bar{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;justify-content:flex-end;padding:.3rem .85rem 0;font-size:.72rem}',
    '.warm-msg{color:var(--muted,#888)}',
    '.warm-msg.warm-bad{color:var(--bad,#c0392b)}'
  ].join('\n');

  var busy = false;

  function addStyle() {
    var st = document.createElement('style');
    st.textContent = STYLE;
    document.head.appendChild(st);
  }

  function grids() { return Array.prototype.slice.call(document.querySelectorAll('table.amap-grid')); }

  function rowsOf(grid) {
    var out = [];
    grid.querySelectorAll('tbody > tr').forEach(function (tr) {
      var live = tr.querySelector('td.amap-list[data-account], td.amap-live[data-account]');
      var cell = tr.querySelector('td.amap-acc');
      if (live && cell) out.push({ tr: tr, cell: cell, account: live.getAttribute('data-account') });
    });
    return out;
  }

  function chipOf(row) {
    var chip = row.cell.querySelector('.warm-chip');
    if (chip) return chip;
    chip = document.createElement('span');
    chip.className = 'warm-chip';
    chip.setAttribute('data-account', row.account);
    var pick = document.createElement('input');
    pick.type = 'checkbox';
    pick.className = 'warm-pick';
    pick.title = 'Select for Open profiles for today / Close profiles';
    var state = document.createElement('span');
    state.className = 'warm-state';
    var act = document.createElement('button');
    act.type = 'button';
    act.className = 'warm-act';
    act.hidden = true;
    act.addEventListener('click', function () { onRowAction(chip, act); });
    chip.appendChild(pick);
    chip.appendChild(state);
    chip.appendChild(act);
    row.cell.appendChild(chip);
    return chip;
  }

  function barOf(grid) {
    var bar = grid.previousElementSibling;
    if (bar && !(bar.classList && bar.classList.contains('warm-bar'))) bar = null;
    if (bar) return bar;
    bar = document.createElement('div');
    bar.className = 'warm-bar';
    var open = document.createElement('button');
    open.type = 'button';
    open.className = 'warm-open-all';
    open.textContent = 'Open profiles for today';
    open.title = 'Starts the Multilogin profiles of the ticked accounts (all accounts here when none are ticked) with automation, one after another, and keeps them open';
    open.addEventListener('click', function () { openProfiles(grid); });
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'warm-close-all';
    close.textContent = 'Close profiles';
    close.title = 'Closes the ticked profiles that this dashboard opened. Anything opened another way is left alone.';
    close.addEventListener('click', function () { closeProfiles(grid); });
    var msg = document.createElement('span');
    msg.className = 'warm-msg';
    bar.appendChild(open);
    bar.appendChild(close);
    bar.appendChild(msg);
    grid.parentNode.insertBefore(bar, grid);
    return bar;
  }

  function say(grid, text, bad) {
    var m = barOf(grid).querySelector('.warm-msg');
    m.textContent = text || '';
    m.classList.toggle('warm-bad', !!bad);
  }

  function picked(grid) {
    var rows = rowsOf(grid);
    var ticked = rows.filter(function (r) {
      var p = r.cell.querySelector('.warm-pick');
      return p && p.checked;
    });
    return { rows: rows, ticked: ticked };
  }

  function paint(grid, data) {
    var accounts = (data && data.accounts) || {};
    rowsOf(grid).forEach(function (row) {
      var info = accounts[row.account];
      var chip = chipOf(row);
      if (!info) { chip.hidden = true; return; }
      chip.hidden = false;
      chip.setAttribute('data-state', info.state);
      var st = chip.querySelector('.warm-state');
      st.textContent = info.text;
      st.title = info.title || '';
      var act = chip.querySelector('.warm-act');
      // Одна кнопка на строку: открыть закрытый / закрыть поднятый дешбордом.
      if (info.state === 'closed' || info.state === 'open') {
        act.hidden = false; act.textContent = 'Open'; act.setAttribute('data-do', 'open');
        act.title = 'Open this profile with automation and keep it open';
      } else if (info.closable && (info.state === 'warm' || info.state === 'logged_out')) {
        act.hidden = false; act.textContent = 'Close'; act.setAttribute('data-do', 'close');
        act.title = 'Close this profile (opened by the dashboard)';
      } else {
        act.hidden = true; act.removeAttribute('data-do');
      }
      act.disabled = busy || !!info.busy;
    });
    var bar = barOf(grid);
    bar.hidden = false;
  }

  function hideAll() {
    grids().forEach(function (g) {
      var bar = g.previousElementSibling;
      if (bar && bar.classList && bar.classList.contains('warm-bar')) bar.hidden = true;
      rowsOf(g).forEach(function (r) {
        var c = r.cell.querySelector('.warm-chip');
        if (c) c.hidden = true;
      });
    });
  }

  function refresh() {
    var gs = grids();
    if (!gs.length) return Promise.resolve();
    var all = [];
    gs.forEach(function (g) { rowsOf(g).forEach(function (r) { all.push(r.account); }); });
    if (!all.length) return Promise.resolve();
    var q = all.map(function (a) { return 'account=' + encodeURIComponent(a); }).join('&');
    return fetch('/warm/state?' + q, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.available) { hideAll(); return; }
        gs.forEach(function (g) {
          paint(g, data);
          var acts = !!data.actions;
          var bar = barOf(g);
          bar.querySelectorAll('button').forEach(function (b) { b.disabled = busy || !acts; });
          if (!acts && data.reason) say(g, data.reason, false);
        });
      })
      .catch(function () { /* подсказка не должна ломать страницу */ });
  }

  function setBusy(v) {
    busy = v;
    document.querySelectorAll('.warm-bar button, .warm-chip .warm-act').forEach(function (b) { b.disabled = v; });
  }

  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); });
  }

  // ACL-1826: после потери общей сессии Multilogin слота B остальные B-профили пачки пропущены
  // сервером с той же причиной: одна строка на пачку и Retry только для них.
  var BATCH_STOPS = { mlx_session_lost: 1, mlx_token_expired: 1, mlx_token_workspace: 1 };

  function summarize(results) {
    var ok = 0, bad = [], stopped = [], stopText = '';
    Object.keys(results || {}).forEach(function (k) {
      var r = results[k];
      if (r.state === 'ok' || r.ok) ok++;
      else if (BATCH_STOPS[r.code]) { stopped.push(k); stopText = stopText || r.message || ''; }
      else if (r.state !== 'waiting') bad.push(k + ': ' + (r.message || r.code || 'failed'));
    });
    return { ok: ok, bad: bad, stopped: stopped, stopText: stopText };
  }

  function sayStopped(grid, s) {
    if (!s.stopped.length) return;
    var m = barOf(grid).querySelector('.warm-msg');
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'warm-retry';
    retry.textContent = 'Retry';
    retry.addEventListener('click', function () { runOpen(grid, s.stopped); });
    m.appendChild(document.createTextNode(' ' + s.stopText + ' (' + s.stopped.length + ' not opened) '));
    m.appendChild(retry);
    m.classList.add('warm-bad');
  }

  function runOpen(grid, labels) {
    if (!labels.length) { say(grid, 'Nothing to open.', true); return; }
    if (busy) return;   // второе «да» из очереди окон, пока идёт первое открытие
    setBusy(true);
    say(grid, 'Opening ' + labels.length + ' profile(s) one after another…', false);
    post('/warm/open', { accounts: labels }).then(function (res) {
      if (res.status >= 400 || !res.body.job_id) {
        say(grid, res.body.error || 'Could not start opening profiles.', true);
        setBusy(false);
        return;
      }
      pollJob(grid, res.body.job_id, labels.length);
    }).catch(function () { say(grid, 'Could not reach the dashboard.', true); setBusy(false); });
  }

  function pollJob(grid, jobId, total) {
    fetch('/warm/job/' + encodeURIComponent(jobId), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (job) {
        var s = summarize(job.results);
        if (job.state === 'running') {
          say(grid, 'Opening profiles: ' + s.ok + ' of ' + total + ' done…', false);
          setTimeout(function () { pollJob(grid, jobId, total); }, 2000);
          return;
        }
        setBusy(false);
        say(grid, s.ok + ' of ' + total + ' ready.' + (s.bad.length ? ' ' + s.bad.join(' · ') : ''),
            s.bad.length > 0 || s.stopped.length > 0);
        sayStopped(grid, s);
        refresh();
      })
      .catch(function () { setBusy(false); say(grid, 'Lost the opening job; check the account rows.', true); });
  }

  function openProfiles(grid) {
    var p = picked(grid);
    var rows = p.ticked.length ? p.ticked : p.rows;
    var accounts = rows.map(function (r) { return r.account; });
    if (rows.length <= 3) { runOpen(grid, accounts); return; }
    window.aclConfirm('They open one after another and stay open.',
      { title: 'Start ' + rows.length + ' Multilogin profiles?', okLabel: 'Start' })
      .then(function (yes) { if (yes) runOpen(grid, accounts); });
  }

  function runClose(grid, labels) {
    if (!labels.length) { say(grid, 'Tick the accounts whose profiles to close.', true); return; }
    window.aclConfirm('Only profiles that this dashboard opened are closed, '
      + 'gently, through Multilogin. Anything opened another way stays open. A closed profile can be '
      + 'started again after about 3 minutes.',
      { title: 'Close ' + labels.length + ' profile(s)?', okLabel: 'Close profiles' })
      .then(function (ok) { if (ok) closeNow(grid, labels); });
  }

  function closeNow(grid, labels) {
    if (busy) return;   // пока окно было открыто, могло начаться другое действие
    setBusy(true);
    post('/warm/close', { accounts: labels, confirm: true }).then(function (res) {
      setBusy(false);
      if (res.status >= 400) { say(grid, res.body.error || 'Could not close profiles.', true); return; }
      var s = summarize(res.body.results);
      say(grid, s.ok + ' closed.' + (s.bad.length ? ' ' + s.bad.join(' · ') : ''), s.bad.length > 0);
      refresh();
    }).catch(function () { setBusy(false); say(grid, 'Could not reach the dashboard.', true); });
  }

  function closeProfiles(grid) {
    runClose(grid, picked(grid).ticked.map(function (r) { return r.account; }));
  }

  function onRowAction(chip, btn) {
    var grid = chip.closest('table.amap-grid');
    var account = chip.getAttribute('data-account');
    if (btn.getAttribute('data-do') === 'open') runOpen(grid, [account]);
    else if (btn.getAttribute('data-do') === 'close') runClose(grid, [account]);
  }

  addStyle();
  document.addEventListener('DOMContentLoaded', refresh);
  if (document.readyState !== 'loading') refresh();
  var settleTimer = null;
  document.addEventListener('htmx:afterSettle', function () {
    // Одно обновление на пачку подмен: каждое — обход процессов на машине.
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(function () { settleTimer = null; refresh(); }, 800);
  });
  window.aclWarmRefresh = refresh;
})();
