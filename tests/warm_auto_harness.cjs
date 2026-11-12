// ACL-2268: the automatic profile start is triggered only by a person, never on load, never without a card.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const code = fs.readFileSync(process.argv[2], 'utf8');

function world(slots, answer) {
  const posts = [];
  const gets = [];
  const listeners = {};
  const line = { style: {}, textContent: '', parentNode: null };
  const field = { value: 'Acc' };
  const form = {
    slots,
    _qs: { '[name="account_label"]': field },
    querySelector(q) { return q === '.warm-auto-line' ? (form._line || null) : (this._qs[q] || null); },
    querySelectorAll(q) { return q === '.cb-slot' ? new Array(form.slots).fill({}) : []; },
    appendChild(el) { form._line = el; },
    closest() { return null; },
  };
  const sb = {
    console, Promise, JSON, Object, encodeURIComponent, String, Array,
    setTimeout: (fn) => { Promise.resolve().then(fn); return 1; },
    document: {
      readyState: 'complete',
      addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
      createElement: () => ({ style: {}, textContent: '', appendChild() {} }),
    },
    fetch: (url, init) => {
      if (url === '/warm/ensure') {
        posts.push(JSON.parse(init.body));
        return Promise.resolve({ status: 202, json: () => Promise.resolve({ accounts: { Acc: answer || 'queued' } }) });
      }
      gets.push(url);
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ accounts: { Acc: { state: 'warm', auto: 'done' } } }) });
    },
    window: { ACL_FB_BACKEND: 'api' },
  };
  vm.createContext(sb);
  vm.runInContext(code, sb);
  const fire = (type, trusted, selector) => {
    const target = { closest: (q) => (q.includes(selector) ? target : (q === 'form.open-account-form' ? form : null)) };
    (listeners[type] || []).forEach((fn) => fn({ type, isTrusted: trusted, target }));
  };
  return { posts, gets, form, fire, sb, listeners };
}

const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  // 1. Page load with a restored draft: no listener fires on its own, no request.
  let w = world(1);
  await tick();
  assert.equal(w.posts.length, 0, 'page load never warms');

  // 2. Programmatic events (draft restore, autofill) are not a person.
  w.fire('input', false, '.cb-slot');
  w.fire('click', false, '.cb-add-btn');
  await tick();
  assert.equal(w.posts.length, 0, 'isTrusted=false never warms');

  // 3. A trusted edit inside a card: one request with the trigger and the card count; repeats stay quiet.
  w.fire('input', true, '.cb-slot');
  await tick();
  w.fire('change', true, '.cb-slot');
  w.fire('click', true, '.cb-dup-slot-btn');
  await tick();
  assert.equal(w.posts.length, 1);
  assert.deepEqual(w.posts[0], { accounts: ['Acc'], trigger: 'edit', campaigns: 1 });
  assert.ok(w.gets.length >= 1, 'status is read from /warm/state only after a queued answer');

  // 4. Add click on an empty account: no card yet -> nothing; once a card exists the same account may ask.
  w = world(0);
  w.fire('click', true, '.cb-add-btn');
  await tick();
  assert.equal(w.posts.length, 0, 'no first campaign, no start');
  w.form.slots = 1;
  w.fire('click', true, '.cb-add-btn');
  await tick();
  assert.equal(w.posts.length, 1);
  assert.equal(w.posts[0].trigger, 'add');

  // 5. Duplicate click.
  w = world(2);
  w.fire('click', true, '.cb-dup-slot-btn');
  await tick();
  assert.equal(w.posts[0].trigger, 'duplicate');
  assert.equal(w.posts[0].campaigns, 2);

  // 6. The drawer calls aclWarmAutoUser: only with a trusted event.
  w = world(1);
  w.sb.window.aclWarmAutoUser(w.form, 'add', false);
  await tick();
  assert.equal(w.posts.length, 0);
  w.sb.window.aclWarmAutoUser(w.form, 'add', true);
  await tick();
  assert.equal(w.posts.length, 1);

  // 7. A refusal is quiet: no state polling, no line text, no button.
  w = world(1, 'recent');
  w.sb.window.aclWarmAutoUser(w.form, 'add', true);
  await tick();
  assert.equal(w.gets.length, 0);
  assert.ok(!w.form._line || !w.form._line.textContent);

  // 8. The RPA backend never asks.
  w = world(1);
  w.sb.window.ACL_FB_BACKEND = 'rpa';
  w.sb.window.aclWarmAutoUser(w.form, 'add', true);
  await tick();
  assert.equal(w.posts.length, 0);
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
