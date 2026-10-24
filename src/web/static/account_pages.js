/* ACL-1880: Page choice belongs to a campaign, never to the account map. */
(function () {
  'use strict';
  var states = Object.create(null);
  function account(slot) {
    var form = slot && slot.closest('form');
    var field = form && form.querySelector('[name="account_label"]');
    return field ? String(field.value || '').trim() : '';
  }
  function paint(slot) {
    var key = account(slot), state = states[key];
    var hidden = slot.querySelector('input[name$="_page_id"]');
    var select = slot.querySelector('.cb-page-select');
    var note = slot.querySelector('[data-page-note]');
    if (!hidden || !select || !note) return;
    // A slot moved to another account must not carry the source identity.
    if (slot._pageAccount && slot._pageAccount !== key) hidden.value = '';
    slot._pageAccount = key;
    var pages = state && state.pages || [];
    var signature = JSON.stringify([key, hidden.value, state]);
    if (select._pageSignature === signature) return;
    select._pageSignature = signature;
    var first = pages[0];
    select.replaceChildren(new Option(first ? 'Automatic: ' + first.name + ' (' + first.id + ')'
      : 'Automatic — first available Page of this account', ''));
    pages.forEach(function (p) { select.add(new Option(p.name + ' (' + p.id + ')' + (p.source === 'profile' ? ' — profile Page' : ''), p.id)); });
    var chosen = pages.find(function (p) { return p.id === hidden.value; });
    if (hidden.value && !chosen) select.add(new Option('Unavailable Page (' + hidden.value + ')', hidden.value));
    select.value = hidden.value;
    select.disabled = !state || state.state === 'loading' || state.state === 'failed';
    if (!key) note.textContent = 'Connect an account to load its Facebook Pages.';
    else if (!state || state.state === 'loading') note.textContent = 'Loading Facebook Pages…';
    else if (state.state !== 'ready') note.textContent = state.message || 'No Facebook Pages are available.';
    else if (hidden.value && !chosen) note.textContent = 'This Page is unavailable. Choose another Page or Automatic.';
    else {
      var resolved = chosen || pages[0];
      note.textContent = (chosen ? 'Selected: ' : 'Automatic: ') + resolved.name + ' (' + resolved.id + ')';
    }
  }
  function paintAccount(key) {
    document.querySelectorAll('.cb-slot').forEach(function (s) {
      if (account(s) !== key) return;
      paint(s);
      if (window.cbFillFormSelects) window.cbFillFormSelects(s);
    });
  }
  window.cbResolvedPage = function (slot) {
    var state = states[account(slot)];
    if (!state || state.state !== 'ready') return '';
    var input = slot.querySelector('input[name$="_page_id"]');
    var selected = input && input.value;
    var found = selected ? state.pages.find(function (p) { return p.id === selected; }) : state.pages[0];
    return found ? found.id : '';
  };
  // cachedOnly: render-time sync asks the server for its cached list only (no profile opened).
  window.cbRefreshPages = function (slot, force, cachedOnly) {
    var key = account(slot);
    var known = states[key];
    if (!key || (known && (known.state === 'loading' || (!force && !(known.state === 'not_loaded' && !cachedOnly))))) return;
    var request = {state: 'loading', pages: []};
    states[key] = request;
    paintAccount(key);
    var body = new FormData();
    body.append('account_label', key);
    if (cachedOnly) body.append('cached', '1');
    if (force) body.append('force', '1');   // «Refresh pages» — мимо кэша сервера
    fetch('/api/account-pages', {method: 'POST', body: body})
      .then(function (r) { return r.json().then(function (data) {
        if (!r.ok || !data.ok) throw new Error(data.message || 'Could not load Facebook Pages. Refresh pages to retry.');
        return data;
      }); })
      .catch(function (err) { return {state: 'failed', pages: [], message: err.message || 'Could not load Facebook Pages. Refresh pages to retry.'}; })
      .then(function (data) {
        if (states[key] !== request) return;
        states[key] = data;
        paintAccount(key);
      });
  };
  window.cbSyncPages = function (slot) {
    if (!slot) return;
    paint(slot);
    if (slot.getClientRects().length) window.cbRefreshPages(slot, false, true);
  };
  window.cbLoadPages = function (slot) { window.cbRefreshPages(slot, false, false); };
  window.cbPageChanged = function (select) {
    var slot = select.closest('.cb-slot');
    var hidden = slot.querySelector('input[name$="_page_id"]');
    hidden.value = select.value;
    hidden.dispatchEvent(new Event('input', {bubbles: true}));
    hidden.dispatchEvent(new Event('change', {bubbles: true}));
    paint(slot);
    if (window.cbFillFormSelects) window.cbFillFormSelects(slot);
  };
  document.querySelectorAll('.cb-slot').forEach(window.cbSyncPages);
}());
