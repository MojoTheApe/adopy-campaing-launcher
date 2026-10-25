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

function fakeDocument() {
  return {createElement: () => ({value: '', textContent: ''})};
}
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
    Object, Promise, String, encodeURIComponent, JSON, document: fakeDocument(),
    prompt: () => prompts.shift(),
    // ACL-141: страница спрашивает своим окном; ответы — те же, что у prompt.
    aclPrompt: (m, o) => Promise.resolve(ctx.prompt(m, o)),
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
    await new Promise(setImmediate);
    assert.match(f.msg.textContent, /did not answer/, 'message is readable before the reload');
    assert.ok(!f.calls.some(c => c.url === 'RELOAD'), 'no reload before the message was shown');
    f.timers.find(x => x.ms === 6000).fn();
    await done;
    const patch = f.calls.find(c => c.url === '/account-map-patch');
    assert.ok(patch, 'row must still be saved after the timeout');
    const body = JSON.parse(patch.opts.body);
    assert.equal(body.profile_id, 'p9');
    assert.equal(body.folder_id, 'f9');
    assert.equal(body.multilogin_instance, 'B');
    assert.equal(body.ad_account_id, 'act_77');
    assert.equal(btn.disabled, true, "still disabled: the original open has not settled");
    assert.ok(!f.calls.some(c => c.url === '/account-links'));
    // manual path pre-fill source
    assert.equal(f.ctx.AMAP_LAST_FOUND.profile_id, 'p9');
  }
})().catch(e => { console.error(e); process.exit(1); });

(async () => {
  // 3. Re-press after a timeout must NOT start a second /mlx-profile-open while the
  //    first server request is still pending (reviewer's adv.cjs case B).
  {
    const f = fixture((url) => url === '/mlx-profile-open' ? new Promise(() => {}) : ok({ok: true}), []);
    const btn = {disabled: false};
    const profile = {profile_id: 'p5', folder_id: '', name: 'N', _details: {folder_id: 'fd5', ad_account_id: 'act_5'}};
    f.ctx.prompt = () => 'N';
    f.inst.value = 'A';
    const done = f.ctx.amapOpenAndConnect(f.box, profile, 'A', btn, {});
    await new Promise(setImmediate);
    f.timers.find(x => x.ms === 75000).fn();
    await new Promise(setImmediate);
    f.timers.find(x => x.ms === 6000).fn();
    await done;
    assert.equal(btn.disabled, true, 'button stays disabled while the original open is in flight');
    f.ctx.prompt = () => null;
    await f.ctx.amapOpenAndConnect(f.box, profile, 'A', {disabled: false}, {});
    assert.equal(f.calls.filter(c => c.url === '/mlx-profile-open').length, 1, 'no second open');
    assert.match(f.msg.textContent, /still opening/);
  }
  // 4. With a country set the timeout message is shown before the reload too.
  {
    const f = fixture((url) => url === '/mlx-profile-open' ? new Promise(() => {}) : ok({ok: true}), []);
    f.box.getAttribute = () => 'Chile';
    const profile = {profile_id: 'p6', folder_id: 'f6', name: 'N', _details: {ad_account_id: 'act_6'}};
    f.ctx.prompt = () => 'N';
    const done = f.ctx.amapOpenAndConnect(f.box, profile, 'A', {disabled: false}, {});
    await new Promise(setImmediate);
    f.timers.find(x => x.ms === 75000).fn();
    await new Promise(setImmediate);
    assert.ok(!f.calls.some(c => c.url === '/account-links'), 'links wait for the message');
    assert.match(f.msg.textContent, /did not answer/);
    f.timers.find(x => x.ms === 6000).fn();
    await done;
    assert.ok(f.calls.some(c => c.url === '/account-links'));
  }
  // 5. Manual-form prefill from the last found profile.
  {
    const f = fixture(() => ok({}), []);
    const ps = {children: [], appendChild(o) { this.children.push(o); }};
    const lf = {profile_id: 'pX', folder_id: 'fX', instance: 'B', name: 'Found One', ad_account_id: 'act_9'};
    assert.equal(f.ctx.amapPrefillSelect(ps, '', [], lf), 'pX');
    assert.equal(ps.children.length, 1);
    assert.match(ps.children[0].textContent, /Found One/);
    assert.equal(f.ctx.amapPrefillSelect(ps, 'mine', [], lf), 'mine', 'a user choice is never overridden');
    const ex = f.ctx.amapManualExtras('pX', null, lf);
    assert.equal(ex.folder_id, 'fX'); assert.equal(ex.multilogin_instance, 'B'); assert.equal(ex.ad_account_id, 'act_9');
    const other = f.ctx.amapManualExtras('pOther', null, lf);
    assert.equal(other.multilogin_instance, undefined); assert.equal(other.ad_account_id, '');
  }
  // 6. Owner hint display.
  {
    const f = fixture(() => ok({}), []);
    const esc = v => String(v);
    assert.match(f.ctx.amapOwnerHint({owner_hint: 'owner in stats: Dan Dicker'}, esc), /owner in stats: Dan Dicker/);
    assert.equal(f.ctx.amapOwnerHint({owner_hint: ''}, esc), '');
  }
})().catch(e => { console.error(e); process.exit(1); });
