/* Пиксель кампании выбирается в её карточке, как Страница (account_pages.js):
   список пикселей кабинета читается в профиле по нажатию, выбор уходит в черновик. */
(function () {
  'use strict';
  var states = Object.create(null);
  function account(slot) {
    var form = slot && slot.closest('form');
    var field = form && form.querySelector('[name="account_label"]');
    return field ? String(field.value || '').trim() : '';
  }
  // ACL-1775: Instant Form ведёт на форму Страницы — пиксель не нужен.
  function instantOnly(slot) {
    var dests = slot.querySelectorAll('.cb-destination');
    if (!dests.length) return false;
    return Array.prototype.every.call(dests, function (d) { return d.value === 'instant_form'; });
  }
  function label(p) { return p.name && p.name !== p.id ? p.name + ' (' + p.id + ')' : p.id; }
  function paint(slot) {
    var key = account(slot), state = states[key];
    var hidden = slot.querySelector('input[name$="_pixel_id"]');
    var select = slot.querySelector('.cb-pixel-select');
    var note = slot.querySelector('[data-pixel-note]');
    var title = slot.querySelector('[data-pixel-label]');
    var box = select && select.closest('.cb-pixel-choice');
    if (!hidden || !select || !note) return;
    var off = instantOnly(slot);
    if (box && box.hidden !== off) box.hidden = off;
    if (title && title.hidden !== off) title.hidden = off;
    // Карточка, перенесённая на другой аккаунт, не несёт пиксель прежнего кабинета.
    if (slot._pixelAccount && slot._pixelAccount !== key) hidden.value = '';
    slot._pixelAccount = key;
    var pixels = state && state.pixels || [];
    var signature = JSON.stringify([key, hidden.value, state]);
    if (select._pixelSignature === signature) return;
    select._pixelSignature = signature;
    var only = pixels.length === 1 ? pixels[0] : null;
    // При нескольких пикселях «Automatic» не сработает: первая строка просит выбрать.
    select.replaceChildren(new Option(only ? 'Automatic: ' + label(only)
      : pixels.length > 1 ? 'Choose a pixel (' + pixels.length + ' on this account)'
      : state && state.state === 'empty' ? 'No pixel found on this account'
      : 'Automatic', ''));
    pixels.forEach(function (p) { select.add(new Option(label(p), p.id)); });
    var chosen = pixels.find(function (p) { return p.id === hidden.value; });
    if (hidden.value && !chosen) select.add(new Option('Unavailable pixel (' + hidden.value + ')', hidden.value));
    select.value = hidden.value;
    select.disabled = !!state && state.state === 'loading';
    if (!key) note.textContent = 'Connect an account to load its pixels.';
    else if (!state) note.textContent = '';
    else if (state.state === 'loading') note.textContent = state.progress || 'Loading pixels…';
    else if (state.state !== 'ready') note.textContent = state.message || 'No pixels are available.';
    else if (hidden.value && !chosen) note.textContent = 'This pixel is not on this ad account. Choose another pixel.';
    else if (chosen) note.textContent = 'Selected: ' + label(chosen);
    else if (only) note.textContent = 'Automatic: ' + label(only);
    else note.textContent = 'This ad account has ' + pixels.length + ' pixels. Choose the one for this campaign.';
  }
  function paintAccount(key) {
    document.querySelectorAll('.cb-slot').forEach(function (s) { if (account(s) === key) paint(s); });
  }
  // cachedOnly: отрисовка спрашивает только кэш сервера (профиль не открывается).
  window.cbRefreshPixels = function (slot, force, cachedOnly) {
    var key = account(slot);
    var known = states[key];
    if (!key || (known && (known.state === 'loading' || (!force && !(known.state === 'not_loaded' && !cachedOnly))))) return;
    var request = {state: 'loading', pixels: []};
    states[key] = request;
    paintAccount(key);
    var body = new FormData();
    body.append('account_label', key);
    if (cachedOnly) body.append('cached', '1');
    if (force) body.append('force', '1');
    // ACL-2850: чтение с профилем идёт потоком строк JSON: {progress} по ходу ожидания, затем {result}.
    // Строка ожидания та же, что у Pages и Read spend; опроса и таймеров нет.
    if (!cachedOnly) body.append('stream', '1');
    var fail = 'Could not load pixels. Refresh pixels to retry.';
    function progress(text) {
      if (states[key] !== request || !text) return;
      request = {state: 'loading', pixels: [], progress: text};
      states[key] = request;
      paintAccount(key);
    }
    function finish(data) {
      if (states[key] !== request) return;
      states[key] = data;
      paintAccount(key);
    }
    fetch('/api/account-pixels', {method: 'POST', body: body})
      .then(function (r) {
        var type = r.headers.get('content-type') || '';
        if (type.indexOf('ndjson') < 0 || !r.body || !r.body.getReader) {
          return r.json().then(function (data) {
            if (!r.ok || !data.ok) throw new Error(data.message || fail);
            return data;
          });
        }
        var reader = r.body.getReader(), decoder = new TextDecoder(), buffer = '', result = null;
        function take(line) {
          if (!line.trim()) return;
          var item = JSON.parse(line);
          if (item.progress) progress(item.progress);
          if (item.result) result = item.result;
        }
        return reader.read().then(function pump(chunk) {
          buffer += decoder.decode(chunk.value || new Uint8Array(), {stream: !chunk.done});
          var lines = buffer.split('\n');
          buffer = lines.pop();
          lines.forEach(take);
          if (!chunk.done) return reader.read().then(pump);
          take(buffer);
          if (!result) throw new Error(fail);
          if (!result.ok) throw new Error(result.message || fail);
          return result;
        });
      })
      .catch(function (err) { return {state: 'failed', pixels: [], message: err.message || fail}; })
      .then(finish);
  };
  window.cbSyncPixels = function (slot) {
    if (!slot) return;
    paint(slot);
    if (slot.getClientRects().length && !instantOnly(slot)) window.cbRefreshPixels(slot, false, true);
  };
  window.cbLoadPixels = function (slot) { window.cbRefreshPixels(slot, false, false); };
  window.cbPixelChanged = function (select) {
    var slot = select.closest('.cb-slot');
    var hidden = slot.querySelector('input[name$="_pixel_id"]');
    hidden.value = select.value;
    hidden.dispatchEvent(new Event('input', {bubbles: true}));
    hidden.dispatchEvent(new Event('change', {bubbles: true}));
    paint(slot);
  };
  document.querySelectorAll('.cb-slot').forEach(window.cbSyncPixels);
}());
