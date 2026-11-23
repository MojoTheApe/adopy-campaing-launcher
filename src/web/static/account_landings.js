/* Лендинги аккаунта из JCloudFront (ACL-2852): поиск по Variant / пути / URL, выбор до трёх,
   проценты. Поиск и хранение — на сервере (/api/jcloud/landings/search, /api/account-landings);
   токен JCloud в браузер не попадает. Логика процентов тоже на сервере: здесь только отрисовка. */
(function () {
  'use strict';
  var MAX = 3;
  var PREVIEW_DELAY_MS = 400;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function post(url, fields) {
    var body = new FormData();
    Object.keys(fields).forEach(function (k) { if (fields[k] != null) body.append(k, fields[k]); });
    return fetch(url, {method: 'POST', body: body}).then(function (r) { return r.json(); });
  }

  function init(root) {
    var account = root.getAttribute('data-landings-account') || '';
    var list = root.querySelector('[data-landings-list]');
    var openBtn = root.querySelector('[data-landings-open]');
    var statusEl = root.querySelector('[data-landings-status]');
    var search = root.querySelector('[data-landings-search]');
    var query = root.querySelector('[data-landings-query]');
    var results = root.querySelector('[data-landings-results]');
    var preview = root.querySelector('[data-landings-preview]');
    var assetInput = root.querySelector('[data-landings-asset]');
    var assetList = root.querySelector('[data-landings-asset-list]');
    var assetChip = root.querySelector('[data-landings-asset-chip]');
    var assetName = root.querySelector('[data-landings-asset-name]');
    var asset = null;       // выбранный ассет {id, name} или null
    var assetToken = 0;
    var assetTimer = null;
    var chosen = [];
    var token = 0;         // номер поиска: устаревшие ответы и повторы при pending отбрасываются
    var previewTimer = null;

    function say(text) { statusEl.textContent = text || ''; }

    function paintChosen() {
      list.replaceChildren();
      chosen.forEach(function (l) {
        var row = el('div', 'cb-landing-chosen');
        row.appendChild(el('span', 'cb-landing-chosen-name', l.title || ('#' + l.id)));
        row.appendChild(el('span', 'cb-landing-chosen-meta cb-landing-chosen-path', [l.domain, l.path].filter(Boolean).join('')));
        var pct = el('input', 'cb-input cb-landing-pct');
        pct.type = 'number'; pct.min = '0'; pct.max = '100'; pct.value = l.percent == null ? '' : l.percent;
        pct.title = 'Share of traffic, %';
        pct.addEventListener('change', function () { apply({op: 'percent', id: l.id, percent: pct.value}); });
        row.appendChild(pct);
        row.appendChild(el('span', 'cb-landing-chosen-meta', '%'));
        var rm = el('button', 'cb-landing-remove', 'Remove');
        rm.type = 'button';
        rm.addEventListener('click', function () { apply({op: 'remove', id: l.id}); });
        row.appendChild(rm);
        list.appendChild(row);
      });
      var total = chosen.reduce(function (s, l) { return s + (Number(l.percent) || 0); }, 0);
      if (chosen.length > 1 && total !== 100) say('Shares add up to ' + total + '%, not 100%.');
      openBtn.textContent = chosen.length ? '+ Add landing' : 'Pick landing';
      openBtn.hidden = chosen.length >= MAX;
    }

    function apply(fields) {
      fields.account_label = account;
      return post('/api/account-landings', fields).then(function (data) {
        if (data && data.landings) chosen = data.landings;
        say(data && data.ok === false ? (data.message || '') : '');
        paintChosen();
        if (!results.hidden) markChosen();
        return data;
      }).catch(function () { say('Could not save. Try again.'); });
    }

    function isChosen(id) { return chosen.some(function (l) { return String(l.id) === String(id); }); }
    function markChosen() {
      results.querySelectorAll('[data-landing-id]').forEach(function (row) {
        var b = row.querySelector('[data-landing-select]');
        if (!b) return;
        var done = isChosen(row.getAttribute('data-landing-id')) || chosen.length >= MAX;
        b.disabled = done;
        b.textContent = isChosen(row.getAttribute('data-landing-id')) ? 'Added' : 'Select';
      });
    }

    function hidePreview() {
      clearTimeout(previewTimer);
      preview.hidden = true;
      preview.replaceChildren();
    }
    function showPreview(item, anchor) {
      if (!/^https?:\/\//i.test(item.url || '')) return;
      var frame = document.createElement('iframe');
      // Скрипты страницы разрешены (лендинг рендерится на JS), но без доступа к ACL: другой origin.
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.setAttribute('loading', 'lazy');
      frame.src = item.url;
      preview.replaceChildren(frame);
      preview.hidden = false;
      placePreview(anchor);
    }
    // ACL-2876: окно привязано к окну браузера, а не к блоку: справа от списка, если влезает,
    // иначе слева, иначе у правого края; по вертикали всегда внутри окна.
    function placePreview(anchor) {
      var box = preview.getBoundingClientRect();
      var list = (anchor || results).closest('[data-landings-results]') || results;
      var lr = list.getBoundingClientRect();
      var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight, gap = 8;
      var left = lr.right + gap;
      if (left + box.width > vw - gap) left = lr.left - box.width - gap;
      if (left < gap) left = Math.max(gap, vw - box.width - gap);
      var ar = (anchor || list).getBoundingClientRect();
      var top = Math.min(Math.max(gap, ar.top), Math.max(gap, vh - box.height - gap));
      preview.style.left = Math.round(left) + 'px';
      preview.style.top = Math.round(top) + 'px';
    }

    function addRows(items) {
      items.forEach(function (item) {
        var row = el('div', 'cb-landing-row');
        row.setAttribute('data-landing-id', item.id);
        var main = el('div', 'cb-landing-main');
        main.appendChild(el('div', 'cb-landing-title', item.title || ('#' + item.id)));
        main.appendChild(el('div', 'cb-landing-sub',
          [(item.domain || '') + (item.path || ''), item.template, item.language, item.status].filter(Boolean).join(' · ')));
        row.appendChild(main);
        var open = el('a', 'cb-landing-sub', 'Open ↗');
        open.href = /^https?:\/\//i.test(item.url || '') ? item.url : '#'; open.target = '_blank'; open.rel = 'noopener noreferrer';
        row.appendChild(open);
        var pick = el('button', 'cb-field-save-btn', 'Select');
        pick.type = 'button'; pick.setAttribute('data-landing-select', '');
        pick.addEventListener('click', function () {
          apply({op: 'add', landing: JSON.stringify(item)}).then(function (data) {
            if (data && data.ok) closeSearch();
          });
        });
        row.appendChild(pick);
        row.addEventListener('mouseenter', function () {
          clearTimeout(previewTimer);
          previewTimer = setTimeout(function () { showPreview(item, row); }, PREVIEW_DELAY_MS);
        });
        row.addEventListener('mouseleave', hidePreview);
        results.appendChild(row);
      });
      markChosen();
    }

    function note(text) {
      results.hidden = false;
      var n = el('div', 'cb-landing-note', text);
      n.setAttribute('data-landing-note', '');
      results.appendChild(n);
    }
    function clearNotes() {
      results.querySelectorAll('[data-landing-note]').forEach(function (n) { n.remove(); });
    }

    // mode/snapshot держат способ поиска и каталог при листании и ожидании `pending`.
    function run(text, page, mode, snapshot, mine, stock) {
      post('/api/jcloud/landings/search', {query: text, page: page, mode: mode, snapshot_id: snapshot, stock_id: stock})
        .then(function (data) {
          if (mine !== token) return;
          clearNotes();
          if (!data || data.ok === false) { note((data && data.message) || 'Search failed. Try again.'); return; }
          if (data.state === 'pending') {
            note('JCloudFront is preparing the catalog…');
            setTimeout(function () {
              if (mine === token && !search.hidden) { clearNotes(); run(text, page, data.searched_as, snapshot, mine, stock); }
            }, Math.max(1, Number(data.retry_after_seconds) || 3) * 1000);
            return;
          }
          if (page === 1 && !data.items.length) { note('Nothing found.'); return; }
          addRows(data.items);
          if (!data.complete) note(data.unclassified_count + ' landings could not be checked.');
          if (data.has_more) {
            var more = el('button', 'cb-field-save-btn cb-landing-secondary', 'Load more');
            more.type = 'button';
            more.setAttribute('data-landing-note', '');
            more.addEventListener('click', function () {
              more.remove();
              run(text, page + 1, data.searched_as, data.snapshot_id, mine, stock);
            });
            results.appendChild(more);
          }
        })
        .catch(function () { if (mine === token) { clearNotes(); note('Search failed. Try again.'); } });
    }

    function doSearch() {
      var text = (query.value || '').trim();
      token += 1;
      results.replaceChildren();
      hidePreview();
      if (!text && !asset) { results.hidden = true; return; }
      results.hidden = false;
      note('Searching…');
      run(text, 1, null, null, token, asset ? asset.id : null);
    }

    // Фильтр «Asset»: подсказки по началу названия (сток JCloudFront), выбранный ассет — чип.
    function pickAsset(stock) {
      asset = stock;
      assetList.hidden = true; assetList.replaceChildren();
      assetInput.value = '';
      assetInput.hidden = !!stock;
      assetChip.hidden = !stock;
      assetName.textContent = stock ? stock.name : '';
      if (stock) doSearch();
    }
    function suggestAssets() {
      var text = (assetInput.value || '').trim();
      assetToken += 1;
      var mineA = assetToken;
      if (text.length < 2) { assetList.hidden = true; assetList.replaceChildren(); return; }
      post('/api/jcloud/stocks/search', {query: text}).then(function (data) {
        if (mineA !== assetToken) return;
        assetList.replaceChildren();
        assetList.hidden = false;
        var stocks = (data && data.ok !== false && data.stocks) || [];
        if (!stocks.length) {
          assetList.appendChild(el('div', 'cb-landing-note', data && data.ok === false ? data.message : 'No such asset.'));
          return;
        }
        stocks.forEach(function (s) {
          var b = el('button', 'cb-landing-row cb-landing-asset-option', s.name);
          b.type = 'button';
          b.setAttribute('data-asset-id', s.id);
          b.addEventListener('click', function () { pickAsset(s); });
          assetList.appendChild(b);
        });
      }).catch(function () { if (mineA === assetToken) { assetList.hidden = true; } });
    }

    // Выбранные лендинги читаем один раз и только когда блок аккаунта виден: свёрнутые блоки
    // не создают запросов при загрузке страницы.
    var loaded = false;
    function load() {
      if (loaded) return;
      loaded = true;
      post('/api/account-landings', {account_label: account, op: 'get'})
        .then(function (data) { chosen = (data && data.landings) || []; paintChosen(); })
        .catch(function () { loaded = false; paintChosen(); });
    }

    function openSearch(prefill) {
      load();
      search.hidden = false;
      if (prefill != null) query.value = prefill;
      query.focus();
      if (prefill) doSearch();
    }
    function closeSearch() {
      token += 1;
      assetToken += 1;
      assetList.hidden = true;
      search.hidden = true;
      results.hidden = true;
      results.replaceChildren();
      hidePreview();
    }

    openBtn.addEventListener('click', function () { openSearch(); });
    root.querySelector('[data-landings-go]').addEventListener('click', doSearch);
    root.querySelector('[data-landings-close]').addEventListener('click', closeSearch);
    query.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); doSearch(); } });
    assetInput.addEventListener('input', function () {
      clearTimeout(assetTimer);
      assetTimer = setTimeout(suggestAssets, 250);
    });
    root.querySelector('[data-landings-asset-clear]').addEventListener('click', function () {
      pickAsset(null);
      doSearch();
      assetInput.focus();
    });
    root._landingsOpen = openSearch;

    paintChosen();
    if (typeof IntersectionObserver === 'function') {
      var seen = new IntersectionObserver(function (entries) {
        if (entries.some(function (e) { return e.isIntersecting; })) { seen.disconnect(); load(); }
      });
      seen.observe(root);
    } else {
      load();
    }
  }

  // Для кнопки «Use landing» в статистике: открыть выбор у аккаунта и сразу найти Variant.
  window.cbUseLanding = function (account, variant) {
    var roots = document.querySelectorAll('.cb-landings');
    for (var i = 0; i < roots.length; i++) {
      if (roots[i].getAttribute('data-landings-account') === account && roots[i]._landingsOpen) {
        roots[i]._landingsOpen(variant);
        return true;
      }
    }
    return false;
  };

  // «Use landing» в таблице Landings статистики страны: переходим в Manage, раскрываем билдер,
  // аккаунт и блок Account, открываем выбор с этим Variant. Аккаунт — тот, чей блок уже раскрыт,
  // иначе первый в списке страны; на нём же и подпись, чтобы было видно, куда попал выбор.
  window.cbUseLandingFromStats = function (country, variant) {
    var link = document.querySelector('.sidebar-link[data-view="country-' + country + '-manage"]');
    if (link) link.click();
    var builder = document.getElementById('manage-builder-' + country);
    if (!builder) return false;
    builder.open = true;
    var rows = builder.querySelectorAll('.cb-acc-row');
    var row = null;
    for (var i = 0; i < rows.length && !row; i++) {
      if (rows[i].querySelector('.cb-acc-body.cb-acc-open')) row = rows[i];
    }
    row = row || rows[0];
    if (!row) return false;
    var header = row.querySelector('.cb-acc-header');
    var body = row.querySelector('.cb-acc-body');
    if (body && !body.classList.contains('cb-acc-open') && typeof window.cbToggleAcc === 'function' && header) {
      window.cbToggleAcc(header.getAttribute('data-acc-id'));
    }
    var pre = row.querySelector('.cb-preflight');
    if (pre) pre.classList.remove('cb-preflight-collapsed');
    var landings = row.querySelector('.cb-landings');
    if (!landings) return false;
    var opened = window.cbUseLanding(landings.getAttribute('data-landings-account'), variant);
    landings.scrollIntoView({block: 'center'});
    return opened;
  };

  document.querySelectorAll('.cb-landings').forEach(init);
}());
