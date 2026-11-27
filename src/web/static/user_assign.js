/* ACL-2900 / ACL-2920 / ACL-2924: назначение аккаунтов байеру, выбранному в «Logged as». Браузер говорит
   только с /api/user-assign/*, ключ владельца хаба сюда не попадает. Разметка — _user_assign.html (может
   перерисоваться вместе с картой аккаунтов), поэтому обработчики делегированы на document.
   Смена «Logged as» перезагружает страницу, так что байер читается из #buyer-select в момент действия.
   Почта байера на хабе показана в поле #ua-email и правится на месте. */
(function () {
  'use strict';
  var STATES = {
    found: 'Found', add: 'Will be added', not_found: 'Not found', ambiguous: 'Several match',
    duplicate: 'Duplicate line', clash: 'Ad account clash'
  };
  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function msg(text, bad) {
    var el = $('ua-msg'); if (!el) return;
    el.textContent = text || ''; el.className = 'amap-msg' + (bad ? ' bad' : '');
  }
  function buyer() {
    var sel = $('buyer-select');
    return ((sel && sel.value) || '').trim();
  }
  /* Почта уходит на сервер только если владелец сам её набрал: иначе юзера находит сервер (по имени или по памяти машины). */
  function typedEmail() {
    var el = $('ua-email');
    return el && el.dataset.dirty === '1' ? (el.value || '').trim() : '';
  }
  function setEmail(value) {
    var el = $('ua-email'); if (!el) return;
    el.value = value || ''; el.dataset.dirty = '';
  }
  async function post(url, body) {
    var r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin', body: JSON.stringify(body) });
    var d = null; try { d = await r.json(); } catch (e) {}
    return d || { ok: false, error: 'Unexpected answer (HTTP ' + r.status + ').' };
  }
  function askEmail() { var el = $('ua-email'); if (el) el.focus(); }

  function renderList(accounts, newUser) {
    var box = $('ua-list'); if (!box) return;
    if (!accounts || !accounts.length) {
      box.innerHTML = '<div class="amap-note">No accounts assigned yet.'
        + (newUser ? ' The hub user is created on the first Assign.' : '') + '</div>';
      return;
    }
    box.innerHTML = '<div class="amap-note">Assigned on the hub (' + accounts.filter(function (a) { return !a.removed; }).length
      + ' active)</div>' + accounts.map(function (a) {
        if (a.removed) {
          return '<div class="ua-line" style="opacity:.55"><s>' + esc(a.account_name) + '</s> <span class="amap-note">removed</span></div>';
        }
        return '<div class="ua-line">' + esc(a.account_name) + ' <button type="button" class="amap-btn danger" data-ua-remove="'
          + esc(a.account_name) + '">Remove</button></div>';
      }).join('');
  }

  /* Не трогает строку результата (#ua-msg), кроме случаев, когда загрузить список не вышло: после Assign и Remove
     «Assigned N…» / «Removed …» остаются на экране, пока список перечитывается. */
  async function loadAccounts() {
    var who = buyer(), label = $('ua-who');
    if (label) label.textContent = who || 'the selected buyer';
    if (!who) { setEmail(''); renderList([]); return; }
    try {
      var r = await fetch('/api/user-assign/accounts?buyer=' + encodeURIComponent(who), { credentials: 'same-origin' });
      var d = await r.json();
      if (d.need_email) { setEmail(''); renderList([]); msg(d.error || '', false); askEmail(); return; }
      if (!d.ok) { msg(d.error || 'Could not load the hub accounts.', true); renderList([]); return; }
      setEmail(d.user_email);
      renderList(d.accounts, d.new_user);
    } catch (e) { msg('Could not load the hub accounts: ' + (e && e.message || e), true); }
  }

  /* Новая почта заменяет запомненную для этого байера; список перечитывается уже по ней. */
  async function saveEmail() {
    var el = $('ua-email'), who = buyer(); if (!el || !who) return;
    var value = (el.value || '').trim();
    if (el.dataset.dirty !== '1') return;
    if (!EMAIL_RE.test(value)) { msg('Enter a valid email address.', true); return; }
    var d = await post('/api/user-assign/buyer-email', { buyer: who, user_email: value });
    if (!d.ok) { msg(d.error || 'Could not save the email.', true); return; }
    el.dataset.dirty = '';
    msg('Hub user for ' + who + ': ' + d.user_email + '.');
    loadAccounts();
  }

  function render(results) {
    var box = $('ua-results'); if (!box) return;
    box.innerHTML = (results || []).map(function (r) {
      var extra = '';
      if (r.state === 'found') extra = esc(r.account_name) + (r.sent ? ' · assigned' : '');
      else if (r.state === 'ambiguous') extra = esc((r.candidates || []).join(', '));
      else if (r.state === 'clash') extra = esc(r.error || '');
      else if (r.state === 'add') extra = esc(r.account_name) + (r.sent ? ' · assigned' : r.added ? ' · added to the map, not assigned' : '');
      else if (r.state === 'not_found') extra = esc(r.error || '');
      else if (r.state === 'duplicate') extra = esc(r.account_name) + ' is already in this list';
      var label = (r.state === 'found' && r.match === 'partial') ? 'Matched by part of the name'
        : (r.state === 'add') ? ((r.sent || r.added) ? 'Added to ' : 'Will be added to ') + (r.country || '') : (STATES[r.state] || r.state);
      return '<div class="ua-line ua-' + esc(r.state) + '"><span class="ua-in">' + esc(r.input) + '</span> '
        + '<b>' + esc(label) + '</b> <span class="amap-note">' + extra + '</span></div>';
    }).join('');
  }

  document.addEventListener('toggle', function (e) {
    if (e.target && e.target.id === 'ua-root' && e.target.open) { msg(''); loadAccounts(); }
  }, true);

  document.addEventListener('input', function (e) {
    if (e.target && e.target.id === 'ua-email') e.target.dataset.dirty = '1';
  });
  document.addEventListener('change', function (e) {
    if (e.target && e.target.id === 'ua-email') saveEmail();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target && e.target.id === 'ua-email') { e.preventDefault(); saveEmail(); }
  });

  document.addEventListener('click', async function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var btn = t.closest('#ua-check, #ua-send, [data-ua-remove]');
    if (!btn) return;
    var text = ($('ua-text') || {}).value || '';
    var who = buyer();
    if (btn.id === 'ua-check') {
      msg('Checking…');
      var d = await post('/api/user-assign/preview', { text: text });
      if (!d.ok) { msg(d.error || 'Could not check the lines.', true); return; }
      msg(''); render(d.results);
    } else if (btn.id === 'ua-send') {
      if (!who) { msg('Pick a buyer in "Logged as" first.', true); return; }
      btn.disabled = true; msg('Sending…');
      try {
        var s = await post('/api/user-assign/send', { buyer: who, user_email: typedEmail(), text: text });
        if (s.need_email) { askEmail(); msg(s.error || 'Enter the buyer\'s email.', true); return; }
        if (!s.ok) { if (s.results) render(s.results); msg(s.error || 'Could not assign.', true); return; }
        render(s.results);
        msg('Assigned ' + (s.assigned || []).length + ' to ' + who
          + ((s.added || []).length ? ' (' + s.added.length + ' added to the map).' : '.'));
        loadAccounts();
      } finally { btn.disabled = false; }
    } else {
      var name = btn.getAttribute('data-ua-remove');
      if (!who) { msg('Pick a buyer in "Logged as" first.', true); return; }
      btn.disabled = true;
      var rm = await post('/api/user-assign/remove', { buyer: who, user_email: typedEmail(), account_names: [name] });
      if (rm.need_email) { btn.disabled = false; askEmail(); msg(rm.error || 'Enter the buyer\'s email.', true); return; }
      if (!rm.ok) { btn.disabled = false; msg(rm.error || 'Could not remove.', true); return; }
      msg('Removed ' + name + ' from ' + who + '.');
      loadAccounts();
    }
  });
})();
