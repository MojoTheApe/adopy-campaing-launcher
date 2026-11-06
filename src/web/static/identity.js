/* ACL-1242: вход рабочим аккаунтом Google — экран входа, подтверждение имени, плашка в шапке.
   Грузится из /static/identity.js, поэтому это код ТЕКУЩЕЙ сборки (и на сохранённой странице).
   Идентичность — метка, а не замок (D1): оверлей закрывает только экран, ни один маршрут и
   ни одна задача от него не зависят. «Not now» появляется только после неудачи или когда вход
   невозможен. Серверный текст попадает в DOM только через textContent; почта нигде не логируется. */
(function () {
  'use strict';

  var TEXT = {
    title: 'Sign in to Adopy Campaign Manager',
    body: 'Use your Adopy Google account. ACL reads only your name and email, so your reports and library edits show who made them. It adds no access to your Google files.',
    go: 'Sign in with Google',
    waiting: 'Waiting for the Google sign-in in your browser...',
    another: 'Try another account',
    notNow: 'Not now',
    again: 'Try again',
    offline: 'Could not reach Google. Check the internet connection and try again. You can keep working and sign in later.',
    unavailable: 'Google sign-in is not set up in this version. Update ACL, or ask your team lead. You can keep working.',
    confirmName: 'Confirm your name as it appears in the CM column of the Stats workbook (or as BI knows you).',
    expired: 'Your Google sign-in has expired. Sign in again so your reports show your name.',
    signOutConfirm: 'Sign out of ACL on this computer? Your settings and accounts stay.'
  };
  var CARD_CSS = 'background:var(--bg-elev,#fff);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:1.2rem 1.3rem;width:min(30rem,92vw)';
  var BOX_CSS = 'position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:10000';

  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, message: 'Unexpected answer.' }; }); });
  }
  function getIdentity() {
    return fetch('/api/identity', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) { return j && j.ok ? j : null; })
      .catch(function () { return null; });
  }

  // ── чистые функции (без DOM): их проверяет Node-харнесс ───────────────────
  // Показывать ли экран входа при загрузке (D1: none / expired; unavailable — с причиной).
  function overlayMode(identity, shownThisSession) {
    var s = identity && identity.state;
    if (s === 'none') return 'sign-in';
    if (s === 'unavailable') return 'unavailable';
    if (s === 'expired') return 'expired';
    if (s === 'declined') return shownThisSession ? '' : 'sign-in';
    return '';
  }
  function chipText(identity) {
    var who = identity && (identity.cm_name || identity.name);
    return who ? 'Signed in as ' + who : '';
  }
  function staleText(identity) {
    var n = identity && identity.stale_days;
    return identity && identity.state === 'stale' && n ? 'Google sign-in not checked for ' + n + ' days' : '';
  }
  function whoText(previous, email) {
    return 'This computer was set up for ' + previous + '. You are signed in as ' + email + '. Who works here?';
  }
  function notListedText(name, list) {
    return '"' + name + '" is not in the campaign manager list.' + (list && list.length ? ' Did you mean: ' + list.join(', ') + '?' : '');
  }
  function isSignedIn(identity) {
    var s = identity && identity.state;
    return s === 'verified' || s === 'stale' || s === 'expired';
  }

  // ── шапка ─────────────────────────────────────────────────────────────────
  var current = null;

  function paintBanner(identity) {
    var old = document.getElementById('identity-banner'); if (old) old.remove();
    if (!identity || identity.state !== 'expired') return;
    var bar = document.createElement('div');
    bar.id = 'identity-banner'; bar.setAttribute('role', 'status');
    bar.style.cssText = 'padding:.4rem .9rem;background:var(--warn-soft,#fff3cd);color:var(--text);border-bottom:1px solid var(--border);font-size:var(--adopy-text-sm);display:flex;gap:.8rem;align-items:center';
    var span = document.createElement('span'); span.textContent = TEXT.expired; bar.appendChild(span);
    bar.appendChild(button('Sign in', false, function () { showOverlay('expired'); }));
    if (document.body.insertBefore && document.body.firstChild) document.body.insertBefore(bar, document.body.firstChild);
    else document.body.appendChild(bar);
  }

  function paintHeader(identity) {
    current = identity;
    var chip = document.getElementById('identity-chip');
    if (chip) {
      var text = chipText(identity), stale = staleText(identity);
      if (isSignedIn(identity) && text) {
        chip.hidden = false;
        chip.textContent = text + (stale ? ' · ' + stale : '') + (identity.state === 'expired' ? ' · sign-in expired' : '');
        chip.title = (identity.email || '') + (stale ? ' — ' + stale : '');
        chip.onclick = null; chip.removeAttribute('role'); chip.removeAttribute('tabindex');
      } else if (identity && identity.state === 'declined') {
        chip.hidden = false; chip.textContent = 'Sign in'; chip.title = TEXT.title;
        chip.setAttribute('role', 'button'); chip.tabIndex = 0;
        chip.onclick = function () { showOverlay('sign-in'); };
      } else {
        chip.hidden = true; chip.textContent = ''; chip.title = '';
      }
    }
    paintBanner(identity);
  }

  // ── оверлей ───────────────────────────────────────────────────────────────
  function removeOverlay() { var o = document.getElementById('identity-box'); if (o) o.remove(); }

  function button(label, primary, onclick) {
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'amap-btn' + (primary ? ' primary' : ''); b.textContent = label; b.onclick = onclick;
    return b;
  }
  function paragraph(text, muted) {
    var p = document.createElement('p');
    p.style.cssText = 'margin:0 0 .7rem' + (muted ? ';color:var(--muted);font-size:var(--adopy-text-sm)' : '');
    p.textContent = text; return p;
  }
  function heading(text) {
    var h = document.createElement('h3'); h.style.margin = '0 0 .5rem'; h.textContent = text; return h;
  }
  function rowOf() {
    var r = document.createElement('div'); r.style.cssText = 'display:flex;gap:.5rem;justify-content:flex-end;flex-wrap:wrap'; return r;
  }

  // Экран имени: после входа (confirm / who) и из Connections («Change name»).
  // data: { title, text, value, picks[], onDone }
  function renderName(card, data) {
    card.innerHTML = '';
    card.appendChild(heading(data.title));
    card.appendChild(paragraph(data.text, false));
    var input = document.createElement('input');
    input.type = 'text'; input.id = 'identity-name'; input.maxLength = 100; input.value = data.value || '';
    input.style.cssText = 'width:100%;box-sizing:border-box;margin:0 0 .5rem';
    card.appendChild(input);
    var msg = paragraph('', true); msg.id = 'identity-msg'; msg.setAttribute('role', 'status'); msg.style.minHeight = '1.4em';
    card.appendChild(msg);
    var picks = document.createElement('div'); picks.style.cssText = 'display:flex;gap:.4rem;flex-wrap:wrap;margin:0 0 .6rem';
    card.appendChild(picks);
    var unlisted = false;
    function offer(list) {
      picks.innerHTML = '';
      (list || []).forEach(function (n) {
        picks.appendChild(button(n, false, function () { input.value = n; unlisted = false; ok.textContent = 'Continue'; }));
      });
    }
    function bad(text) { msg.style.color = 'var(--danger)'; msg.textContent = text; }
    var row = rowOf();
    var ok = button('Continue', true, function () {
      post('/api/identity/name', { name: input.value, confirm_unlisted: unlisted }).then(function (r) {
        if (r && r.ok) { paintHeader(r.identity); data.onDone(); return; }
        if (r && r.reason === 'not_listed') {
          unlisted = true; bad(notListedText(input.value, r.suggestions)); offer(r.suggestions);
          ok.textContent = 'Use this name anyway'; return;
        }
        bad((r && r.message) || 'Could not save the name. Try again.');
      }).catch(function () { bad('Could not reach the dashboard.'); });
    });
    ok.id = 'identity-save';
    if (data.closable) row.appendChild(button('Close', false, data.onDone));
    row.appendChild(ok); card.appendChild(row);
    input.oninput = function () { unlisted = false; ok.textContent = 'Continue'; };
    offer(data.picks);
  }

  // mode: sign-in | unavailable | expired | name
  function showOverlay(mode, nameData) {
    removeOverlay();
    var box = document.createElement('div');
    box.id = 'identity-box'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', TEXT.title);
    box.style.cssText = BOX_CSS;
    var card = document.createElement('div'); card.style.cssText = CARD_CSS;
    box.appendChild(card);
    document.body.appendChild(box);
    if (mode === 'name') {
      renderName(card, { title: 'Change name', text: TEXT.confirmName, value: nameData.value, picks: [],
        closable: true, onDone: removeOverlay });
      return;
    }

    var failed = false;
    function screenSignIn(message, bad, reason) {
      card.innerHTML = '';
      card.appendChild(heading(TEXT.title));
      card.appendChild(paragraph(mode === 'expired' ? TEXT.expired : TEXT.body, false));
      var msg = paragraph(message || '', true); msg.id = 'identity-msg'; msg.setAttribute('role', 'status');
      msg.style.minHeight = '1.4em'; if (bad) msg.style.color = 'var(--danger)';
      card.appendChild(msg);
      var row = rowOf();
      // «Not now» — только после неудачи или когда вход невозможен (D1).
      var cannot = mode === 'unavailable' || (current && current.client_available === false);
      if (failed || cannot) row.appendChild(button(TEXT.notNow, false, notNow));
      var label = reason === 'wrong_domain' ? TEXT.another : (failed ? TEXT.again : TEXT.go);
      var go = button(label, true, signIn); go.id = 'identity-go';
      if (mode === 'unavailable') go.disabled = true;
      row.appendChild(go); card.appendChild(row);
    }
    function notNow() {
      if (mode === 'expired' || (current && current.state === 'expired')) { removeOverlay(); return; }
      post('/api/identity/decline').then(function (j) {
        if (j && j.identity) paintHeader(j.identity);
        removeOverlay();
      }).catch(removeOverlay);
    }
    function signIn() {
      var go = document.getElementById('identity-go'); if (go) go.disabled = true;
      var msg = document.getElementById('identity-msg');
      if (msg) { msg.style.color = 'var(--muted)'; msg.textContent = TEXT.waiting; }
      post('/api/identity/sign-in').then(function (j) {
        if (j && j.ok) { paintHeader(j.identity); nameStep(j); return; }
        failed = true;
        screenSignIn((j && j.message) || TEXT.offline, true, j && j.reason);
      }).catch(function () { failed = true; screenSignIn(TEXT.offline, true, 'offline'); });
    }
    function nameStep(j) {
      if (j.name_step !== 'confirm' && j.name_step !== 'who') { removeOverlay(); return; }
      var id = j.identity || {}, hints = j.suggestions || [];
      if (j.name_step === 'who') {
        renderName(card, { title: 'Who works here?', text: whoText(hints[0] || '', id.email || ''),
          value: id.name || '', picks: hints.slice(0, 1), onDone: removeOverlay });
      } else {
        renderName(card, { title: 'Your name', text: TEXT.confirmName, value: hints[0] || id.name || '',
          picks: hints, onDone: removeOverlay });
      }
    }
    var cannot0 = mode === 'unavailable';
    screenSignIn(cannot0 ? TEXT.unavailable : '', cannot0, cannot0 ? 'unavailable' : '');
  }

  // ── Connections: действия Sign in / Sign out / Change name ────────────────
  function mountActions(container) {
    if (!container) return Promise.resolve();
    return getIdentity().then(function (j) {
      container.innerHTML = '';
      if (!j) return;
      paintHeader(j);
      var line = document.createElement('p');
      line.style.cssText = 'margin:.2rem 0;color:var(--muted);font-size:var(--adopy-text-sm)';
      line.textContent = chipText(j) || 'Not signed in with Google';
      container.appendChild(line);
      var row = document.createElement('div'); row.style.cssText = 'display:flex;gap:.5rem;flex-wrap:wrap;margin:.3rem 0 .6rem';
      if (!isSignedIn(j) || j.state === 'expired') {
        row.appendChild(button('Sign in', false, function () { showOverlay(j.state === 'expired' ? 'expired' : 'sign-in'); }));
      }
      if (isSignedIn(j)) {
        row.appendChild(button('Change name', false, function () { showOverlay('name', { value: j.cm_name || j.name || '' }); }));
        row.appendChild(button('Sign out', false, function () {
          var yes = typeof window.aclConfirm === 'function'
            ? window.aclConfirm(TEXT.signOutConfirm, { title: 'Sign out', okLabel: 'Sign out' })
            : Promise.resolve(typeof window.confirm === 'function' ? window.confirm(TEXT.signOutConfirm) : false);
          yes.then(function (agreed) {
            if (!agreed) return;
            post('/api/identity/sign-out').then(function (r) {
              if (r && r.identity) paintHeader(r.identity);
              mountActions(container);
            });
          });
        }));
      }
      container.appendChild(row);
    });
  }

  // ── старт ─────────────────────────────────────────────────────────────────
  function init() {
    return getIdentity().then(function (j) {
      if (!j) return;
      paintHeader(j);
      var shown = false;
      try { shown = window.sessionStorage.getItem('acl-identity-shown') === '1'; } catch (e) { /* без хранилища просто покажем */ }
      var mode = overlayMode(j, shown);
      if (!mode) return;
      try { window.sessionStorage.setItem('acl-identity-shown', '1'); } catch (e) { /* не страшно */ }
      showOverlay(mode);
    });
  }

  window.aclIdentity = { init: init, overlayMode: overlayMode, chipText: chipText, staleText: staleText,
    whoText: whoText, notListedText: notListedText, mountActions: mountActions, show: showOverlay,
    paintHeader: paintHeader };
  if (!window.__aclIdentityManual && typeof document !== 'undefined' && document.addEventListener) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }
})();
