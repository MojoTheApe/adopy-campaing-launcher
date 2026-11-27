/* Generate в блоке Landings аккаунта: копии лендингов на корневом домене аккаунта (Limited_QA).
   Источники — лендинги, выбранные на аккаунте, плюс свободный текст; страна и аккаунт берутся из билдера,
   домен подставляется из карты/таблицы аккаунтов и правится руками. Токен остаётся на сервере. */
(function () {
  'use strict';
  var TERMINAL = ['succeeded', 'mixed', 'failed', 'rejected'];
  var LABEL = {accepted: 'Starting', submitting: 'Sending', running: 'In progress', paused: 'Needs operator action',
               succeeded: 'Done', mixed: 'Partly done', failed: 'Failed', rejected: 'Rejected', unknown: 'Checking'};
  var POLL_MS = 20000;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function accountKey(label) { return String(label || '').trim().replace(/^facebook\s*/i, '').trim().toLowerCase(); }

  function init(root) {
    var panel = root.querySelector('[data-lqa]');
    var openBtn = root.querySelector('[data-lqa-open]');
    if (!panel || !openBtn) return;
    var account = root.getAttribute('data-landings-account') || '';
    var country = root.getAttribute('data-landings-country') || '';
    var urls = panel.querySelector('[data-lqa-urls]');
    var domain = panel.querySelector('[data-lqa-domain]');
    var domainNote = panel.querySelector('[data-lqa-domain-note]');
    var go = panel.querySelector('[data-lqa-go]');
    var msg = panel.querySelector('[data-lqa-msg]');
    var out = panel.querySelector('[data-lqa-result]');
    var timer = null;

    function say(text, bad) { msg.textContent = text || ''; msg.classList.toggle('cb-lqa-bad', !!bad); }

    function paint(rec) {
      out.replaceChildren();
      if (!rec) return;
      out.appendChild(el('div', null, (LABEL[rec.status] || rec.status) + ' · ' + rec.target_domain + ' · ' + rec.country));
      (rec.result_urls || []).forEach(function (u) {
        var a = el('a', null, u);
        a.href = /^https?:\/\//i.test(u) ? u : '#'; a.target = '_blank'; a.rel = 'noopener noreferrer';
        out.appendChild(a);
      });
      var bad = (rec.result_items || []).filter(function (i) { return i.state === 'failed'; });
      if (bad.length) out.appendChild(el('div', 'cb-lqa-bad', 'Not created for source ' + bad.map(function (i) { return '#' + (i.index + 1); }).join(', ')));
      if (rec.status === 'failed' || rec.status === 'rejected') {
        out.appendChild(el('div', 'cb-lqa-bad', rec.error || 'The landing pages could not be created. Contact support with request ' + rec.request_id + '.'));
      }
    }

    // Последний запрос этого аккаунта: опрос идёт, пока он не завершён и панель открыта.
    function refresh() {
      clearTimeout(timer);
      return fetch('/api/limited-qa/requests').then(function (r) { return r.json(); }).then(function (d) {
        var mine = (d.requests || []).filter(function (r) { return accountKey(r.facebook_account) === accountKey(account); });
        mine.sort(function (a, b) { return String(b.created_local || '').localeCompare(String(a.created_local || '')); });
        var rec = mine[0] || null;
        paint(rec);
        if (rec && TERMINAL.indexOf(rec.status) < 0 && !panel.hidden) timer = setTimeout(refresh, POLL_MS);
      }).catch(function () {});
    }

    function open() {
      panel.hidden = false;
      domain.value = domain.value || root.getAttribute('data-landings-domain') || '';
      domainNote.textContent = domain.value ? '' : 'No domain on this account. Enter the root domain.';
      // Источники — лендинги, уже выбранные на аккаунте; руками можно дописать любые.
      var body = new FormData(); body.append('account_label', account); body.append('op', 'get');
      fetch('/api/account-landings', {method: 'POST', body: body}).then(function (r) { return r.json(); }).then(function (d) {
        if (urls.value.trim()) return;
        urls.value = ((d && d.landings) || []).map(function (l) { return l.url; }).filter(Boolean).join('\n');
      }).catch(function () {});
      refresh();
    }

    function close() { clearTimeout(timer); panel.hidden = true; }

    go.addEventListener('click', function () {
      var list = urls.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      go.disabled = true; say('Sending…');
      fetch('/api/limited-qa/requests', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        source_urls: list, target_domain: domain.value, country: country, facebook_account: account})})
        .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
        .then(function (x) {
          if (x[0]) { say('Request sent.'); paint(x[1]); clearTimeout(timer); timer = setTimeout(refresh, POLL_MS); }
          else say(x[1].error || 'Could not send the request.', true);
        })
        .catch(function () { say('Could not send the request.', true); })
        .then(function () { go.disabled = false; });
    });
    openBtn.addEventListener('click', function () { if (panel.hidden) open(); else close(); });
    panel.querySelector('[data-lqa-close]').addEventListener('click', close);
  }

  document.querySelectorAll('.cb-landings').forEach(init);
}());
