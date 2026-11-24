/* ACL-2900: назначение аккаунтов пользователю. Браузер говорит только с /api/user-assign/*,
   ключ владельца хаба сюда не попадает. Разметка — _user_assign.html (может перерисоваться
   вместе с картой аккаунтов), поэтому обработчики делегированы на document. */
(function () {
  'use strict';
  var STATES = {
    found: 'Found', not_found: 'Not found', ambiguous: 'Several match',
    duplicate: 'Duplicate line', clash: 'Ad account clash'
  };
  var assigned = {}; // email -> имена, назначенные из этого окна

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
  function email() {
    var typed = ($('ua-email').value || '').trim();
    return typed || ($('ua-user').value || '').trim();
  }
  async function post(url, body) {
    var r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin', body: JSON.stringify(body) });
    var d = null; try { d = await r.json(); } catch (e) {}
    return d || { ok: false, error: 'Unexpected answer (HTTP ' + r.status + ').' };
  }

  async function loadUsers() {
    var sel = $('ua-user'); if (!sel || sel.dataset.loaded) return;
    sel.dataset.loaded = '1';
    try {
      var r = await fetch('/api/user-assign/users', { credentials: 'same-origin' });
      var d = await r.json();
      if (!d.ok) { msg(d.error || 'Could not load users.', true); sel.dataset.loaded = ''; return; }
      (d.users || []).filter(function (u) { return !u.disabled; }).forEach(function (u) {
        var o = document.createElement('option'); o.value = u.email;
        o.textContent = (u.name ? u.name + ' · ' : '') + u.email + ' (' + (u.active_accounts || 0) + ')';
        sel.appendChild(o);
      });
    } catch (e) { msg('Could not load users: ' + (e && e.message || e), true); sel.dataset.loaded = ''; }
  }

  function render(results, who) {
    var box = $('ua-results'); if (!box) return;
    var html = (results || []).map(function (r) {
      var extra = '';
      if (r.state === 'found') extra = esc(r.account_name) + (r.sent ? ' · assigned' : '');
      else if (r.state === 'ambiguous') extra = esc((r.candidates || []).join(', '));
      else if (r.state === 'clash') extra = esc(r.error || '');
      else if (r.state === 'duplicate') extra = esc(r.account_name) + ' is already in this list';
      var label = (r.state === 'found' && r.match === 'partial') ? 'Matched by part of the name' : (STATES[r.state] || r.state);
      return '<div class="ua-line ua-' + esc(r.state) + '"><span class="ua-in">' + esc(r.input) + '</span> '
        + '<b>' + esc(label) + '</b> <span class="amap-note">' + extra + '</span></div>';
    }).join('');
    var mine = assigned[who] || [];
    if (who && mine.length) {
      html += '<div class="ua-assigned"><div class="amap-note">Assigned in this window to ' + esc(who) + '</div>'
        + mine.map(function (n) {
          return '<div class="ua-line">' + esc(n) + ' <button type="button" class="amap-btn danger" data-ua-remove="'
            + esc(n) + '">Remove</button></div>';
        }).join('') + '</div>';
    }
    box.innerHTML = html;
  }

  document.addEventListener('toggle', function (e) {
    if (e.target && e.target.id === 'ua-root' && e.target.open) loadUsers();
  }, true);

  document.addEventListener('click', async function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var btn = t.closest('#ua-check, #ua-send, [data-ua-remove]');
    if (!btn) return;
    var text = ($('ua-text') || {}).value || '';
    var who = email();
    if (btn.id === 'ua-check') {
      msg('Checking…');
      var d = await post('/api/user-assign/preview', { text: text });
      if (!d.ok) { msg(d.error || 'Could not check the lines.', true); return; }
      msg(''); render(d.results, who);
    } else if (btn.id === 'ua-send') {
      if (!who) { msg('Pick a user or type an email.', true); return; }
      btn.disabled = true; msg('Sending…');
      try {
        var s = await post('/api/user-assign/send', { user_email: who, text: text });
        if (!s.ok) { if (s.results) render(s.results, who); msg(s.error || 'Could not assign.', true); return; }
        var list = assigned[who] = assigned[who] || [];
        (s.assigned || []).forEach(function (n) { if (list.indexOf(n) < 0) list.push(n); });
        render(s.results, who);
        msg('Assigned ' + (s.assigned || []).length + ' to ' + who + '.');
      } finally { btn.disabled = false; }
    } else {
      var name = btn.getAttribute('data-ua-remove');
      if (!who) { msg('Pick a user or type an email.', true); return; }
      btn.disabled = true;
      var rm = await post('/api/user-assign/remove', { user_email: who, account_names: [name] });
      if (!rm.ok) { btn.disabled = false; msg(rm.error || 'Could not remove.', true); return; }
      assigned[who] = (assigned[who] || []).filter(function (n) { return n !== name; });
      msg('Removed ' + name + ' from ' + who + '.');
      var line = btn.closest('.ua-line'); if (line) line.remove();
    }
  });
})();
