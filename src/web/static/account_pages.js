/* ACL-1880: Page choice belongs to a campaign, never to the account map. */
(function () {
  'use strict';
  var states = Object.create(null);
  function account(slot) {
    var form = slot && slot.closest('form');
    var field = form && form.querySelector('[name="account_label"]');
    return field ? field.value : '';
  }
  /* Правило «Automatic» (зеркало page_choice.automatic_page): страница карты, если она среди
     страниц кабинета; иначе единственная; иначе несколько - человек обязан выбрать (ACL-1880).
     Первую из списка не берём: порядок promote_pages у Meta ничего не значит. */
  function automatic(state) {
    var pages = state && state.state === 'ready' ? state.pages || [] : [];
    var mapped = state && state.map_page_id ? pages.find(function (p) { return p.id === state.map_page_id; }) : null;
    if (mapped) return {page: mapped, source: 'account map', several: 0};
    if (pages.length === 1) return {page: pages[0], source: 'only Page of this account', several: 0};
    return {page: null, source: '', several: pages.length > 1 ? pages.length : 0};
  }
  function severalText(n) {
    return 'This ad account has ' + n + ' Facebook Pages. Choose the Page for this campaign in the builder.';
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
    var auto = automatic(state);
    var autoText = auto.page ? 'Automatic: ' + auto.page.name + ' (' + auto.page.id + ')'
      : auto.several ? 'Choose a Page (this account has ' + auto.several + ')' : 'Automatic';
    select.replaceChildren(new Option(autoText, ''));
    pages.forEach(function (p) { select.add(new Option(p.name + ' (' + p.id + ')', p.id)); });
    var chosen = pages.find(function (p) { return p.id === hidden.value; });
    if (hidden.value && !chosen) select.add(new Option('Unavailable Page (' + hidden.value + ')', hidden.value));
    select.value = hidden.value;
    select.disabled = !state || (state.state !== 'ready' && state.state !== 'empty');
    if (!key) note.textContent = 'Connect an account to load its Facebook Pages.';
    else if (!state || state.state === 'loading') note.textContent = 'Loading Facebook Pages…';
    else if (state.state !== 'ready') note.textContent = state.message;
    else if (hidden.value && !chosen) note.textContent = 'This Page is unavailable. Choose another Page or Automatic.';
    else if (chosen) note.textContent = 'Selected: ' + chosen.name + ' (' + chosen.id + ')';
    else if (auto.page) note.textContent = autoText + ' · ' + auto.source;
    else if (auto.several) note.textContent = severalText(auto.several);
    else note.textContent = 'No Page is available. Automatic creates only the paused website campaign and ad sets; Instant Forms need a Page.';
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
    var found = selected ? state.pages.find(function (p) { return p.id === selected; }) : automatic(state).page;
    return found ? found.id : '';
  };
  /* Для pre-flight (`page_choice`) и загрузки форм: что известно о Страницах кабинета. */
  window.cbPageAudit = function (slot) {
    var state = states[account(slot)];
    var input = slot.querySelector('input[name$="_page_id"]');
    var ready = state && state.state === 'ready';
    return {
      page_id: input ? input.value : '',
      page_count: ready ? state.pages.length : null,
      page_map_ok: ready && !!state.map_page_id && state.pages.some(function (p) { return p.id === state.map_page_id; })
    };
  };
  /* Несколько Страниц и выбора нет - текст-просьба выбрать; иначе пусто. */
  window.cbPageNeedsChoice = function (slot) {
    var state = states[account(slot)];
    var input = slot.querySelector('input[name$="_page_id"]');
    if (!state || state.state !== 'ready' || (input && input.value)) return '';
    var auto = automatic(state);
    return auto.several ? severalText(auto.several) : '';
  };
  window.cbRefreshPages = function (slot, force) {
    var key = account(slot);
    if (!key || (states[key] && (!force || states[key].state === 'loading'))) return;
    var request = {state: 'loading', pages: []};
    states[key] = request;
    paintAccount(key);
    var body = new FormData();
    body.append('account_label', key);
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
        document.dispatchEvent(new CustomEvent('cb-pages-changed', {detail: {account: key}}));
      });
  };
  window.cbSyncPages = function (slot) {
    if (!slot) return;
    paint(slot);
    if (slot.getClientRects().length) window.cbRefreshPages(slot, false);
  };
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
