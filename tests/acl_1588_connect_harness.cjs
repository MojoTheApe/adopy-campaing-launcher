// ACL-1588: Connect без страны не ходит в /account-links; таймаут открытия
// профиля даёт сообщение и всё равно сохраняет строку с профилем.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const template = fs.readFileSync(process.argv[2], 'utf8');
const start = template.indexOf('var AMAP_OPEN_WAIT_MS');
const end = template.indexOf('function amapAddThisCountry(');
assert.ok(start > 0 && end > start, 'connect block not found');
// Keep only the functions we need, cut out the unrelated middle block.
const open = template.slice(start, template.indexOf('/* Открыть и подключить одним нажатием.'));
const both = template.slice(template.indexOf('async function amapOpenAndConnect('), template.indexOf('/* One credential form is mounted'));
const connect = template.slice(template.indexOf('async function amapConnect('), end);

function fixture(handlers, prompts) {
  const calls = [];
  const timers = [];
  const msg = {textContent: '', className: ''};
  const inst = {value: 'A'};
  const box = {
    getAttribute: () => '',
    querySelector: sel => ({'.amap-msg': msg, '.amap-inst': inst}[sel]),
  };
  const ctx = {
    calls, msg, FormData: class { constructor() { this.f = {}; } append(k, v) { this.f[k] = v; } },
    Object, Promise, String, encodeURIComponent, JSON,
    prompt: () => prompts.shift(),
    amapHint() {}, location: {reload() { calls.push({url: 'RELOAD'}); }},
    amapSend() { calls.push({url: '/account-links'}); },
    setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; },
    clearTimeout() {},
    fetch: (url, opts) => { calls.push({url, opts}); return handlers(url, opts); },
  };
  vm.createContext(ctx);
  vm.runInContext(open + both + connect, ctx);
  return {ctx, box, msg, calls, timers, inst};
}
const ok = body => Promise.resolve({json: async () => body});

(async () => {
  // 1. Empty country: the row is saved, /account-links is never called.
  {
    const f = fixture((url) => ok(url === '/account-map-patch' ? {ok: true} : {ok: true}), []);
    const btn = {disabled: false};
    const profile = {profile_id: 'p1', folder_id: 'f1', name: 'FacebookElf403 - Bob', _details: {ad_account_id: 'act_1', suggested_name: 'FacebookElf403 - Bob'}};
    f.ctx.prompt = () => 'FacebookElf403 - Bob';
    await f.ctx.amapConnect(f.box, profile, 'A', btn, true);
    const urls = f.calls.map(c => c.url);
    assert.ok(urls.includes('/account-map-patch'), 'row must be saved');
    assert.ok(!urls.includes('/account-links'), 'no /account-links call for an empty country');
    assert.ok(urls.includes('RELOAD'));
    assert.equal(JSON.parse(f.calls.find(c => c.url === '/account-map-patch').opts.body).profile_id, 'p1');
  }
  // 2. Timeout: message, then the row is still saved with profile/folder/instance.
  {
    const f = fixture((url) => url === '/mlx-profile-open' ? new Promise(() => {}) : ok({ok: true}), []);
    const btn = {disabled: false};
    const profile = {profile_id: 'p9', folder_id: 'f9', name: 'FacebookElf403 - Bob Marcus', _details: {ad_account_id: 'act_77', suggested_name: 'FacebookElf403 - Bob Marcus'}};
    f.ctx.prompt = () => 'FacebookElf403 - Bob Marcus';
    f.inst.value = 'B';
    const done = f.ctx.amapOpenAndConnect(f.box, profile, 'B', btn, {});
    await new Promise(setImmediate);
    const t = f.timers.find(x => x.ms >= 60000 && x.ms <= 90000);
    assert.ok(t, 'a 60-90 s wait must be armed');
    t.fn();
    await done;
    assert.match(f.msg.textContent, /did not answer/);
    const patch = f.calls.find(c => c.url === '/account-map-patch');
    assert.ok(patch, 'row must still be saved after the timeout');
    const body = JSON.parse(patch.opts.body);
    assert.equal(body.profile_id, 'p9');
    assert.equal(body.folder_id, 'f9');
    assert.equal(body.multilogin_instance, 'B');
    assert.equal(body.ad_account_id, 'act_77');
    assert.equal(btn.disabled, false);
    assert.ok(!f.calls.some(c => c.url === '/account-links'));
    // manual path pre-fill source
    assert.equal(f.ctx.AMAP_LAST_FOUND.profile_id, 'p9');
  }
})().catch(e => { console.error(e); process.exit(1); });
