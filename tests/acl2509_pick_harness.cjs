// ACL-2509: after "connect" reloads the page, the drawer keeps the country the
// buyer was in and selects the account that was just added. Runs the real
// block from the template, not a copy.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('/* ACL-2509 pick-after-connect {');
const b = html.indexOf('/* } ACL-2509 pick-after-connect */');
assert(a > 0 && b > a, 'pick-after-connect block not found');
const code = html.slice(a, b);

// One call = one page load: the block's own load-time code runs against the
// shared sessionStorage `store`, like the script does after a reload.
function page(store, tree) {
  const holder = { tree };
  const ctx = {
    Date, JSON,
    sessionStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: k => { delete store[k]; },
    },
    places: () => holder.tree.map(t => ({ country: t.country, name: t.country, accounts: t.accounts.slice() })),
  };
  vm.createContext(ctx);
  vm.runInContext(code + '\nthis.api = {rememberPick, takePick, forgetPick, resolvePick};', ctx);
  ctx.api.holder = holder;
  return ctx.api;
}

const KEY = 'cbd-pick-after-connect';
const store = {};
const before = [{ country: 'AU', accounts: ['AU1'] }, { country: 'KW', accounts: ['FacebookDoc283'] }];
const after = [{ country: 'AU', accounts: ['AU1'] }, { country: 'KW', accounts: ['FacebookDoc283', 'KLD_BFL_F95'] }];

// The real sequence: click handler parks the pick and paints (paintFoot calls
// takePick); the page reloads; the drawer paints again.
const pre = page(store, before);
pre.rememberPick('KW');
assert.equal(pre.takePick(), null, 'pre-reload paint must not apply the pick');
assert.equal(pre.takePick(), null, 'nor do polling paints');
assert(KEY in store, 'the key survives paints before the reload');
const post = page(store, after);               // reload
assert(!(KEY in store), 'read once at page load');
let hit = post.takePick();
assert.equal(hit.country, 'KW'); assert.equal(hit.account, 'KLD_BFL_F95', 'country kept, new account selected');
assert.equal(post.takePick(), null, 'second paint falls back to the normal choice');
assert.equal(page(store, after).takePick(), null, 'a further reload has nothing parked');

// Country not in the tree yet on the first paint after reload: held until it is.
page(store, before).rememberPick('KW');
const late = page(store, []);
assert.equal(late.takePick(), null);
late.holder.tree = after;
assert.equal(late.takePick().account, 'KLD_BFL_F95');

// Closing the form without connecting forgets it.
const p3 = page(store, before);
p3.rememberPick('KW'); p3.forgetPick();
assert.equal(page(store, after).takePick(), null);

// Stale entry (older than the TTL) is dropped, not applied.
store[KEY] = JSON.stringify({ country: 'KW', accounts: [], at: Date.now() - 3600 * 1000 });
assert.equal(page(store, after).takePick(), null);
assert.equal(Object.keys(store).length, 0);

// Nothing new appeared (profile already linked): country kept, no account forced.
page(store, after).rememberPick('KW');
hit = page(store, after).takePick();
assert.equal(hit.country, 'KW'); assert.equal(hit.account, '');

// Storage blocked: no throw.
const broken = { sessionStorage: { getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); }, removeItem() { throw new Error('x'); } } };
const ctx2 = Object.assign({ Date, JSON, places: () => before }, broken);
vm.createContext(ctx2);
vm.runInContext(code + '\nthis.api = {rememberPick, takePick};', ctx2);
ctx2.api.rememberPick('KW');
assert.equal(ctx2.api.takePick(), null);
console.log('ok');
