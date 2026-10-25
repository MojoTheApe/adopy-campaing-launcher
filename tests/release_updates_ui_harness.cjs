const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../src/web/templates/report.html'), 'utf8');
const buttonAt = html.indexOf("var btn = document.getElementById('acl-update-btn')");
const code = html.slice(html.lastIndexOf('(function', buttonAt), html.indexOf('</script>', buttonAt));
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const handlers = {}, intervals = [], alerts = [];
  let reloads = 0;
  const button = { hidden: true, disabled: false, dataset: {}, addEventListener: (e, f) => handlers[e] = f };
  let status = { available: true, supervised: true, channel: 'release', candidate: { version: '2026-09-20' }, fixed: [] };
  const context = {
    document: { getElementById: () => button },
    fetch: async url => ({ ok: true, json: async () => url.includes('/check') ? status :
      url === '/health' ? { app: 'ACL', ok: true, version: '2026-09-20' } : { ok: true } }),
    setInterval: f => intervals.push(f), setTimeout: () => {},
    alert: s => alerts.push(s), confirm: () => true,
    // ACL-141: окна страницы асинхронные; «да» на обновление, текст — как у alert.
    aclAlert: s => { alerts.push(s); return Promise.resolve(); },
    aclConfirm: () => Promise.resolve(true),
    aclInlineError: (_a, s) => { if (s) alerts.push(s); },
    localStorage: { getItem: () => null, setItem: () => {} },
    location: { reload: () => reloads++ }, Date,
  };
  vm.runInNewContext(code, context);
  await flush(); await flush();
  assert.equal(button.hidden, false);
  assert.equal(button.dataset.target, '2026-09-20');
  handlers.click();
  await flush(); await flush();
  assert.equal(reloads, 1, 'a fast restart is detected by version even if no failed health poll was observed');
  button.disabled = false;
  status = { available: false, supervised: true, last_attempt: {
    result: 'rollback_failed', finished_at: '2026-09-20T00:00:00Z', reason: 'previous executable is blocked' } };
  intervals[0]();
  await flush(); await flush();
  assert.equal(button.hidden, true, 'an obsolete offer is hidden');
  assert.match(alerts[0], /could not fully restore/);
  console.log('release update UI behavior OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
