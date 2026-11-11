// ACL-2509: after "connect" reloads the page, the drawer keeps the country the
// buyer was in and selects the account that was just added. Runs the real
// block from the template, not a copy.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('/* ACL-2509 pick-after-connect {');
const b = html.indexOf('/* } ACL-2509 pick-after-connect */');
assert(a > 0 && b > a, 'pick-after-connect block not found');
const code = html.slice(a, b);

function page(store, tree) {
  const ctx = {
    Date, JSON,
    sessionStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: k => { delete store[k]; },
    },
    places: () => tree.map(t => ({ country: t.country, name: t.country, accounts: t.accounts.slice() })),
  };
  vm.createContext(ctx);
  vm.runInContext(code + '\nthis.api = {rememberPick, takePick, forgetPick, resolvePick};', ctx);
  return ctx.api;
}

const store = {};
// Before the connect: Kuwait has one account, Australia first in the list.
const before = [{ country: 'AU', accounts: ['AU1'] }, { country: 'KW', accounts: ['FacebookDoc283'] }];
page(store, before).rememberPick('KW');

// After the reload: Kuwait has the new account too.
const after = [{ country: 'AU', accounts: ['AU1'] }, { country: 'KW', accounts: ['FacebookDoc283', 'KLD_BFL_F95'] }];
let hit = page(store, after).takePick();
assert.equal(hit.country, 'KW'); assert.equal(hit.account, 'KLD_BFL_F95', 'country kept, new account selected');
assert.equal(Object.keys(store).length, 0, 'used once');
assert.equal(page(store, after).takePick(), null, 'second paint falls back to the normal choice');

// Country not in the tree yet (early paint): stays parked, then resolves.
page(store, before).rememberPick('KW');
assert.equal(page(store, []).takePick(), null);
assert.equal(Object.keys(store).length, 1, 'kept until the tree is there');
assert.equal(page(store, after).takePick().account, 'KLD_BFL_F95');

// Closing the form without connecting forgets it.
page(store, before).rememberPick('KW');
page(store, before).forgetPick();
assert.equal(page(store, after).takePick(), null);

// Stale entry (older than the TTL) is dropped, not applied.
store['cbd-pick-after-connect'] = JSON.stringify({ country: 'KW', accounts: [], at: Date.now() - 3600 * 1000 });
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
