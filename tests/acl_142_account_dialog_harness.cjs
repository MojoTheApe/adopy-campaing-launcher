// ACL-142: the in-page "Read from the open account" dialog and the Connect flow
// around it. Real template code runs in a vm with a tiny fake DOM; no network.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const tpl = fs.readFileSync(process.argv[2], 'utf8');

const a = tpl.indexOf('function amapAccountModel()');
const b = tpl.indexOf('function amapAddThisCountry(');
assert.ok(a > 0 && b > a, 'dialog block not found');
const dialogSrc = tpl.slice(a, tpl.indexOf('function amapReadOpenAccount('));
const askSrc = tpl.slice(tpl.indexOf('function amapAskAccount('), tpl.indexOf('/* } ACL-142 account dialog */'));
const readSrc = tpl.slice(tpl.indexOf('function amapReadOpenAccount('), tpl.indexOf('function amapAskAccount('));
const connectSrc = tpl.slice(tpl.indexOf('async function amapConnect('), b);

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.textContent = '';
    this.value = ''; this.disabled = false; this.hidden = false; this.checked = false; this.listeners = {}; }
  append(...c) { this.children.push(...c); }
  appendChild(c) { this.children.push(c); }
  setAttribute() {}
  addEventListener(n, f) { this.listeners[n] = f; }
  remove() { this.removed = true; }
  close() { this.closed = true; }
  showModal() { this.shown = true; }
}
function world(readAnswers) {
  const made = [];
  const calls = [];
  const ctx = {
    document: { createElement: t => { const e = new El(t); made.push(e); return e; }, body: new El('body') },
    FormData: class { constructor() { this.f = {}; } append(k, v) { this.f[k] = v; } },
    AMAP_PROBE_WAIT_MS: 1, AMAP_OPEN_TIMEOUT_MSG: 'timed out',
    Promise, String, Object, JSON, setTimeout, clearTimeout,
    amapFetchJson: (url, opts) => { calls.push({ url, opts }); return Promise.resolve(readAnswers.shift()); },
  };
  vm.createContext(ctx);
  vm.runInContext(dialogSrc + readSrc + askSrc, ctx);
  const pick = (cls, tag) => made.filter(e => (tag ? e.tag === tag : true) && e.className.split(' ').includes(cls));
  return { ctx, made, calls, pick,
    btn: text => made.find(e => e.tag === 'button' && e.textContent.startsWith(text)),
    tick: () => made.find(e => e.tag === 'input' && e.type === 'checkbox'),
    input: () => made.find(e => e.tag === 'input' && e.type === 'text'),
    info: () => made.find(e => e.className === 'amap-ad-info') };
}
const tick = () => new Promise(r => setImmediate(r));
const found = (act, name) => ({ ok: true, act, account_name: name, needs_confirmation: true });

(async () => {
  // 1. A read result fills the field but cannot be submitted until it is confirmed.
  {
    const w = world([found('act_111', 'KLD BFL X84')]);
    const p = w.ctx.amapAskAccount(() => w.ctx.amapReadOpenAccount({ profile_id: 'p1', folder_id: 'f1' }, 'B'), true);
    await tick(); await tick();
    assert.equal(w.calls[0].url, '/account-probe/ad-account');
    assert.equal(w.calls[0].opts.body.f.profile_id, 'p1');
    assert.equal(w.calls[0].opts.body.f.instance, 'B');
    assert.equal(w.input().value, 'act_111', 'the detected number is put in the field');
    assert.match(w.info().textContent, /KLD BFL X84 \u00b7 act_111/, 'name and number are shown');
    assert.equal(w.btn('Connect').disabled, true, 'not submittable before confirmation');
    assert.equal(w.pick('amap-ad-confirm')[0].hidden, false, 'confirmation is offered');
    assert.match(w.pick('amap-ad-warn')[0].textContent, /default ad account/);
    const t = w.tick(); t.checked = true; t.onchange();
    assert.equal(w.btn('Connect').disabled, false);
    w.btn('Connect').onclick();
    assert.equal(await p, 'act_111');
  }
  // 2. Editing the number is itself the choice: no confirmation needed.
  {
    const w = world([found('act_111', 'Default One')]);
    const p = w.ctx.amapAskAccount(() => w.ctx.amapReadOpenAccount({ profile_id: 'p1' }, 'A'), true);
    await tick(); await tick();
    const i = w.input(); i.value = 'act_222'; i.oninput();
    assert.equal(w.pick('amap-ad-confirm')[0].hidden, true);
    assert.equal(w.btn('Connect').disabled, false);
    w.btn('Connect').onclick();
    assert.equal(await p, 'act_222');
  }
  // 3. A failed read explains itself and still allows connecting without a number.
  {
    const w = world([{ ok: false, error: 'no ad account number' }]);
    const p = w.ctx.amapAskAccount(() => w.ctx.amapReadOpenAccount({ profile_id: 'p1' }, 'A'), true);
    await tick(); await tick();
    assert.match(w.info().textContent, /Could not read it from the profile: no ad account number/);
    assert.equal(w.btn('Connect without a number').disabled, false);
    w.btn('Connect without a number').onclick();
    assert.equal(await p, '');
  }
  // 4. The button reads again and a fresh result needs a fresh confirmation.
  {
    const w = world([found('act_1', 'First'), found('act_2', 'Second')]);
    const p = w.ctx.amapAskAccount(() => w.ctx.amapReadOpenAccount({ profile_id: 'p1' }, 'A'), true);
    await tick(); await tick();
    const t = w.tick(); t.checked = true; t.onchange();
    w.btn('Read from the open account').onclick();
    await tick(); await tick();
    assert.equal(w.input().value, 'act_2');
    assert.equal(w.btn('Connect').disabled, true, 'a new result is not covered by the old tick');
    w.btn('Cancel').onclick();
    assert.equal(await p, null);
  }
  // 5. The Connect flow saves what the dialog returned, and nothing on Cancel.
  for (const [answer, expectPatch] of [['act_9', true], [null, false]]) {
    const calls = [];
    const msg = { textContent: '', className: '' };
    const box = { getAttribute: () => '', querySelector: () => msg };
    const ctx = {
      calls, FormData: class {}, Object, Promise, String, JSON, encodeURIComponent,
      prompt: () => 'KLD BFL X84', aclPrompt: async () => 'KLD BFL X84', aclConfirm: async () => true, location: { reload() { calls.push({ url: 'RELOAD' }); } },
      amapAskAccount: async () => answer, amapReadOpenAccount() {},
      amapSend() { calls.push({ url: '/account-links' }); }, setTimeout, AMAP_OPEN_TIMEOUT_MSG: '',
      fetch: (url, opts) => { calls.push({ url, opts }); return Promise.resolve({ json: async () => ({ ok: true }) }); },
    };
    vm.createContext(ctx);
    vm.runInContext(connectSrc, ctx);
    await ctx.amapConnect(box, { profile_id: 'p1', folder_id: 'f1', name: 'KLD', _details: {} }, 'A', { disabled: false }, true);
    const patch = calls.find(c => c.url === '/account-map-patch');
    assert.equal(!!patch, expectPatch);
    if (patch) assert.equal(JSON.parse(patch.opts.body).ad_account_id, 'act_9');
  }
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
