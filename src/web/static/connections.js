/* ACL-2306 / ACL-2311: the Connections dialog (Library / BI / Multilogin).
   Loaded from /static/connections.js, so it is always the code of the CURRENT build:
   report.html includes it, and the saved (cached) page shown right after a restart
   gets the same tag and a Connections button from report_store.decorate.
   Values are never shown or logged: green/red lines only. Server text goes in via
   textContent. Nothing here touches the DOM at load time except window.aclConnections. */
(function () {
  'use strict';

  function post(url) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'Unexpected answer.' }; }); });
  }

  // What the lead sees after "Publish my connection settings". Pure: no DOM, no values.
  function publishLines(j) {
    var out = [];
    if (!j || !j.ok) return out;
    out.push({ ok: !!j.library, line: j.library ? 'Team library: published' : 'Team library: not published' });
    out.push({ ok: !!j.bi, line: j.bi ? 'BI: published' : 'BI: not published' });
    (j.notes || []).forEach(function (n) { out.push({ ok: false, line: String(n) }); });
    return out;
  }

  function ask(question, okLabel) {
    if (typeof window.aclConfirm === 'function') {
      return window.aclConfirm(question, { title: 'Publish connection settings', okLabel: okLabel });
    }
    return Promise.resolve(typeof window.confirm === 'function' ? window.confirm(question) : false);
  }

  function open() {
    var old = document.getElementById('cx-box'); if (old) old.remove();
    var box = document.createElement('div');
    box.id = 'cx-box'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', 'Connections');
    box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:9999';
    box.innerHTML = '<div style="background:var(--bg-elev,#fff);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:1.1rem 1.2rem;width:min(32rem,92vw)">'
      + '<h3 style="margin:0 0 .5rem">Connections</h3>'
      + '<p style="margin:0 0 .7rem;color:var(--muted);font-size:var(--adopy-text-sm)">Sign in with your Adopy Google account. ACL reads the team connection file shared with you and saves the settings on this computer only. Nothing is shown or pasted.</p>'
      + '<ul id="cx-lines" style="list-style:none;padding:0;margin:0 0 .6rem"></ul>'
      + '<p id="cx-msg" role="status" style="min-height:1.4em;margin:.4rem 0"></p>'
      + '<div style="display:flex;gap:.5rem;justify-content:flex-end;flex-wrap:wrap">'
      + '<button type="button" id="cx-publish" class="amap-btn" title="Team lead only: write the connection settings loaded in this dashboard to the team connection file.">Publish my connection settings</button>'
      + '<button type="button" id="cx-close" class="amap-btn">Close</button>'
      + '<button type="button" id="cx-check" class="amap-btn">Check</button>'
      + '<button type="button" id="cx-go" class="amap-btn primary">Sign in with Google</button></div></div>';
    document.body.appendChild(box);
    var lines = box.querySelector('#cx-lines'), msg = box.querySelector('#cx-msg');
    function paint(checks) {
      lines.innerHTML = '';
      (checks || []).forEach(function (c) {
        var li = document.createElement('li');
        li.style.color = c.ok ? 'var(--good)' : 'var(--danger)';
        li.textContent = (c.ok ? '✓ ' : '✕ ') + c.line;
        lines.appendChild(li);
      });
    }
    function say(t, bad) { msg.style.color = bad ? 'var(--danger)' : 'var(--muted)'; msg.textContent = t || ''; }
    function run(url, busyText) {
      say(busyText);
      post(url).then(function (j) {
        if (j && j.ok) { say(''); paint(j.checks); } else { say((j && j.error) || 'Could not reach the team connection file.', true); }
      }).catch(function () { say('Could not reach the dashboard.', true); });
    }
    function publish() {
      var button = box.querySelector('#cx-publish');
      ask('Write the connection settings loaded in this dashboard to the team connection file? '
        + 'It replaces what is there now, and everyone on the team gets it at their next sign-in.', 'Publish')
        .then(function (yes) {
          if (!yes) return;
          button.disabled = true; say('Publishing…');
          return post('/api/connections/publish').then(function (j) {
            if (j && j.ok) { say('Published.'); paint(publishLines(j)); }
            else { lines.innerHTML = ''; say((j && j.error) || 'Could not publish. Try again.', true); }
          }).catch(function () { say('Could not reach the dashboard.', true); })
            .then(function () { button.disabled = false; });
        });
    }
    box.querySelector('#cx-close').onclick = function () { box.remove(); };
    box.querySelector('#cx-go').onclick = function () { run('/api/connections/sign-in', 'Waiting for Google sign-in in your browser…'); };
    box.querySelector('#cx-check').onclick = function () { run('/api/connections/check', 'Checking…'); };
    box.querySelector('#cx-publish').onclick = publish;
    run('/api/connections/check', 'Checking…');
  }

  window.aclConnections = { open: open, publishLines: publishLines };
})();
