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
    '.warm-msg.warm-bad{color:var(--bad,#c0392b)}',
    '.aov-acts{display:flex;flex-direction:column;align-items:flex-start;gap:.3rem;flex:0 0 auto}',
    '.aov-pop .aov-acts{margin:0 0 .5rem}',
    '.aov-open,.aov-takeout{padding:0 .5rem;min-height:24px}',
    '.aov-open:disabled,.aov-takeout:disabled{opacity:.6;cursor:default}',
    '.aov-acts .acl-inline-err{display:block;color:var(--bad,#c0392b);font-size:.72rem}',
    '.aov-open-msg{color:var(--adopy-text-muted,#888)}',
    '.aov-open-msg.warm-bad{color:var(--bad,#c0392b)}'
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
    var policy = document.createElement('label');
    var automatic = document.createElement('input');
    automatic.type = 'checkbox';
    automatic.className = 'warm-auto-reopen';
    policy.appendChild(automatic);
    policy.appendChild(document.createTextNode(' Reopen hand-opened profiles automatically on this computer'));
    bar.appendChild(policy);
    fetch('/warm/settings', {credentials: 'same-origin'}).then(function (r) { return r.json(); })
      .then(function (v) { automatic.checked = v.auto_reopen_manual === true; }).catch(function () {});
    automatic.addEventListener('change', function () {
      var wanted = automatic.checked;
      var confirmed = wanted ? window.aclConfirm('Allow this dashboard to close a hand-opened profile and reopen it for automation before an action?',
        {title: 'Automatic profile recovery', okLabel: 'Allow on this computer'}) : Promise.resolve(true);
      confirmed.then(function (yes) {
        if (!yes) { automatic.checked = false; return; }
        return post('/warm/settings', {auto_reopen_manual: wanted}).then(function (r) {
          if (r.status >= 400) { automatic.checked = !wanted; say(grid, r.body.error || 'Could not save connection settings.', true); }
          else document.querySelectorAll('.warm-auto-reopen').forEach(function (box) { box.checked = wanted; });
        }).catch(function () { automatic.checked = !wanted; say(grid, 'Could not save connection settings.', true); });
      });
    });
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
    homePaint();
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

  function runOpen(grid, labels, confirmedReopen) {
    if (!labels.length) { say(grid, 'Nothing to open.', true); return; }
    if (busy) return;   // второе «да» из очереди окон, пока идёт первое открытие
    setBusy(true);
    say(grid, 'Opening ' + labels.length + ' profile(s) one after another…', false);
    post('/warm/open', { accounts: labels, confirm_reopen: confirmedReopen === true }).then(function (res) {
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
        // ACL-2419: после Open идёт одно статус-чтение через очередь; его исход видно в строке аккаунта.
        var reads = Object.keys(job.results || {}).map(function (a) { return job.results[a].status_read; });
        var statusNote = reads.indexOf('queued') >= 0 ? ' Opened. Checking the account status…'
          : (reads.indexOf('skipped') >= 0 ? ' Opened. The status was not read now; it updates with the next list read.' : '');
        say(grid, s.ok + ' of ' + total + ' ready.' + statusNote + (s.bad.length ? ' ' + s.bad.join(' · ') : ''),
            s.bad.length > 0 || s.stopped.length > 0);
        sayStopped(grid, s);
        var hand = Object.keys(job.results || {}).filter(function (a) { return job.results[a].code === 'open_by_hand'; });
        if (hand.length) {
          var reopen = document.createElement('button');
          reopen.type = 'button'; reopen.textContent = 'Reopen for automation';
          reopen.addEventListener('click', function () {
            window.aclConfirm('Close and reopen these profiles for automation: ' + hand.join(', '),
              {title: 'Reopen for automation', okLabel: 'Reopen'})
              .then(function (yes) { if (yes) runOpen(grid, hand, true); });
          });
          barOf(grid).querySelector('.warm-msg').appendChild(reopen);
        }
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

  /* ACL-2557: «Open profile» у каждого аккаунта на Home (строка Expanded и поповер плитки Compact).
     Тот же путь, что в Manage: POST /warm/open -> задание -> статус-чтение из ACL-2419. Кнопку рисует
     шаблон только аккаунтам с профилем; здесь: доступность, одно открытие за раз, причина отказа и
     перерисовка строки после открытия. Ничего не открывается само и таймеров на живые аккаунты нет:
     пока идёт задание, опрашивается только локальное состояние (задание и очередь чтения). */
  var homeAvail = null;

  function homeButtons() { return Array.prototype.slice.call(document.querySelectorAll('[data-aov-open]')); }

  function homePaint() {
    var on = !!(homeAvail && homeAvail.available);
    var acts = !!(homeAvail && homeAvail.actions);
    homeButtons().forEach(function (b) {
      b.hidden = !on;
      if (b.getAttribute('data-aov-busy') === '1') return;
      b.disabled = busy || !acts;
      b.title = !acts ? ((homeAvail && homeAvail.reason) || '')
        : 'Open this account’s Multilogin profile with automation and check its status';
    });
  }

  function homeRefresh() {
    var seen = {}, all = [];
    homeButtons().forEach(function (b) {
      var a = b.getAttribute('data-aov-open');
      if (a && !seen[a]) { seen[a] = true; all.push(a); }
    });
    if (!all.length) return Promise.resolve();
    var q = all.map(function (a) { return 'account=' + encodeURIComponent(a); }).join('&');
    return fetch('/warm/state?' + q, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) { homeAvail = data || null; homePaint(); })
      .catch(function () { /* подсказка не должна ломать страницу */ });
  }

  function homeMsg(btn) {
    var wrap = btn.parentNode, m = wrap && wrap.querySelector('.aov-open-msg');
    if (!m && wrap) {
      m = document.createElement('small'); m.className = 'aov-open-msg'; m.setAttribute('role', 'status');
      wrap.appendChild(m);
    }
    return m;
  }

  function homeSay(btn, text, bad) {
    var m = homeMsg(btn);
    if (!m) return null;
    m.textContent = text || '';
    m.classList.toggle('warm-bad', !!bad);
    var old = m.parentNode.querySelector('.aov-open-reopen');
    if (old) old.remove();
    return m;
  }

  function homeFinish(btn, text, bad) {
    btn.removeAttribute('data-aov-busy');
    btn.textContent = 'Open profile';
    busy = false;
    setBusy(false);
    return homeSay(btn, text, bad);
  }

  function homeOpen(btn, account, confirmed) {
    if (busy) return;   // второй щелчок, пока идёт открытие, ничего не делает
    busy = true;
    btn.setAttribute('data-aov-busy', '1');
    setBusy(true);
    btn.disabled = true;
    btn.textContent = 'Opening…';
    homeSay(btn, '', false);
    post('/warm/open', { accounts: [account], confirm_reopen: confirmed === true }).then(function (res) {
      if (res.status >= 400 || !res.body.job_id) {
        homeFinish(btn, res.body.error || 'Could not start opening the profile.', true);
        return;
      }
      homePoll(btn, account, res.body.job_id);
    }).catch(function () { homeFinish(btn, 'Could not reach the dashboard.', true); });
  }

  function homePoll(btn, account, jobId) {
    fetch('/warm/job/' + encodeURIComponent(jobId), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (job) {
        if (job.state === 'running') { setTimeout(function () { homePoll(btn, account, jobId); }, 2000); return; }
        var r = (job.results || {})[account] || {};
        if (r.state === 'ok' || r.ok) {
          var queued = r.status_read === 'queued';
          homeFinish(btn, queued ? 'Opened. Checking the account status…'
            : 'Opened. The status was not read now; it updates with the next list read.', false);
          if (queued) homeAwaitStatus(btn, account, 0);
          else homeRepaint(account);
          return;
        }
        var m = homeFinish(btn, r.message || r.code || 'Could not open the profile.', true);
        if (r.code === 'open_by_hand' && m) {
          var again = document.createElement('button');
          again.type = 'button'; again.className = 'aov-btn aov-open-reopen'; again.textContent = 'Reopen for automation';
          again.addEventListener('click', function () {
            window.aclConfirm('Close and reopen this profile for automation: ' + account,
              { title: 'Reopen for automation', okLabel: 'Reopen' })
              .then(function (yes) { if (yes) homeOpen(btn, account, true); });
          });
          m.parentNode.appendChild(again);
        }
      })
      .catch(function () { homeFinish(btn, 'Lost the opening job; check the account state.', true); });
  }

  // Исход статус-чтения ждём только по локальной очереди (GET своего сервера), не по Meta.
  function homeAwaitStatus(btn, account, tries) {
    fetch('/account-refresh/state', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (v) {
        var waiting = (v.queued || []).some(function (q) { return q.account === account; });
        if ((waiting || v.running) && tries < 45) {
          setTimeout(function () { homeAwaitStatus(btn, account, tries + 1); }, 2000);
          return;
        }
        homeRepaint(account).then(function (ok) {
          homeSay(btn, ok ? 'Status updated.' : 'Opened. Reload the page to see the new status.', !ok);
        });
      })
      .catch(function () { homeSay(btn, 'Opened. Reload the page to see the new status.', true); });
  }

  // Перерисовать состояние аккаунта на Home из свежего фрагмента /account-map: ячейку состояния
  // строки, плитку и счётчики. Остальные ячейки, раскрытые блоки и фильтры остаются как были.
  function homeFresh() {
    var per = typeof window.amapPeriod === 'function' ? window.amapPeriod() : {};
    var url = '/account-map?date_from=' + encodeURIComponent(per.from || '') + '&date_to=' + encodeURIComponent(per.to || '')
      + '&span=' + encodeURIComponent(per.span || '');
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('refresh'); return r.text(); })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, 'text/html');
        if (!fresh.querySelector('#aov')) throw new Error('refresh');
        return fresh;
      });
  }

  function homeRepaint(account) {
    return homeFresh()
      .then(function (fresh) {
        function rowOf(root) {
          var out = null;
          root.querySelectorAll('.aov-row').forEach(function (tr) {
            var c = tr.querySelector('td.amap-list[data-account]');
            if (c && c.getAttribute('data-account') === account) out = tr;
          });
          return out;
        }
        function tileOf(root) {
          var out = null;
          root.querySelectorAll('.aov-tile').forEach(function (t) { if (t.getAttribute('data-name') === account) out = t; });
          return out;
        }
        var oldRow = rowOf(document), newRow = rowOf(fresh);
        if (oldRow && newRow) {
          oldRow.setAttribute('data-state', newRow.getAttribute('data-state'));
          oldRow.className = newRow.className;
          var cell = oldRow.firstElementChild, nc = newRow.firstElementChild;
          if (cell && nc) cell.replaceWith(nc.cloneNode(true));
        }
        var oldTile = tileOf(document), newTile = tileOf(fresh);
        if (oldTile && newTile) oldTile.replaceWith(newTile.cloneNode(true));
        var bar = document.querySelector('#aov .aov-fbar'), nbar = fresh.querySelector('#aov .aov-fbar');
        if (bar && nbar) bar.innerHTML = nbar.innerHTML;
        fresh.querySelectorAll('#aov .aov-block').forEach(function (blk) {
          var key = blk.getAttribute('data-aov-country');
          document.querySelectorAll('#aov .aov-block').forEach(function (cur) {
            if (cur.getAttribute('data-aov-country') !== key) return;
            var a = cur.querySelectorAll('.aov-bh .aov-cnt'), b = blk.querySelectorAll('.aov-bh .aov-cnt');
            if (a.length === b.length) a.forEach(function (n, i) { n.replaceWith(b[i].cloneNode(true)); });
          });
        });
        if (typeof window.aovApply === 'function') window.aovApply();
        homePaint();
        return true;
      })
      .catch(function () { return false; });
  }

  // ACL-2560: после «Take out of <country>» страна на Home перерисовывается из свежего /account-map:
  // блок страны (таблица), её строка плиток, строка фильтров и итог. Раскрытие других стран и фильтр остаются.
  function homeAfterTakeOut(ckey) {
    return homeFresh().then(function (fresh) {
      ['#aov .aov-block', '#aov .aov-crow'].forEach(function (sel) {
        var cur = null, nw = null;
        document.querySelectorAll(sel).forEach(function (n) { if (n.getAttribute('data-aov-country') === ckey) cur = n; });
        fresh.querySelectorAll(sel).forEach(function (n) { if (n.getAttribute('data-aov-country') === ckey) nw = n; });
        if (cur && nw) cur.replaceWith(nw.cloneNode(true));
      });
      ['#aov .aov-fbar', '#amap-totals'].forEach(function (sel) {
        var a = document.querySelector(sel), b = fresh.querySelector(sel);
        if (a && b) a.innerHTML = b.innerHTML;
      });
      var pop = document.getElementById('aov-pop');
      if (pop) pop.hidden = true;
      if (typeof window.amapCampaignMountVisible === 'function' && window.AMAP_CAMPAIGN_TABLE) {
        try { window.amapCampaignMountVisible(); } catch (e) { /* дерево подтянется при следующем открытии */ }
      }
      if (typeof window.aovApply === 'function') window.aovApply();
      homePaint();
    }).catch(function () { window.location.reload(); });
  }

  document.addEventListener('click', function (e) {
    var tk = e.target && e.target.closest ? e.target.closest('[data-aov-takeout]') : null;
    if (tk && !tk.disabled && typeof window.amapUnlink === 'function') {
      e.preventDefault(); e.stopPropagation();
      var ck = tk.getAttribute('data-aov-ckey');
      window.amapUnlink(tk.getAttribute('data-aov-takeout'), 'country', ck, tk, function () { return homeAfterTakeOut(ck); });
      return;
    }
    var t = e.target && e.target.closest ? e.target.closest('[data-aov-open]') : null;
    if (!t || t.disabled) return;
    homeOpen(t, t.getAttribute('data-aov-open'), false);
  });

  window.aclWarmHome = { paint: homePaint, refresh: homeRefresh, repaint: homeRepaint, afterTakeOut: homeAfterTakeOut };

  addStyle();
  document.addEventListener('DOMContentLoaded', function () { refresh(); homeRefresh(); });
  if (document.readyState !== 'loading') { refresh(); homeRefresh(); }
  var settleTimer = null;
  document.addEventListener('htmx:afterSettle', function () {
    // Одно обновление на пачку подмен: каждое — обход процессов на машине.
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(function () { settleTimer = null; refresh(); homeRefresh(); }, 800);
  });
  window.aclWarmRefresh = refresh;
})();
