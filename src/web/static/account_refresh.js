/* ACL-2063: one page coordinator; polling only local queue state, never Meta. */
(function () {
  'use strict';
  if (window.aclAccountRefresh) return;
  var listeners = [], last = null, timer = null, loading = null, inFlight = {}, failures = 0, admissionError = '';
  function json(url, body) {
    return fetch(url, {method: body ? 'POST' : 'GET', body: body, credentials: 'same-origin'}).then(function (r) {
      return r.json().then(function (j) { if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status)); return j; });
    });
  }
  function statusBox() {
    var box = document.getElementById('account-refresh-status');
    if (!box) {
      var anchor = document.querySelector('.amap') || document.querySelector('.amap-campaigns');
      if (!anchor) return null;
      box = document.createElement('div'); box.id = 'account-refresh-status';
      box.className = 'amap-refresh-status'; box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
      anchor.parentNode.insertBefore(box, anchor);
    }
    return box;
  }
  function announce(text, bad, retryAccount) {
    var box = statusBox(); if (!box) return;
    box.hidden = !text; box.textContent = text || '';
    box.setAttribute('role', bad ? 'alert' : 'status');
    // ACL-2652: a checkpoint line says "press Retry", so the button is right here (one queued read, no timer).
    if (text && retryAccount) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'amap-btn amap-banner-retry'; b.textContent = 'Retry';
      b.title = 'Read the status of ' + retryAccount + ' once';
      b.addEventListener('click', function () { b.disabled = true; request(retryAccount, 'retry'); });
      box.appendChild(document.createTextNode(' ')); box.appendChild(b);
    }
  }
  // ACL-2653: a Facebook lock holds only its profile. Its own line names the account and has its own Retry.
  function lockBanner(locks) {
    var anchor = statusBox(); if (!anchor) return;
    var box = document.getElementById('account-lock-status');
    var names = Object.keys(locks);
    if (!names.length) { if (box) box.hidden = true; return; }
    if (!box) {
      box = document.createElement('div'); box.id = 'account-lock-status';
      box.className = 'amap-refresh-status'; box.setAttribute('role', 'alert');
      anchor.parentNode.insertBefore(box, anchor);
    }
    var account = names[0];
    box.hidden = false; box.textContent = account + ': ' + locks[account].reason + ' ' + locks[account].next
      + (names.length > 1 ? ' (+' + (names.length - 1) + ' more)' : '') + ' ';
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'amap-btn amap-banner-retry'; b.textContent = 'Retry';
    b.title = 'Read the status of ' + account + ' once';
    b.addEventListener('click', function () { b.disabled = true; request(account, 'retry'); });
    box.appendChild(b);
  }
  function accept(view) {
    last = view; failures = 0;
    var sweep = view.sweep, halt = view.halt;
    var holds = view.holds || {}, retryFor = halt && halt.kind === 'checkpoint' && halt.account && holds[halt.account] ? halt.account : '';
    if (!retryFor && halt && halt.kind === 'checkpoint') retryFor = halt.account || Object.keys(holds)[0] || '';
    announce(halt ? halt.reason : view.needs_api ? 'Account reads are off: enable the API backend to refresh. Saved lists remain available.' : admissionError || (sweep && sweep.done < sweep.total
      ? 'Refreshing accounts ' + sweep.done + '/' + sweep.total : ''), !!halt || !!admissionError, halt ? retryFor : '');
    lockBanner(view.locks || {});
    listeners.slice().forEach(function (fn) { fn(view); });
    if (view.running || (view.queued || []).length && (!halt || halt.until != null)) later();
  }
  function later() {
    if (timer) return;
    timer = setTimeout(function () { timer = null; load(); }, 2000);
  }
  function load() {
    if (loading) return loading;
    loading = json('/account-refresh/state').then(accept, function () {
      announce('Could not read the account list status. Expand an account to try again.', true);
      if (++failures < 3) later();
    }).then(function () { loading = null; });
    return loading;
  }
  function request(account, trigger) {
    if (inFlight[account]) return inFlight[account];
    var body = new FormData(); body.append('account', account); body.append('trigger', trigger);
    inFlight[account] = json('/account-refresh/request', body).then(function () { admissionError = ''; return load(); }, function (e) {
      admissionError = String(e && e.message || 'Account refresh could not start.') + ' Reload or expand an account after fixing it.';
      announce(admissionError, true);
    }).then(function () { delete inFlight[account]; });
    return inFlight[account];
  }
  window.aclAccountRefresh = {
    request: request, load: load,
    subscribe: function (fn) { listeners.push(fn); if (last) fn(last); },
    // Only a workbook date transition consumes this marker. Removing it means
    // a later ordinary reload is again a real page opening in the current window.
    start: function () {
      var url = new URL(window.location.href), viewOnly = url.searchParams.get('view_only') === '1';
      if (viewOnly) {
        url.searchParams.delete('view_only'); history.replaceState(history.state, '', url.pathname + url.search + url.hash);
        return load();
      }
      return json('/account-refresh/page-open', new FormData()).then(function () { admissionError = ''; }).catch(function (e) {
        // Локальный GET состояния не доказывает, что общий отказ уже устранён.
        admissionError = String(e && e.message || 'Scheduled account refresh could not start.') + ' Reload or expand an account after fixing it.';
        announce(admissionError, true);
      }).then(load);
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', window.aclAccountRefresh.start, {once: true});
  else window.aclAccountRefresh.start();
})();
