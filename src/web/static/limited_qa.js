(function () {
  'use strict';
  var form = document.getElementById('limited-qa-form');
  var list = document.getElementById('request-list');
  var error = document.getElementById('form-error');
  var submit = document.getElementById('submit-request');
  var enabled = document.body.dataset.enabled === 'true';
  var pollTimer = null;

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
    });
  }

  function requestJson(url, options) {
    return fetch(url, options || {}).then(async function (response) {
      var body = {};
      try { body = await response.json(); } catch (_) {}
      if (!response.ok) throw new Error(body.error || 'The request could not be completed.');
      return body;
    });
  }

  function statusLabel(value) {
    return ({accepted:'Accepted',running:'Running',paused:'Paused',succeeded:'Succeeded',failed:'Failed',mixed:'Partly completed',unknown:'Needs attention'})[value] || 'Unknown';
  }

  function paint(rows) {
    if (!rows.length) {
      list.innerHTML = '<p class="empty">No LP duplication requests yet.</p>';
      return;
    }
    list.innerHTML = rows.map(function (row) {
      var urls = Array.isArray(row.result_urls) ? row.result_urls : [];
      var output = urls.length ? '<ul>' + urls.map(function (url) {
        return '<li><a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(url) + '</a></li>';
      }).join('') + '</ul>' : '';
      var jira = row.jira_issue_key ? '<small>Internal task ' + esc(row.jira_issue_key) + '</small>' : '<small>Waiting for internal task</small>';
      var retry = row.status === 'unknown' ? '<button type="button" data-retry="' + esc(row.request_id) + '">Retry safely</button>' : '';
      var err = row.error ? '<p class="error">' + esc(row.error) + '</p>' : '';
      return '<article class="request" data-id="' + esc(row.request_id) + '">' +
        '<div class="request-head"><span class="badge ' + esc(row.status) + '">' + esc(statusLabel(row.status)) + '</span>' + jira + '</div>' +
        '<small>' + esc((row.source_urls || []).length) + ' LP(s) → ' + esc(row.target_domain) + ' · ' + esc(row.country) + '</small>' +
        output + err + retry + '</article>';
    }).join('');
  }

  function loadRows() {
    if (!enabled) return Promise.resolve([]);
    return requestJson('/api/limited-qa/requests').then(function (body) {
      var rows = body.requests || [];
      paint(rows);
      return rows;
    }).catch(function (exc) {
      list.innerHTML = '<p class="error">' + esc(exc.message) + '</p>';
      return [];
    });
  }

  function refreshOpen(rows) {
    var open = rows.filter(function (row) { return !['succeeded','failed','mixed'].includes(row.status); }).slice(0, 10);
    return Promise.all(open.map(function (row) {
      return requestJson('/api/limited-qa/requests/' + encodeURIComponent(row.request_id)).catch(function () { return null; });
    })).then(loadRows);
  }

  function schedule(rows) {
    clearTimeout(pollTimer);
    if (!enabled) return;
    var hasOpen = rows.some(function (row) { return !['succeeded','failed','mixed'].includes(row.status); });
    if (hasOpen) pollTimer = setTimeout(function () { refreshOpen(rows).then(schedule); }, 10000);
  }

  if (form) form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (!enabled) return;
    error.textContent = '';
    var sourceUrls = document.getElementById('lp-urls').value.split(/\r?\n/).map(function (value) { return value.trim(); }).filter(Boolean);
    var payload = {
      source_urls: sourceUrls,
      target_domain: document.getElementById('target-domain').value.trim(),
      country: document.getElementById('country').value.trim(),
      facebook_account: document.getElementById('facebook-account').value.trim() || null
    };
    submit.disabled = true;
    requestJson('/api/limited-qa/requests', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
      .then(function () { form.reset(); return loadRows(); })
      .then(schedule)
      .catch(function (exc) { error.textContent = exc.message; })
      .finally(function () { submit.disabled = false; });
  });

  list.addEventListener('click', function (event) {
    var button = event.target.closest('[data-retry]');
    if (!button) return;
    button.disabled = true;
    requestJson('/api/limited-qa/requests/' + encodeURIComponent(button.dataset.retry) + '/retry', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
      .then(loadRows).then(schedule)
      .catch(function (exc) { error.textContent = exc.message; })
      .finally(function () { button.disabled = false; });
  });

  loadRows().then(function (rows) { return refreshOpen(rows); }).then(schedule);
}());
