/* ACL-2268: the account profile is brought up when a PERSON starts working with the account in the Builder.

   Triggers (only a trusted event, `isTrusted`): a click on the add-campaign button, a click on the duplicate
   button, an edit inside a campaign card (.cb-slot); the drawer's "+ Campaign" / account "+" call
   window.aclWarmAutoUser(form, 'add', event.isTrusted) themselves.

   Rules:
   - never on page load, never from a restored draft, never before the first campaign card exists;
   - API backend only; one request per account per page session; no timers that poll Meta, MLX or CDP;
   - the server decides (feature setting, 30 minutes per profile, memory, ceiling, Meta pauses); a refusal is
     quiet: the line disappears, the reason sits in the account chip tooltip (warm_sessions.js);
   - this script never closes a profile and has no button of its own. */
(function () {
  'use strict';
  if (window.__aclWarmAuto) return;
  window.__aclWarmAuto = true;

  var POLL_MS = 3000;
  var POLL_MAX = 100;          // about 5 minutes, then the line is cleared
  var asked = {};              // account -> true (already asked in this page session)

  function isApi() { return window.ACL_FB_BACKEND === 'api'; }

  function labelOf(form) {
    var f = form && form.querySelector ? form.querySelector('[name="account_label"]') : null;
    return f ? String(f.value || '').trim() : '';
  }

  function slotCount(form) {
    return form.querySelectorAll ? form.querySelectorAll('.cb-slot').length : 0;
  }

  function lineOf(form) {
    var el = form.querySelector('.warm-auto-line');
    if (el) return el;
    el = document.createElement('div');
    el.className = 'warm-auto-line';
    el.style.cssText = 'font-size:.78rem;margin:.2rem 0 .5rem;color:var(--muted,#888)';
    var anchor = form.querySelector('.cb-empty-hint') || form.querySelector('.cb-slots');
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(el, anchor);
    else form.appendChild(el);
    return el;
  }

  function say(form, text, ok) {
    var el = lineOf(form);
    el.textContent = text || '';
    el.style.display = text ? '' : 'none';
    el.style.color = ok ? 'var(--ok,#2a9d4b)' : 'var(--muted,#888)';
  }

  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); });
  }

  /* Local state only (registry + processes on this computer): no call to Meta, MLX or the browser. */
  function poll(form, label, n) {
    fetch('/warm/state?account=' + encodeURIComponent(label), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var info = data && data.accounts && data.accounts[label];
        if (!info) { say(form, ''); return; }
        if (info.auto === 'queued' || info.auto === 'opening') {
          say(form, 'Opening profile…');
          if (n < POLL_MAX) setTimeout(function () { poll(form, label, n + 1); }, POLL_MS);
          else say(form, '');
          return;
        }
        if (info.auto === 'done' || info.state === 'warm') { say(form, 'Profile ready', true); return; }
        say(form, '');       // any refusal or failure: quiet, the tooltip of the chip has the reason
      })
      .catch(function () { say(form, ''); });
  }

  function ensure(form, trigger) {
    if (!isApi() || !form) return;
    var label = labelOf(form);
    var count = slotCount(form);
    if (!label || asked[label] || count < 1) return;
    asked[label] = true;
    post('/warm/ensure', { accounts: [label], trigger: trigger, campaigns: count }).then(function (res) {
      var st = res.body && res.body.accounts && res.body.accounts[label];
      if (res.status >= 400 || st !== 'queued') return;     // off, recent, refused: nothing to show
      say(form, 'Opening profile…');
      poll(form, label, 0);
    }).catch(function () { /* no connection: nothing to show */ });
  }

  /* The drawer calls this after its own add. `trusted` must be the event's isTrusted. */
  window.aclWarmAutoUser = function (form, trigger, trusted) {
    if (trusted !== true) return;
    ensure(form, trigger);
  };

  function touched(ev) {
    if (!ev || !ev.isTrusted || !isApi() || !ev.target || !ev.target.closest) return;
    var t = ev.target;
    var trigger = '';
    if (ev.type === 'click') {
      if (t.closest('.cb-add-btn')) trigger = 'add';
      else if (t.closest('.cb-dup-slot-btn')) trigger = 'duplicate';
    } else if (t.closest('.cb-slot')) {
      trigger = 'edit';
    }
    if (!trigger) return;
    var form = t.closest('form.open-account-form');
    if (!form) return;
    // A new card appears after the click handler ran: look once the handler is done.
    setTimeout(function () { ensure(form, trigger); }, 0);
  }
  ['click', 'input', 'change'].forEach(function (type) { document.addEventListener(type, touched, true); });
})();
